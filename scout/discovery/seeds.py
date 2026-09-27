"""Curated seeds + public listicle scraping.

The seed file is a hand-compiled list of well-known Indian consumer brands.
Listicles ("top Shopify stores in India" style articles) are scraped for
outbound hyperlinks, which resolve brand names to real domains without
guessing. Both are *leads* - verification decides everything downstream.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

import config
from scout.robots import normalize_host

log = logging.getLogger("seeds")

# Public articles that link to Indian Shopify storefronts.
LISTICLE_URLS = [
    "https://www.skailama.com/shopify-stores/india",
    "https://magenest.com/en/shopify-stores-in-india/",
    "https://www.shopify.com/blog/shopify-stores",
    "https://qikink.com/blog/top-d2c-brands-in-india/",
]

NOISE = (
    "shopify", "facebook", "instagram", "twitter", "x.com", "linkedin",
    "youtube", "pinterest", "whatsapp", "google", "gstatic", "cloudflare",
    "amazon", "flipkart", "myntra", "ajio", "nykaa", "paytm", "phonepe",
    "razorpay", "gov.in", "nic.in", "wikipedia", "medium.com", "reddit",
    "quora", "linkedin", "apple.com", "microsoft", "klaviyo", "judge.me",
    "yotpo", "trustpilot", "mailchimp", "youtube", "skailama", "qikink",
    "storeleads", "builtwith", "wordpress", "blogspot", "sentry", "webflow",
    "wix", "wordpress.com", "godaddy", "shopify.dev", "shopify.com",
)

DOMAIN_RE = re.compile(r"href=[\"'](https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})[/\"'?#\s>]", re.I)


def load_seed_domains() -> list[str]:
    domains = set()
    path = config.SEEDS / "indian_brands.txt"
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip().lower()
            if not line or line.startswith("#"):
                continue
            for pre in ("https://", "http://"):
                if line.startswith(pre):
                    line = line[len(pre):]
            line = normalize_host(line.split("/")[0])
            if "." in line:
                domains.add(line)
    return sorted(domains)


def _link_host(href: str) -> str | None:
    m = re.match(r"https?://([^/\"'\s>]+)", href)
    if not m:
        return None
    host = normalize_host(m.group(1))
    if not host or "." not in host:
        return None
    if host.endswith((".png", ".jpg", ".svg", ".webp")):
        return None
    for noise in NOISE:
        if noise in host:
            return None
    # drop asset/CDN style hosts
    first = host.split(".")[0]
    if first in ("cdn", "static", "assets", "img", "images", "media", "js", "css"):
        return None
    return host


async def discover_from_listicles(store, fetcher) -> int:
    hosts: set[str] = set()
    for url in LISTICLE_URLS:
        reply = await fetcher.get(url, use_cache=True)
        if reply is None or not reply.ok:
            log.warning("listicle unreachable: %s", url)
            continue
        for m in re.finditer(r'href=["\']([^"\']+)', reply.body):
            host = _link_host(m.group(1))
            if host:
                hosts.add(host)
    added = store.add_candidates(sorted(hosts), "listicles")
    log.info("listicles: %d linked hosts, %d new", len(hosts), added)
    return added
