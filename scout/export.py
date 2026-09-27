"""Export verified stores to CSV/JSON and compute the completeness report."""

from __future__ import annotations

import csv
import json
from collections import Counter
from urllib.parse import urlsplit

import config

CSV_COLUMNS = [
    "domain_url", "store_name", "emails", "phones", "contacts",
    "instagram", "facebook", "twitter", "linkedin", "youtube", "pinterest",
    "whatsapp", "telegram", "other_socials", "socials_count",
    "category", "tagline", "logo_url", "state",
    "state_method", "category_method", "logo_method", "india_method",
    "shopify_method", "myshopify_domain", "sources", "pages_crawled",
    "products_sampled",
]


def domain_url_of(rec: dict) -> str:
    host = rec.get("canonical_host") or rec["host"]
    return f"https://{host}"


def join(values: list[str]) -> str:
    return "; ".join(v for v in values if v)


def row_of(rec: dict) -> dict:
    emails = rec.get("emails", [])
    phones = rec.get("phones", [])
    contacts = join(emails + phones)
    socials = rec.get("socials", {})
    known = ["instagram", "facebook", "twitter", "linkedin", "youtube",
             "pinterest", "whatsapp", "telegram"]
    other = [u for p, u in socials.items() if p not in known]
    row = {
        "domain_url": domain_url_of(rec),
        "store_name": rec.get("shop_name", ""),
        "emails": join(emails),
        "phones": join(phones),
        "contacts": contacts,
        "instagram": socials.get("instagram", ""),
        "facebook": socials.get("facebook", ""),
        "twitter": socials.get("twitter", ""),
        "linkedin": socials.get("linkedin", ""),
        "youtube": socials.get("youtube", ""),
        "pinterest": socials.get("pinterest", ""),
        "whatsapp": socials.get("whatsapp", ""),
        "telegram": socials.get("telegram", ""),
        "other_socials": join(other),
        "socials_count": len(socials),
        "category": rec.get("category") or "",
        "tagline": rec.get("tagline") or "",
        "logo_url": rec.get("logo_url") or "",
        "state": rec.get("state") or "",
        "state_method": rec.get("state_method", ""),
        "category_method": rec.get("category_method", ""),
        "logo_method": rec.get("logo_method", ""),
        "india_method": rec.get("india_method", ""),
        "shopify_method": rec.get("shopify_method", ""),
        "myshopify_domain": rec.get("myshopify_domain", ""),
        "sources": join(rec.get("sources", [])),
        "pages_crawled": rec.get("pages_crawled", 0),
        "products_sampled": rec.get("products_sampled", 0),
    }
    return row


def export(store) -> dict:
    """Write CSV + JSON, return the report dict."""
    recs = store.dedup_by_shop()
    rows = [row_of(r) for r in recs]
    config.OUTPUT.mkdir(parents=True, exist_ok=True)

    with config.CSV_FILE.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    with config.JSON_FILE.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)

    report = build_report(store, rows)
    with (config.OUTPUT / "report.json").open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    return report


def build_report(store, rows: list[dict]) -> dict:
    def fill(field: str, pred) -> dict:
        n = sum(1 for r in rows if pred(r))
        return {"present": n, "missing": len(rows) - n,
                "pct": round(100 * n / len(rows), 1) if rows else 0}

    report = {
        "total_stores": len(rows),
        "field_completeness": {
            "domain_url": fill("domain_url", lambda r: True),
            "contacts_email_or_phone": fill("contacts", lambda r: bool(r["contacts"])),
            "emails": fill("emails", lambda r: bool(r["emails"])),
            "phones": fill("phones", lambda r: bool(r["phones"])),
            "socials": fill("socials", lambda r: r["socials_count"] > 0),
            "category": fill("category", lambda r: bool(r["category"])),
            "tagline": fill("tagline", lambda r: bool(r["tagline"])),
            "logo": fill("logo", lambda r: bool(r["logo_url"])),
            "state": fill("state", lambda r: bool(r["state"])),
        },
        "state_distribution": dict(Counter(r["state"] for r in rows if r["state"]).most_common()),
        "category_distribution": dict(Counter(r["category"] for r in rows if r["category"]).most_common()),
        "logo_methods": dict(Counter(r["logo_method"] for r in rows).most_common()),
        "state_methods": dict(Counter(r["state_method"] for r in rows).most_common()),
        "category_methods": dict(Counter((r["category_method"] or "").split(":")[0] for r in rows).most_common()),
        "shopify_methods": dict(Counter(r["shopify_method"] for r in rows).most_common()),
        "india_methods": dict(Counter(r["india_method"] for r in rows).most_common()),
        "candidate_sources": dict(Counter(
            src for r in rows for src in r.get("sources", "").split("; ") if src).most_common()),
        "rejection_reasons": dict(Counter(
            r["reason"].split(":")[0] for r in store.rejected.values()).most_common()),
    }
    return report
