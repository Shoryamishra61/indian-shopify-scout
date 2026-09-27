"""Vercel Python serverless function: read-only JSON API over the result set.

Routes:
  /api/stats   -> totals, field completeness, state/category distributions
  /api/stores  -> filtered + paginated rows
                 (?q= &state= &category= &page= &per_page= &has=)

The CSV shipped in the repo is the single source of truth; the function is
stateless and loads it on cold start (about 2 MB, ~2.4k rows).
"""

from __future__ import annotations

import csv
import json
import urllib.parse
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent.parent / "output" / "indian_shopify_stores.csv"
REPORT_FILE = Path(__file__).resolve().parent.parent / "output" / "report.json"

_rows: list[dict] | None = None
_states: list[str] | None = None
_categories: list[str] | None = None


def _load_rows() -> list[dict]:
    global _rows
    if _rows is None:
        with DATA_FILE.open(encoding="utf-8-sig", newline="") as fh:
            _rows = list(csv.DictReader(fh))
    return _rows


def _facets() -> tuple[list[str], list[str]]:
    global _states, _categories
    if _states is None:
        states = sorted({r["state"] for r in _load_rows() if r.get("state")})
        cats = sorted({r["category"] for r in _load_rows() if r.get("category")})
        _states, _categories = states, cats
    return _states, _categories


def _searchable(row: dict) -> str:
    return " ".join(
        (row.get(k) or "") for k in
        ("domain_url", "store_name", "category", "tagline", "emails",
         "phones", "state", "myshopify_domain")
    ).lower()


def _int(params: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(params.get(key, default))))
    except (TypeError, ValueError):
        return default


def handle_stats() -> dict:
    report = {}
    if REPORT_FILE.exists():
        report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    states, categories = _facets()
    return {
        "total": report.get("total_stores", len(_load_rows())),
        "states": states,
        "categories": categories,
        "field_completeness": report.get("field_completeness", {}),
        "state_distribution": report.get("state_distribution", {}),
        "category_distribution": report.get("category_distribution", {}),
    }


def handle_stores(params: dict) -> dict:
    rows = _load_rows()
    q = (params.get("q") or "").strip().lower()
    state = params.get("state") or ""
    category = params.get("category") or ""
    has = params.get("has") or ""  # email|phone|social|logo

    if state:
        rows = [r for r in rows if r.get("state") == state]
    if category:
        rows = [r for r in rows if r.get("category") == category]
    if has:
        key = {"email": "emails", "phone": "phones", "social": "instagram",
               "logo": "logo_url"}.get(has, has)
        rows = [r for r in rows if r.get(key)]
    if q:
        rows = [r for r in rows if q in _searchable(r)]

    page = _int(params, "page", 1, 1, 10_000)
    per_page = _int(params, "per_page", 50, 1, 200)
    start = (page - 1) * per_page
    slice_ = rows[start: start + per_page]

    slim = []
    for r in slice_:
        slim.append({
            "domain_url": r.get("domain_url", ""),
            "store_name": r.get("store_name", ""),
            "category": r.get("category", ""),
            "state": r.get("state", ""),
            "tagline": r.get("tagline", ""),
            "emails": r.get("emails", ""),
            "phones": r.get("phones", ""),
            "logo_url": r.get("logo_url", ""),
            "instagram": r.get("instagram", ""),
            "facebook": r.get("facebook", ""),
            "twitter": r.get("twitter", ""),
            "linkedin": r.get("linkedin", ""),
            "youtube": r.get("youtube", ""),
        })

    return {
        "total": len(rows),
        "page": page,
        "per_page": per_page,
        "pages": max(1, -(-len(rows) // per_page)),
        "rows": slim,
    }


async def app(scope, receive, send):
    """Minimal ASGI app (no framework dependency)."""
    if scope["type"] != "http":
        return
    path = scope["path"]
    query = scope.get("query_string", b"").decode()
    params = {k: v[0] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}

    if path in ("/api/stats", "/api/stores"):
        try:
            body = handle_stats() if path == "/api/stats" else handle_stores(params)
            payload = json.dumps(body, ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"access-control-allow-origin", b"*"),
                                    (b"cache-control", b"public, max-age=300")]})
            await send({"type": "http.response.body", "body": payload})
        except Exception as exc:  # noqa: BLE001
            payload = json.dumps({"error": str(exc)}).encode()
            await send({"type": "http.response.start", "status": 500,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": payload})
    else:
        await send({"type": "http.response.start", "status": 404,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"error":"not found"}'})
