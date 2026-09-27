"""Vercel Python serverless function: read-only JSON API over the result set.

Routes:
  /api/stats   -> totals, completeness, distributions, facets
  /api/stores  -> filtered / sorted / paginated rows (+ &format=csv export)
  /api/query   -> deterministic natural-language query interpreter

The query interpreter is intentionally NOT an LLM: it parses the text into
explicit filters, returns the parsed interpretation alongside the results,
and never fabricates data. Deterministic software owns truth.
"""

from __future__ import annotations

import csv
import io
import json
import re
import urllib.parse
from collections import Counter
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent.parent / "output" / "indian_shopify_stores.csv"
REPORT_FILE = Path(__file__).resolve().parent.parent / "output" / "report.json"

_rows: list[dict] | None = None
_states: list[str] | None = None
_categories: list[str] | None = None

# --------------------------------------------------------------------------
# Category grouping: the pipeline's raw labels roll up into a small
# hierarchy so the UI can filter at either level.
# --------------------------------------------------------------------------
CATEGORY_GROUPS: dict[str, list[str]] = {
    "Apparel & Fashion": [
        "Men's Apparel", "Women's Apparel", "Sarees & Ethnic Wear",
        "Kids & Baby", "Footwear", "Fitness & Sports",
    ],
    "Beauty & Personal Care": [
        "Skincare", "Hair Care", "Beauty & Cosmetics", "Bath & Body",
        "Fragrance",
    ],
    "Food & Beverage": ["Food & Beverage", "Grocery & Staples"],
    "Health & Wellness": ["Ayurveda & Wellness"],
    "Home & Living": ["Home & Living"],
    "Jewellery & Accessories": ["Jewellery", "Bags & Luggage"],
    "Electronics": ["Electronics & Gadgets"],
    "Pets": ["Pets"],
    "Stationery & Toys": ["Arts, Crafts & Stationery", "Toys & Games"],
}

CATEGORY_TO_GROUP: dict[str, str] = {
    cat: group for group, cats in CATEGORY_GROUPS.items() for cat in cats
}

GROUP_ORDER = list(CATEGORY_GROUPS.keys()) + ["Other"]

INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
    "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim",
    "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand",
    "West Bengal", "Delhi", "Jammu and Kashmir", "Ladakh", "Chandigarh",
    "Puducherry", "Lakshadweep",
]

# category keywords for the query interpreter (substring -> category label)
CATEGORY_HINTS: list[tuple[str, str]] = [
    ("saree", "Sarees & Ethnic Wear"), ("ethnic", "Sarees & Ethnic Wear"),
    ("kurta", "Sarees & Ethnic Wear"), ("lehenga", "Sarees & Ethnic Wear"),
    ("skincare", "Skincare"), ("skin", "Skincare"), ("serum", "Skincare"),
    ("cosmetic", "Beauty & Cosmetics"), ("makeup", "Beauty & Cosmetics"),
    ("lipstick", "Beauty & Cosmetics"),
    ("hair", "Hair Care"), ("shampoo", "Hair Care"),
    ("perfume", "Fragrance"), ("fragrance", "Fragrance"),
    ("soap", "Bath & Body"), ("bath", "Bath & Body"),
    ("ayurveda", "Ayurveda & Wellness"), ("wellness", "Ayurveda & Wellness"),
    ("supplement", "Ayurveda & Wellness"), ("health", "Ayurveda & Wellness"),
    ("coffee", "Food & Beverage"), ("tea", "Food & Beverage"),
    ("snack", "Food & Beverage"), ("food", "Food & Beverage"),
    ("spice", "Food & Beverage"), ("chocolate", "Food & Beverage"),
    ("jewel", "Jewellery"),
    ("home decor", "Home & Living"), ("decor", "Home & Living"),
    ("furniture", "Home & Living"), ("kitchen", "Home & Living"),
    ("mattress", "Home & Living"),
    ("electronics", "Electronics & Gadgets"), ("audio", "Electronics & Gadgets"),
    ("earbud", "Electronics & Gadgets"), ("smartwatch", "Electronics & Gadgets"),
    ("gadget", "Electronics & Gadgets"),
    ("luggage", "Bags & Luggage"), ("wallet", "Bags & Luggage"),
    ("backpack", "Bags & Luggage"),
    ("pet", "Pets"), ("dog", "Pets"),
    ("toys", "Toys & Games"), ("games", "Toys & Games"),
    ("stationery", "Arts, Crafts & Stationery"), ("craft", "Arts, Crafts & Stationery"),
    ("kids", "Kids & Baby"), ("baby", "Kids & Baby"),
    ("shoe", "Footwear"), ("footwear", "Footwear"),
]

HAS_FILTERS = {"email": "emails", "phone": "phones", "instagram": "instagram",
               "social": "instagram", "logo": "logo_url", "whatsapp": "whatsapp"}


def _load_rows() -> list[dict]:
    global _rows
    if _rows is None:
        with DATA_FILE.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for i, r in enumerate(rows, 1):
            r["_index"] = i
            r["_emails"] = [e.strip() for e in (r.get("emails") or "").split(";") if e.strip()]
            r["_phones"] = [p.strip() for p in (r.get("phones") or "").split(";") if p.strip()]
            r["_socials"] = {k: r[k] for k in
                             ("instagram", "facebook", "twitter", "linkedin",
                              "youtube", "pinterest", "whatsapp", "telegram")
                             if r.get(k)}
            r["_group"] = CATEGORY_TO_GROUP.get(r.get("category", ""), "Other")
            r["_completeness"] = sum(bool(x) for x in (
                True, r["_emails"] or r["_phones"], r["_socials"],
                r.get("category"), r.get("tagline"), r.get("logo_url"),
                r.get("state")))
        _rows = rows
    return _rows


def _facets() -> tuple[list[str], list[str]]:
    global _states, _categories
    if _states is None:
        _states = sorted({r["state"] for r in _load_rows() if r.get("state")})
        _categories = sorted({r["category"] for r in _load_rows() if r.get("category")})
    return _states, _categories


def _int(params: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(params.get(key, default))))
    except (TypeError, ValueError):
        return default


def _searchable(row: dict) -> str:
    return " ".join(
        (row.get(k) or "") for k in
        ("domain_url", "store_name", "category", "tagline", "emails",
         "phones", "state", "myshopify_domain")
    ).lower()


def _matches(row: dict, q: str, state: str, category: str, group: str,
             has: list[str]) -> bool:
    if state and row.get("state") != state:
        return False
    if category and row.get("category") != category:
        return False
    if group and row.get("_group") != group:
        return False
    for h in has:
        key = HAS_FILTERS.get(h)
        if key and not row.get(key):
            return False
    if q and q not in _searchable(row):
        return False
    return True


def _sort_key(sort: str):
    if sort == "name":
        return lambda r: (r.get("store_name") or r.get("domain_url", "")).lower()
    if sort == "state":
        return lambda r: (r.get("state") or "zz", (r.get("store_name") or "").lower())
    if sort == "category":
        return lambda r: (r.get("category") or "zz", (r.get("store_name") or "").lower())
    if sort == "contacts":
        return lambda r: (-len(r["_emails"]) - len(r["_phones"]),
                          (r.get("store_name") or "").lower())
    if sort == "completeness":
        return lambda r: (-r["_completeness"], (r.get("store_name") or "").lower())
    return lambda r: r["_index"]  # dataset order = default


def _facet_counts(rows: list[dict]) -> dict:
    return {
        "states": dict(Counter(r["state"] for r in rows if r["state"]).most_common()),
        "categories": dict(Counter(
            r["category"] for r in rows if r["category"]).most_common()),
        "groups": {g: sum(1 for r in rows if r["_group"] == g) for g in GROUP_ORDER},
    }


def _slim(row: dict) -> dict:
    return {
        "index": row["_index"],
        "domain_url": row.get("domain_url", ""),
        "store_name": row.get("store_name", ""),
        "category": row.get("category", ""),
        "group": row["_group"],
        "state": row.get("state", ""),
        "tagline": row.get("tagline", ""),
        "emails": row["_emails"],
        "phones": row["_phones"],
        "socials": row["_socials"],
        "logo_url": row.get("logo_url", ""),
        "completeness": row["_completeness"],
        "sources": row.get("sources", ""),
        "myshopify_domain": row.get("myshopify_domain", ""),
    }


CSV_FIELDS = ["index", "domain_url", "store_name", "category", "category_group",
              "state", "tagline", "emails", "phones", "instagram", "facebook",
              "twitter", "linkedin", "youtube", "logo_url", "myshopify_domain"]


def handle_stats() -> dict:
    report = {}
    if REPORT_FILE.exists():
        report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    states, categories = _facets()
    rows = _load_rows()
    return {
        "total": report.get("total_stores", len(rows)),
        "states": states,
        "categories": categories,
        "groups": GROUP_ORDER,
        "field_completeness": report.get("field_completeness", {}),
        "state_distribution": report.get("state_distribution", {}),
        "category_distribution": report.get("category_distribution", {}),
        "group_distribution": {g: sum(1 for r in rows if r["_group"] == g)
                               for g in GROUP_ORDER},
    }


def handle_stores(params: dict) -> tuple[dict | None, str | None]:
    rows = _load_rows()
    q = (params.get("q") or "").strip().lower()
    state = params.get("state") or ""
    category = params.get("category") or ""
    group = params.get("group") or ""
    has = [h for h in (params.get("has") or "").split(",") if h in HAS_FILTERS]
    sort = params.get("sort") or "index"

    rows = [r for r in rows if _matches(r, q, state, category, group, has)]
    rows.sort(key=_sort_key(sort))

    if params.get("format") == "csv":
        return None, _to_csv(rows)

    per_page = _int(params, "per_page", 50, 1, 200)
    page = _int(params, "page", 1, 1, max(1, -(-len(rows) // per_page)))
    start = (page - 1) * per_page

    return {
        "total": len(rows),
        "page": page,
        "per_page": per_page,
        "pages": max(1, -(-len(rows) // per_page)),
        "rows": [_slim(r) for r in rows[start: start + per_page]],
        "facets": _facet_counts(rows) if params.get("facets") else None,
    }, None


def _to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_FIELDS)
    for r in rows:
        writer.writerow([
            r["_index"], r.get("domain_url", ""), r.get("store_name", ""),
            r.get("category", ""), r["_group"], r.get("state", ""),
            r.get("tagline", ""), "; ".join(r["_emails"]), "; ".join(r["_phones"]),
            r.get("instagram", ""), r.get("facebook", ""), r.get("twitter", ""),
            r.get("linkedin", ""), r.get("youtube", ""), r.get("logo_url", ""),
            r.get("myshopify_domain", ""),
        ])
    return buf.getvalue()


# --------------------------------------------------------------------------
# Deterministic natural-language query interpretation
# --------------------------------------------------------------------------

def interpret_query(text: str) -> dict:
    """Parse free text into explicit filters. The interpretation is returned
    so the UI can show exactly what was understood - no black box."""
    t = f" {text.lower().strip()} "
    parsed: dict = {"q": "", "state": "", "category": "", "has": [], "limit": 0}

    limit = re.search(r"\b(?:top|first|show)\s+(\d{1,4})\b", t)
    if limit:
        parsed["limit"] = min(int(limit.group(1)), 200)
        t = t.replace(limit.group(0), " ")

    for st in sorted(INDIAN_STATES, key=len, reverse=True):
        if f" {st.lower()} " in t or f" {st.lower()}," in t or f"in {st.lower()}" in t:
            parsed["state"] = st
            t = t.replace(st.lower(), " ")
            break

    for hint, cat in CATEGORY_HINTS:
        # remove whole words containing the hint so "jewellery" does not
        # leave the fragment "lery" behind as a text query
        pattern = re.compile(rf"\b\w*{re.escape(hint)}\w*\b", re.I)
        if pattern.search(t):
            parsed["category"] = cat
            t = pattern.sub(" ", t)
            break

    for word, has in [("email", "email"), ("mail", "email"), ("phone", "phone"),
                      ("contact", "phone"), ("instagram", "instagram"),
                      ("insta", "instagram"), ("logo", "logo"),
                      ("whatsapp", "whatsapp"), ("social", "instagram")]:
        if re.search(rf"\b{word}\b", t) and has not in parsed["has"]:
            parsed["has"].append(has)
            t = re.sub(rf"\b{word}\b", " ", t)

    leftovers = re.sub(
        r"\b(?:stores?|shops?|brands?|list|of|the|in|with|having|that|have|"
        r"and|for|from|sell|selling|show|me|all|find|give|a|an)\b", " ", t)
    parsed["q"] = re.sub(r"\s+", " ", leftovers).strip()
    return parsed


def handle_query(params: dict) -> dict:
    text = (params.get("text") or "").strip()
    parsed = interpret_query(text)
    rows = _load_rows()
    matched = [r for r in rows if _matches(
        r, parsed["q"], parsed["state"], parsed["category"], "", parsed["has"])]
    matched.sort(key=_sort_key("index"))

    chips = []
    if parsed["state"]:
        chips.append(["state", parsed["state"]])
    if parsed["category"]:
        chips.append(["category", parsed["category"]])
    for h in parsed["has"]:
        chips.append(["has", h])
    if parsed["q"]:
        chips.append(["text", parsed["q"]])

    limit = parsed["limit"] or 50
    return {
        "interpretation": chips,
        "total": len(matched),
        "limit": limit,
        "rows": [_slim(r) for r in matched[:limit]],
    }


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    path = scope["path"]
    query = scope.get("query_string", b"").decode()
    params = {k: v[0] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}

    try:
        if path == "/api/stats":
            await _json(send, 200, handle_stats())
        elif path == "/api/stores":
            body, csv_out = handle_stores(params)
            if csv_out is not None:
                payload = csv_out.encode("utf-8-sig")
                await send({"type": "http.response.start", "status": 200, "headers": [
                    (b"content-type", b"text/csv; charset=utf-8"),
                    (b"content-disposition",
                     b'attachment; filename="indian_shopify_stores_filtered.csv"'),
                    (b"access-control-allow-origin", b"*")]})
                await send({"type": "http.response.body", "body": payload})
            else:
                await _json(send, 200, body)
        elif path == "/api/query":
            await _json(send, 200, handle_query(params))
        else:
            await _json(send, 404, {"error": "not found"})
    except Exception as exc:  # noqa: BLE001
        await _json(send, 500, {"error": f"{type(exc).__name__}: {exc}"})


async def _json(send, status: int, body: dict) -> None:
    payload = json.dumps(body, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"access-control-allow-origin", b"*"),
        (b"cache-control", b"public, max-age=120")]})
    await send({"type": "http.response.body", "body": payload})
