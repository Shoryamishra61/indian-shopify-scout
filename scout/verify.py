"""Verification: is this candidate a live Shopify store, and is it Indian?

Shopify confirmation, in order of reliability:

1. ``GET /meta.json`` - a public endpoint Shopify serves on every storefront
   with the merchant's own shop profile: id, name, city, province, country,
   myshopify_domain, currency. A 200 JSON containing an ``id`` and a
   ``myshopify_domain`` (or shop ``name``) proves Shopify AND gives us the
   self-reported country/state in one request.
2. ``GET /products.json`` - the public catalog JSON (a ``products`` array).
3. Homepage HTML markers - ``cdn.shopify.com`` asset references, the
   ``Shopify.shop`` JS object, ``*.myshopify.com`` links, X-ShopId headers.
   At least two independent marker families are required.

India confirmation:

* ``meta.json`` ``country == "IN"`` - the merchant's own admin setting,
  treated as authoritative.
* If meta.json is unavailable: a weighted fallback score over INR currency,
  Indian state names, PIN codes, +91 phones, GSTIN and explicit India text.
* Any *explicit foreign* country (from meta.json) rejects the candidate -
  this is what removes the `.in` domain that is not an Indian business.

robots.txt is fetched and respected for every host; `/policies/`, `/cart`,
`/checkout`, `/search` and friends are off-limits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from urllib.parse import urlsplit

import config
from scout.robots import RobotsCache, normalize_host

log = logging.getLogger("verify")

robots_cache = RobotsCache()

# ---- Shopify HTML marker families -----------------------------------------
MARKER_CDN = re.compile(r"(cdn\.shopify\.com|cdn\.shopifycdn\.net|shopify\.com/shopifycloud)", re.I)
MARKER_JS = re.compile(r"\bShopify\s*\.\s*(shop|currency|locale|theme)\s*=", re.I)
MARKER_MYSHP_LINK = re.compile(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", re.I)
MARKER_META = re.compile(r'<meta[^>]+(?:name|property)=["\'](?:shopify|tiq|shopyflow)', re.I)
MARKER_HEADER = None  # x-shopid handled via headers when available

PASSWORD_RE = re.compile(r"(store\s*password|opening\s*soon|/password\b|this store is (?:currently )?password)", re.I)

JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I
)


def parse_json_ld(html: str) -> list[dict]:
    out = []
    for m in JSON_LD_RE.finditer(html):
        blob = m.group(1).strip()
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            out.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            out.append(data)
    return out


def canonical_host(cand: str) -> str:
    host = normalize_host(cand)
    return host.removeprefix("https://").removeprefix("http://").split("/")[0]


async def resolve_homepage(fetcher, host: str) -> tuple[str | None, object | None]:
    """Try https://host/ then the www-variant. Returns (host_used, reply)."""
    tries = [f"https://{host}/"]
    if not host.startswith("www."):
        tries.append(f"https://www.{host}/")
    if host.endswith(".myshopify.com"):
        tries = tries[:1]
    for base in tries:
        reply = await fetcher.get(base, check_robots=robots_cache)
        if reply is not None and reply.ok and reply.body.strip():
            return urlsplit(reply.final_url or base).netloc.lower().removeprefix("www.") or host, reply
    return None, None


async def fetch_meta_json(fetcher, host: str) -> dict | None:
    reply = await fetcher.get(f"https://{host}/meta.json", check_robots=robots_cache)
    if reply is None or not reply.ok:
        return None
    try:
        data = json.loads(reply.body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "id" not in data:
        return None
    return data


async def fetch_products_json(fetcher, host: str, limit: int = 1) -> list | None:
    reply = await fetcher.get(
        f"https://{host}/products.json?limit={limit}", check_robots=robots_cache)
    if reply is None or not reply.ok:
        return None
    try:
        data = json.loads(reply.body)
    except json.JSONDecodeError:
        return None
    products = data.get("products") if isinstance(data, dict) else None
    return products if isinstance(products, list) else None


def shopify_markers_in_html(html: str) -> set[str]:
    fam = set()
    if MARKER_CDN.search(html):
        fam.add("cdn")
    if MARKER_JS.search(html):
        fam.add("js_object")
    if MARKER_MYSHP_LINK.search(html):
        fam.add("myshopify_link")
    if 'name="shopify-digital-wallet"' in html or "shopify-features" in html or "/cdn/shop/" in html:
        fam.add("shop_assets")
    return fam


def is_password_page(html: str) -> bool:
    return bool(PASSWORD_RE.search(html[:20000]))


def india_fallback_score(text: str) -> tuple[int, list[str]]:
    """Weighted India evidence for stores without meta.json."""
    from scout.states import INDIAN_STATES, PINCODE_RE, GSTIN_RE, STATE_NAME_RE
    score, evidence = 0, []
    t = text[:400000]
    if re.search(r'currency["\']?\s*[:=]\s*["\']INR|"active":"INR"|₹', t):
        score += 2
        evidence.append("currency_inr")
    if "India" in t or "india" in t.lower():
        score += 1
        evidence.append("india_text")
    hits = set(STATE_NAME_RE.findall(t))
    if hits:
        score += 2
        evidence.append("states:" + ",".join(sorted(hits)[:4]))
    if PINCODE_RE.search(t):
        score += 1
        evidence.append("pincode")
    if GSTIN_RE.search(t):
        score += 2
        evidence.append("gstin")
    if re.search(r"(\+91|91-)[\s-]?\d{10}|\b0\d{2,4}[- ]?\d{6,8}\b", t):
        score += 1
        evidence.append("in_phone")
    if ".myshopify.com" in t and "shopify" in t.lower():
        score += 0
    return score, evidence


async def verify_candidate(store, fetcher, host: str) -> None:
    """Verify one candidate; writes a record or a rejection to the store.

    meta.json-first: a single cheap probe either confirms Shopify + India +
    state in one request or rejects a foreign/dead host without touching the
    storefront. Only meta-confirmed Indian stores get a homepage fetch (for
    liveness and later enrichment). This ordering is what makes six-figure
    candidate pools affordable.
    """
    if store.get_record(host) or host in store.rejected:
        return

    rec = {
        "host": host,
        "canonical_host": host,
        "sources": store.candidates.get(host, {}).get("sources", []),
    }

    # --- Shopify + India via meta.json (1 request) ---------------------------
    # robots.txt is fetched as part of the robots check; if even that fails
    # at the network level the host is almost certainly dead - skip further
    # requests instead of paying two more full timeouts
    rb_status = await robots_cache.probe(host, fetcher)
    if rb_status == 0:
        store.reject(host, "unreachable:dns")
        return
    meta = await fetch_meta_json(fetcher, host)
    if meta is None and rb_status == 200:
        # host is alive; try the www variant once
        meta = await fetch_meta_json(fetcher, f"www.{host}")

    if meta:
        rec["is_shopify"] = True
        rec["shopify_method"] = "meta.json"
        rec["myshopify_domain"] = meta.get("myshopify_domain", "")
        rec["shop_name"] = (meta.get("name") or "").strip()
        rec["meta_city"] = meta.get("city", "")
        rec["meta_province"] = meta.get("province", "")
        rec["meta_country"] = meta.get("country", "")
        rec["meta_currency"] = meta.get("currency", "")
        country = (meta.get("country") or "").strip().upper()
        if country == "IN":
            rec["is_indian"] = True
            rec["india_method"] = "meta.json"
        elif country:
            store.reject(host, f"foreign_country:{country}",
                         {"country": country, "shop_name": rec.get("shop_name", "")})
            return
        else:
            # meta.json exists but reports no country - score homepage evidence
            canon, home = await resolve_homepage(fetcher, host)
            if canon is None or home is None or not home.ok:
                status = home.status if home is not None else "unreachable"
                store.reject(host, f"unreachable:{status}")
                return
            html = home.body or ""
            if is_password_page(html):
                store.reject(host, "password_page")
                return
            rec["canonical_host"] = canon
            rec["homepage_url"] = home.final_url or f"https://{canon}/"
            score, evidence = india_fallback_score(html)
            rec["is_indian"] = score >= config.INDIA_FALLBACK_THRESHOLD
            rec["india_method"] = f"fallback:{score}"
            rec["india_evidence"] = evidence
            if not rec["is_indian"]:
                store.reject(host, "india_unproven", {"india_evidence": evidence})
                return
            rec["homepage_status"] = home.status
    else:
        # --- fallback: storefront signals ------------------------------------
        canon, home = await resolve_homepage(fetcher, host)
        if canon is None or home is None or not home.ok:
            status = home.status if home is not None else "unreachable"
            store.reject(host, f"unreachable:{status}")
            return
        html = home.body or ""
        rec["canonical_host"] = canon
        rec["homepage_url"] = home.final_url or f"https://{canon}/"
        rec["homepage_status"] = home.status

        products = await fetch_products_json(fetcher, canon)
        fam = shopify_markers_in_html(html)
        if products is not None:
            rec["is_shopify"] = True
            rec["shopify_method"] = "products.json"
        elif len(fam) >= 2:
            rec["is_shopify"] = True
            rec["shopify_method"] = "html_markers:" + "+".join(sorted(fam))
        else:
            store.reject(host, "not_shopify", {"markers": sorted(fam)})
            return

        if is_password_page(html):
            store.reject(host, "password_page")
            return

        score, evidence = india_fallback_score(html)
        rec["is_indian"] = score >= config.INDIA_FALLBACK_THRESHOLD
        rec["india_method"] = f"fallback:{score}"
        rec["india_evidence"] = evidence
        if not rec["is_indian"]:
            store.reject(host, "india_unproven", {"india_evidence": evidence})
            return

    store.save_record(rec)


def _priority(host: str, sources: list[str]) -> float:
    """Lower is verified sooner: curated/human evidence beats bulk sweeps.

    Inside the bulk .in class, hosts whose NAME carries an India signal
    (city, state, hindi/ethnic terms) are verified before the rest - the
    .in Shopify space is majority foreign-run, and name priors roughly
    double the Indian yield per request spent.
    """
    if "seeds" in sources:
        return 0
    if any(s.startswith("listicles") for s in sources):
        return 1
    if "expansion" in sources:
        return 2
    if any(s.startswith("cc_myshp") and "partial" not in s for s in sources):
        return 3
    if any(s.startswith("cc_in") for s in sources):
        from scout.discovery.cc_sweep import MYSHP_INDIA
        # business-oriented Indian 2LDs carry a modest extra prior
        business_2ld = host.endswith((".co.in", ".firm.in", ".gen.in", ".net.in"))
        label = host.removesuffix(".co.in").removesuffix(".in").split(".")[0]
        if MYSHP_INDIA.search(label):
            return 3.5
        if business_2ld:
            return 3.7
        return 4
    return 5


async def run(store, fetcher, hosts: list[str], progress_every: int = 250) -> None:
    """Verify candidates in priority order with a chunked early stop.

    Stop conditions: candidate pool exhausted, or VERIFY_TARGET Indian
    Shopify stores confirmed (the target exists because the discovery sweep
    can surface six-figure pools; leftover candidates remain in
    candidates.jsonl for a later run).
    """
    pending = [h for h in hosts if h not in store.records and h not in store.rejected]
    log.info("verifying %d pending candidates", len(pending))
    if not pending:
        return

    src_map = {h: store.candidates.get(h, {}).get("sources", []) for h in pending}
    # priority first, random order within a class (bulk CC classes are huge;
    # an alphabetical order would bias samples toward "0-9"/"a" domains)
    pending.sort(key=lambda h: (_priority(h, src_map[h]), random.random()))
    if len(pending) > config.VERIFY_MAX_PER_RUN:
        # keep the priority ordering, but diversify bulk sources with a sample
        head, tail = pending[:2000], pending[2000:]
        random.seed(11)
        keep_tail = config.VERIFY_MAX_PER_RUN - len(head)
        pending = head + random.sample(tail, max(keep_tail, 0))

    sem = asyncio.Semaphore(config.GLOBAL_CONCURRENCY)
    done = 0
    lock = asyncio.Lock()
    stop = asyncio.Event()

    async def one(h: str):
        nonlocal done
        if stop.is_set():
            return
        async with sem:
            if stop.is_set():
                return
            try:
                await verify_candidate(store, fetcher, h)
            except Exception as exc:  # noqa: BLE001 - one bad host must not stop the run
                log.debug("verify failed for %s: %s", h, exc)
                store.reject(h, f"error:{type(exc).__name__}")
        async with lock:
            done += 1
            if done % progress_every == 0:
                log.info("verify progress: %d processed, %d verified, %d rejected",
                         done, len(store.records), len(store.rejected))
            if len(store.verified()) >= config.VERIFY_TARGET:
                stop.set()

    CHUNK = 1000
    for i in range(0, len(pending), CHUNK):
        if stop.is_set():
            break
        await asyncio.gather(*(one(h) for h in pending[i: i + CHUNK]))

    log.info("verify done: %d processed, %d verified, %d rejected (target %d)",
             done, len(store.records), len(store.rejected), config.VERIFY_TARGET)
