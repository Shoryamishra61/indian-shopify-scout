"""Outbound-link expansion from verified stores.

Indian storefronts link to other Indian storefronts (collabs, press, shared
parent brands). After a verify pass, the cached homepages are re-read - zero
new network requests - and outbound hosts that look like independent shops
are proposed as new candidates. Repeat verify->expand cycles until the pool
stops growing.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

import config
from scout.robots import normalize_host

log = logging.getLogger("expansion")

NOISE = (
    "shopify", "facebook", "instagram", "twitter", "x.com", "linkedin",
    "youtube", "pinterest", "whatsapp", "snapchat", "tiktok", "google",
    "gstatic", "cloudflare", "amazon", "flipkart", "myntra", "ajio", "nykaa",
    "meesho", "jiomart", "tatacliq", "paytm", "phonepe",
    "razorpay", "cashfree", "gov.in", "nic.in", "wikipedia",
    "medium.com", "reddit", "quora", "apple.com", "microsoft", "klaviyo",
    "judge.me", "yotpo", "loox", "okendo", "stamped", "trustpilot",
    "mailchimp", "omnisend", "webflow", "wordpress", "blogspot", "wix",
    "godaddy", "sentry", "jsdelivr", "unpkg", "jquery", "fontawesome",
    "hotjar", "clarity.ms", "doubleclick", "googletag", "gtag",
    "t.me", "telegram", "intercom", "zendesk", "freshdesk", "gorgias",
    "tidio", "crisp.chat", "shiprocket", "dtdc", "delhivery",
    "bluedart", "ecomexpress", "xpressbees", "shadowfax",
    "unicommerce", "easyecom", "substack", "vimeo", "spotify",
    "netcore", "webengage", "moengage", "clevertap", "braze",
    "bugsnag", "newrelic", "fullstory", "crazyegg",
)


def _host_of(href: str) -> str | None:
    if href.startswith("//"):
        href = "https:" + href
    if not href.startswith("http"):
        return None
    try:
        host = urlsplit(href).netloc.lower()
    except ValueError:
        return None
    host = normalize_host(host)
    if not host or "." not in host:
        return None
    if host.endswith((".png", ".jpg", ".jpeg", ".svg", ".webp", ".pdf")):
        return None
    labels = host.split(".")
    if labels[0] in ("cdn", "static", "assets", "img", "images", "media", "js", "css",
                     "email", "mail", "docs", "help", "blog", "support", "www2"):
        return None
    for noise in NOISE:
        if noise in host:
            return None
    return host


async def discover(store, fetcher) -> int:
    """Propose outbound hosts from already-cached verified homepages."""
    hosts: set[str] = set()
    known = set(store.records) | set(store.candidates)
    considered = 0
    for rec in list(store.verified()):
        url = rec.get("homepage_url") or f"https://{rec.get('canonical_host') or rec['host']}/"
        cached = fetcher._read_cache(url)
        if cached is None or not cached.ok or not cached.body:
            continue
        considered += 1
        for m in re.finditer(r'href=["\']([^"\']+)', cached.body):
            host = _host_of(m.group(1))
            if host and host not in known:
                hosts.add(host)
    added = store.add_candidates(sorted(hosts), "expansion")
    log.info("expansion: scanned %d cached homepages, %d unseen outbound hosts, %d new",
             considered, len(hosts), added)
    return added
