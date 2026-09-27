"""Search-engine discovery via DuckDuckGo's lightweight HTML endpoint.

This is the smallest seed source by volume and the most fragile (the
endpoint throttles aggressively), so it runs last, is heavily rate-limited,
and every failure is swallowed: the pipeline never depends on it.

Queries pair India-specific shopping terms with Shopify footprints, either
`site:myshopify.com` (stores that never moved to a custom domain) or the
"Powered by Shopify" footer string (any domain).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import urllib.parse as up

import config

log = logging.getLogger("search")

TERMS = [
    "india", "saree", "kurta", "lehenga", "ethnic wear", "ayurveda", "yoga",
    "chai", "coffee", "spices", "jewellery", "silver jewellery", "sarees online",
    "skincare", "hair oil", "mumbai", "delhi", "jaipur", "surat", "pune",
    "ahmedabad", "kolkata", "chennai", "kerala", "jaipur kurti", "handloom",
    "khadi", "handicraft", "pickles", "dry fruits", "diapers", "toys india",
    "pet food", "grooming", "plus size", "men apparel", "ethnic",
]

QUERY_TEMPLATES = [
    'site:myshopify.com {t}',
    '"powered by shopify" "{t}"',
    '"powered by shopify" india "{t}"',
]

RESULT_RE = re.compile(r'uddg=([^&"\']+)')
HREF_RE = re.compile(r'href=["\']([^"\']+)', re.I)


def _resolve_ddg(href: str) -> str | None:
    if "uddg=" in href:
        target = up.unquote(href.split("uddg=")[1].split("&")[0])
        return target
    if href.startswith("http") and "duckduckgo" not in href:
        return href
    return None


async def discover(store, fetcher) -> int:
    if not config.SEARCH_ENABLED:
        return 0
    queries = []
    for t in TERMS:
        queries.append(QUERY_TEMPLATES[0].format(t=up.quote(t)))
    random.seed(7)
    extra = [QUERY_TEMPLATES[1].format(t=up.quote(t)) for t in TERMS]
    random.shuffle(extra)
    queries += extra[: max(0, config.SEARCH_MAX_QUERIES - len(queries))]
    queries = queries[: config.SEARCH_MAX_QUERIES]

    seen_hosts: set[str] = set()
    ok_queries = 0
    for i, q in enumerate(queries):
        url = f"https://lite.duckduckgo.com/lite/?q={q}"
        try:
            reply = await fetcher.get(url, use_cache=False)
            body = reply.body if reply is not None else ""
            hrefs = HREF_RE.findall(body)
            resolved = [_resolve_ddg(h) for h in hrefs]
            for target in resolved:
                if not target:
                    continue
                try:
                    host = up.urlsplit(target).netloc.lower()
                except ValueError:
                    continue
                host = host.removeprefix("www.")
                if not host or "." not in host:
                    continue
                if any(n in host for n in (
                    "duckduckgo", "google", "bing", "yahoo", "facebook",
                    "instagram", "youtube", "twitter", "x.com", "linkedin",
                    "pinterest", "amazon", "flipkart", "myntra", "reddit",
                    "quora", "medium", "wikipedia", "shopify.com", "tumblr",
                )):
                    continue
                seen_hosts.add(host)
            if reply is not None and reply.ok and hrefs:
                ok_queries += 1
        except Exception as exc:  # noqa: BLE001 - search is best-effort
            log.debug("search query failed: %s", exc)
        await asyncio.sleep(config.SEARCH_DELAY * random.uniform(0.8, 1.3))
        if (i + 1) % 10 == 0:
            log.info("search: %d/%d queries, %d hosts so far", i + 1, len(queries), len(seen_hosts))

    added = store.add_candidates(sorted(seen_hosts), "search")
    log.info("search: %d usable queries, %d hosts, %d new candidates",
             ok_queries, len(seen_hosts), added)
    return added
