"""Field extraction for verified stores.

Every store already has its homepage HTML cached from verification. Here we
add the public catalog (`/products.json?limit=100`) for category evidence and
up to MAX_EXTRA_PAGES extra robots-allowed pages (contact, about, faq...)
that link from the homepage - where merchants usually publish emails,
phones and addresses. All extraction is regex/JSON-LD based, no rendering.

Field provenance is recorded per store so the export can explain itself.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import logging
import re
from urllib.parse import urljoin, urlsplit

import config
from scout.categories import classify
from scout.states import (
    GSTIN_RE,
    normalize_province,
    state_from_city,
    state_from_gstin,
    state_from_pincode,
    state_from_text,
)
from scout.verify import parse_json_ld, robots_cache

log = logging.getLogger("enrich")

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
)
MAILTO_RE = re.compile(r'href=["\']mailto:([^"\'?]+)', re.I)
TEL_RE = re.compile(r'href=["\']tel:([^"\']+)', re.I)

# Indian mobiles: +91 / 0 / bare 10-digit starting 6-9; landlines with STD
PHONE_RE = re.compile(
    r"(?:\+91[\s\-]?)?[6-9]\d{9}\b"
    r"|\b0\d{2,4}[\s\-]?\d{6,8}\b"
)
JUNK_EMAIL_PARTS = (
    "example.com", "sentry", "wixpress", "godaddy", "domain", "noreply",
    "no-reply", "donotreply", "shopify.com", "klaviyo", "omnisend",
    "mailchimp", "yotpo", "judge.me", "stamped.io", "loox", "woocommerce",
    "abc.com", "email.com", "yourdomain", "sentry.io", "myshopify.com",
)
JUNK_EMAIL_LOCAL = re.compile(r"^(u?n?subscribe|abuse|postmaster|webmaster|hostmaster|privacy|test)?" )

SOCIAL_PATTERNS = [
    ("instagram", re.compile(r"https?://(?:www\.)?instagram\.com/(?!p/|reel/|explore/|accounts/)[A-Za-z0-9_.\-/]+", re.I)),
    ("facebook", re.compile(r"https?://(?:www\.|m\.|web\.)?facebook\.com/(?!sharer|share\.php|dialog|tr\b|plugins|hashtag|groups/[0-9]+/)[A-Za-z0-9_.\-/%]+", re.I)),
    ("twitter", re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/(?!share|intent|home|hashtag|search)[A-Za-z0-9_\-/]+", re.I)),
    ("linkedin", re.compile(r"https?://(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/(?:company|in|school)/[A-Za-z0-9_%\-.]+/?", re.I)),
    ("youtube", re.compile(r"https?://(?:www\.)?youtube\.com/(?:c/|channel/|user/|@)[A-Za-z0-9_\-/@]+", re.I)),
    ("pinterest", re.compile(r"https?://(?:www\.|in\.)?pinterest\.(?:com|in)/(?!pin/)[A-Za-z0-9_.\-/]+", re.I)),
    ("whatsapp", re.compile(r"https?://(?:api\.wa\.me|wa\.me|api\.whatsapp\.com|chat\.whatsapp\.com)/\S*", re.I)),
    ("telegram", re.compile(r"https?://(?:www\.)?t\.me/\S+", re.I)),
]

META_DESC_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\'](description|og:description|twitter:description)["\'][^>]+content=["\']([^"\']*)["\']', re.I)
META_DESC_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:name|property)=["\'](description|og:description|twitter:description)["\']', re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)

LOGO_JSONLD_RE = re.compile(r'"logo"\s*:\s*"([^"]+)"', re.I)
IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
ATTR_RE = lambda name, tag: re.compile(rf'{name}=["\']([^"\']+)', re.I)
SRC_RE = re.compile(r'(?:data-src|data-srcset|src|srcset)=["\']([^"\']+)', re.I)
ALT_RE = re.compile(r'alt=["\']([^"\']*)', re.I)
CLASS_RE = re.compile(r'class=["\']([^"\']*)', re.I)
ID_RE = re.compile(r'id=["\']([^"\']*)', re.I)
HEADER_BLOCK_RE = re.compile(
    r"<header\b.*?</header>|<div[^>]+class=[\"'][^\"']*(?:header|banner|announcement)[^\"']*\".*?</div>",
    re.S | re.I)

PAYMENT_LOGO_RE = re.compile(
    r"(paytm|phonepe|razorpay|upi|visa|mastercard|gpay|google-?pay|apple-?pay|"
    r"amex|cod|cash[-_ ]?on[-_ ]?deli|dhl|fedex|shiprocket|delhivery|payments?|"
    r"stripe|paypal|secure|rupee|emi|badge|icon|star[-_ ]?rating)", re.I)
FAVICON_HINT_RE = re.compile(r"(favicon|apple-?touch|shortcut|icon\.(png|ico|jpg|svg))", re.I)


def clean_text(s: str, limit: int = 300) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s[:limit].strip()


JUNK_EMAIL_LOCAL_RE = re.compile(
    r"^(abc|abcd|abc123|test|test1|example|sample|foo|bar|baz|xyz|john|jane|"
    r"doe|user|username|your-?name|your-?email|email|name|firstname|"
    r"someone|something|you|your)[0-9.@]*$",
    re.I,
)


def _clean_email(raw: str) -> str | None:
    email = raw.strip().strip(".").strip("><\"'=")
    # HTML-escaped or JS-escaped prefixes: \u003c, \u003e, &gt;, &lt;
    email = re.sub(r"^(\\?u003[ce]|&gt;|&lt;|>+|<+)+", "", email, flags=re.I)
    email = email.strip().lower()
    if not email or "@" not in email:
        return None
    local, _, domain = email.partition("@")
    if any(j in domain for j in JUNK_EMAIL_PARTS):
        return None
    if domain.endswith((".js", ".json", ".css", ".map", ".bundle")):
        return None
    if JUNK_EMAIL_LOCAL_RE.match(local):
        return None
    if len(local) < 2 or len(domain) < 5 or "." not in domain:
        return None
    if re.fullmatch(r"[0-9a-f]{16,}", local):  # hash-like junk
        return None
    return email


def extract_emails(html: str) -> list[str]:
    found: list[str] = []
    for m in MAILTO_RE.finditer(html):
        found.append(m.group(1).strip())
    # obfuscated "name [at] domain [dot] com"
    html_deob = re.sub(r"\s*\[\s*at\s*\]\s*", "@", html, flags=re.I)
    html_deob = re.sub(r"\s*\[\s*dot\s*\]\s*", ".", html_deob, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html_deob)
    found += EMAIL_RE.findall(text)
    out, seen = [], set()
    for raw in found:
        email = _clean_email(raw)
        if email and email not in seen:
            seen.add(email)
            out.append(email)
    return out[:6]


def _normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0091"):
        digits = "91" + digits[4:]
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "123456789":
        if digits[0] in "6789":
            return "+91 " + digits
        return None
    if 10 <= len(digits) <= 12 and digits[0] == "0":
        return "+91 " + digits[1:]
    return None


def extract_phones(html: str) -> list[str]:
    found: list[str] = []
    for m in TEL_RE.finditer(html):
        found.append(m.group(1))
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("\u200b", "").replace("\u00a0", " ")
    found += PHONE_RE.findall(text)
    out, seen = [], set()
    for raw in found:
        norm = _normalize_phone(raw)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out[:4]


def extract_socials(html: str) -> dict[str, str]:
    socials: dict[str, str] = {}
    for m in re.finditer(r'href=["\'](https?://[^"\']+)', html, re.I):
        href = m.group(1)
        if re.search(r"(utm_|fbclid|gclid)", href, re.I):
            href = re.sub(r"[?&](utm_[^&]+|fbclid|gclid)=[^&]*", "", href)
        for platform, pat in SOCIAL_PATTERNS:
            if platform in socials:
                continue
            if pat.match(href):
                if re.search(r"/(sharer|share|intent|dialog)/", href):
                    continue
                socials[platform] = href.split("?")[0].rstrip("/")
                break
    return socials


def extract_tagline(html: str, shop_name: str) -> tuple[str | None, str]:
    for m in META_DESC_RE.finditer(html):
        desc = clean_text(m.group(2))
        if len(desc) >= 25 and "your description here" not in desc.lower():
            return desc, "meta_description"
    for m in META_DESC_RE2.finditer(html):
        desc = clean_text(m.group(1))
        if len(desc) >= 25 and "your description here" not in desc.lower():
            return desc, "meta_description"
    # JSON-LD description (Shopify Organization / WebSite schema)
    for data in parse_json_ld(html):
        desc = data.get("description")
        if isinstance(desc, str):
            desc = clean_text(desc)
            if len(desc) >= 25:
                return desc, "json_ld"
    # hero paragraph fallback
    hero = re.search(
        r"<(?:h1|h2|p)[^>]*>([^<]{60,200})</", html, re.I)
    if hero:
        desc = clean_text(hero.group(1))
        if not re.match(r"^(javascript|var |function)", desc.lower()):
            return desc, "hero_text"
    return None, ""


def _absolutize(url: str, base: str) -> str | None:
    if not url or url.startswith(("data:", "javascript:", "#")):
        return None
    # JSON-LD values often contain escaped slashes and unicode escapes;
    # HTML attributes may carry &amp; - normalize before joining.
    url = url.replace("\\/", "/").replace("\\u0026", "&")
    url = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), url)
    url = html_mod.unescape(url)
    if url.startswith(("data:", "javascript:", "#")):
        return None
    if url.startswith("//"):
        url = "https:" + url
    try:
        return urljoin(base, url)
    except ValueError:
        return None


def extract_logo(html: str, base_url: str) -> tuple[str | None, str]:
    """Return (logo_url, method). Favicons explicitly excluded."""
    # 1. JSON-LD Organization.logo (used by most themes)
    for m in LOGO_JSONLD_RE.finditer(html):
        url = _absolutize(m.group(1), base_url)
        if url and not FAVICON_HINT_RE.search(url):
            return url, "json_ld"

    # 2. header <img> scored by logo-ness
    header_html = ""
    hm = HEADER_BLOCK_RE.search(html)
    if hm:
        header_html = hm.group(0)
    scan_scope = header_html + html[:60000]

    best, best_score = None, 0
    for img in IMG_RE.finditer(scan_scope):
        tag = img.group(0)
        srcs = SRC_RE.findall(tag)
        if not srcs:
            continue
        src = srcs[0].split()[0]
        url = _absolutize(src, base_url)
        if not url:
            continue
        low = url.lower()
        if FAVICON_HINT_RE.search(low):
            continue
        if PAYMENT_LOGO_RE.search(low):
            continue
        alt = (ALT_RE.search(tag) or [None, ""])[1].lower()
        cls = (CLASS_RE.search(tag) or [None, ""])[1].lower()
        ident = (ID_RE.search(tag) or [None, ""])[1].lower()
        blob = f"{low} {alt} {cls} {ident}"
        score = 0
        if "logo" in blob:
            score += 5
        if "brand" in blob or "site-header" in blob or "header__heading" in blob:
            score += 2
        if "shopify" in low and ("shop/files" in low or "/files/" in low):
            score += 1
        if "sprite" in low or "payment" in low:
            score = 0
        if score > best_score:
            best, best_score = url, score
    if best:
        return best, "img_heuristic"

    # 3. og:image fallback (usually brand-accurate on Shopify, flagged as such)
    og = re.search(r'property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html, re.I)
    og2 = re.search(r'content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.I)
    url = _absolutize(og.group(1) if og else (og2.group(1) if og2 else None), base_url)
    if url and not FAVICON_HINT_RE.search(url.lower()):
        return url, "og_image_fallback"
    return None, ""


def _collect_extra_page_urls(html: str, base: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'href=["\']([^"\']+)', html, re.I):
        href = m.group(1)
        low = href.lower()
        if not any(h in low for h in config.EXTRA_PAGE_HINTS):
            continue
        if low.startswith(("mailto:", "tel:", "javascript:")):
            continue
        url = _absolutize(href, base)
        if not url:
            continue
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        if host != urlsplit(base).netloc.lower().removeprefix("www."):
            continue  # stay on-domain
        if url.rstrip("/") in seen:
            continue
        seen.add(url.rstrip("/"))
        urls.append(url)
    return urls[: config.MAX_EXTRA_PAGES]


async def enrich_store(store, fetcher, rec: dict) -> dict:
    host = rec["host"]
    canon = rec.get("canonical_host") or host
    base_url = rec.get("homepage_url") or f"https://{canon}/"
    html = ""
    home = await fetcher.get(base_url, check_robots=robots_cache)
    if home is not None and home.ok:
        html = home.body

    pages: list[tuple[str, str]] = [(base_url, html)]

    # products.json for category (robots-allowed)
    products = []
    reply = await fetcher.get(
        f"https://{canon}/products.json?limit={config.PRODUCTS_JSON_LIMIT}",
        check_robots=robots_cache)
    if reply is not None and reply.ok:
        try:
            data = json.loads(reply.body)
            products = data.get("products") or []
        except (json.JSONDecodeError, AttributeError):
            products = []
    rec["products_sampled"] = len(products)

    # extra pages (contact/about/...)
    extra_urls = _collect_extra_page_urls(html, base_url) if html else []
    for url in extra_urls:
        reply = await fetcher.get(url, check_robots=robots_cache)
        if reply is not None and reply.ok and reply.body:
            pages.append((url, reply.body))

    # ---- contacts -----------------------------------------------------------
    emails: list[str] = []
    phones: list[str] = []
    all_pages_text = ""
    for _, body in pages:
        body = body[:600000]  # bound regex work on huge generated pages
        all_pages_text += body + "\n"
        for e in extract_emails(body):
            if e not in emails:
                emails.append(e)
        for p in extract_phones(body):
            if p not in phones:
                phones.append(p)
    rec["emails"] = emails
    rec["phones"] = phones

    # ---- socials ------------------------------------------------------------
    socials: dict[str, str] = {}
    for _, body in pages:
        for platform, url in extract_socials(body[:600000]).items():
            socials.setdefault(platform, url)
    rec["socials"] = socials

    # ---- category -----------------------------------------------------------
    category, cat_method = classify(products, html)
    rec["category"] = category
    rec["category_method"] = cat_method or ""

    # ---- tagline ------------------------------------------------------------
    tagline, tag_method = extract_tagline(html, rec.get("shop_name", ""))
    if not tagline and rec.get("meta_city") is None:
        for _, body in pages[1:3]:
            tagline, tag_method = extract_tagline(body, rec.get("shop_name", ""))
            if tagline:
                break
    rec["tagline"] = tagline
    rec["tagline_method"] = tag_method

    # ---- logo ---------------------------------------------------------------
    logo, logo_method = extract_logo(html, base_url)
    rec["logo_url"] = logo
    rec["logo_method"] = logo_method

    # ---- state --------------------------------------------------------------
    state, state_method = None, ""
    prov = normalize_province(rec.get("meta_province"))
    if prov:
        state, state_method = prov, "meta.json"
    else:
        # merge of extra pages + homepage text
        text_blob = re.sub(r"<[^>]+>", " ", all_pages_text[:600000])
        state = state_from_text(text_blob)
        if state:
            state_method = "page_text"
        else:
            state = state_from_city(text_blob)
            if state:
                state_method = "city_name"
    rec["state"] = state
    rec["state_method"] = state_method

    rec["has_gstin"] = bool(GSTIN_RE.search(all_pages_text.upper()))
    rec["pages_crawled"] = len(pages)

    # completeness over the 7 required fields (contacts counts if either present)
    complete = sum(bool(x) for x in (
        True, bool(emails or phones), bool(socials), bool(category),
        bool(tagline), bool(logo), bool(state)))
    rec["fields_complete"] = complete

    return rec


async def run(store, fetcher, progress_every: int = 50) -> None:
    # enrichment always re-runs over every verified store: responses come
    # from the disk cache, so a full pass costs no new network traffic
    targets = [r for r in store.records.values()
               if r.get("is_shopify") and r.get("is_indian")]
    log.info("enriching %d stores", len(targets))
    sem = asyncio.Semaphore(config.GLOBAL_CONCURRENCY)
    done = 0

    async def one(rec):
        nonlocal done
        async with sem:
            try:
                await enrich_store(store, fetcher, rec)
                if not rec.get("discarded"):
                    store.save_record(rec)
            except Exception as exc:  # noqa: BLE001
                log.debug("enrich failed for %s: %s", rec.get("host"), exc)
        done += 1
        if done % progress_every == 0:
            log.info("enrich progress: %d/%d", done, len(targets))

    await asyncio.gather(*(one(r) for r in targets))
