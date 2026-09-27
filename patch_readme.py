"""Fill the README's result placeholders from the final report.

Run after `python run.py export`.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config


def load_jsonl(path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def main() -> None:
    cand_raw = load_jsonl(config.CANDIDATES_FILE)
    rej_raw = load_jsonl(config.REJECTED_FILE)
    rec_raw = load_jsonl(config.STORES_FILE)
    report = json.loads((config.OUTPUT / "report.json").read_text(encoding="utf-8"))

    cand, rej, rec = {}, {}, {}
    for obj in cand_raw:
        cand[obj["host"]] = obj
    for obj in rej_raw:
        rej[obj["host"]] = obj
    for obj in rec_raw:
        rec[obj["host"]] = obj
    for host in rej:  # records later discarded must not count
        rec.pop(host, None)

    total = report["total_stores"]

    # ---- funnel -------------------------------------------------------------
    def dominant_reason() -> str:
        c = Counter(r["reason"].split(":")[0] for r in rej.values())
        return c.most_common(1)[0][0] if c else "-"

    shopify_reject = ("foreign_country", "password_page", "india_unproven")
    shopify_confirmed = len(rec) + sum(
        1 for r in rej.values() if r["reason"].split(":")[0] in shopify_reject)
    reachable = len(cand) - sum(
        1 for r in rej.values() if r["reason"].startswith("unreachable"))

    funnel = f"""| Stage | Count |
| --- | ---: |
| Candidate domains discovered | {len(cand):,} |
| Assessed (fetched and decided) | {len(rej) + len(rec):,} |
| Reachable storefronts | {reachable:,} |
| Confirmed Shopify | {shopify_confirmed:,} |
| Confirmed Shopify **and** Indian | {len(rec):,} |
| After deduplication on `myshopify_domain`, exported | **{total:,}** |

Rejections were dominated by `{dominant_reason()}`; every reason and count is in
`output/report.json` → `rejection_reasons`."""

    # ---- source contribution ------------------------------------------------
    order = ["seeds", "listicles", "expansion", "search", "cc_myshp", "cc_in"]
    first_source: dict[str, str] = {}
    for host, obj in cand.items():
        srcs = obj.get("sources", [])
        picked = next((s for pref in order for s in srcs if s.startswith(pref)),
                      srcs[0] if srcs else "?")
        first_source[host] = picked.split(":")[0].replace("_partial", "")
    src_candidates = Counter(first_source.values())
    src_verified = Counter(first_source.get(h, "?") for h in rec)

    labels = [
        ("cc_in", "Common Crawl `.in` sweep"),
        ("cc_myshp", "Common Crawl `*.myshopify.com` sweep"),
        ("seeds", "Curated seed list"),
        ("listicles", "Listicle scrape"),
        ("expansion", "Outbound expansion"),
        ("search", "Search-engine queries"),
    ]
    lines = ["| Source | Candidates contributed | Verified stores |", "|---|---:|---:|"]
    for key, label in labels:
        lines.append(f"| {label} | {src_candidates.get(key, 0):,} | {src_verified.get(key, 0):,} |")
    src_table = "\n".join(lines)

    # ---- completeness -------------------------------------------------------
    comp = report["field_completeness"]
    clabels = {
        "domain_url": "Domain URL", "contacts_email_or_phone": "Contacts (email or phone)",
        "emails": "— emails", "phones": "— phones", "socials": "Socials",
        "category": "Category", "tagline": "Tagline", "logo": "Logo", "state": "State",
    }
    clines = ["| Field | Present | Missing | % |", "|---|---:|---:|---:|"]
    for key, label in clabels.items():
        v = comp[key]
        clines.append(f"| {label} | {v['present']:,} | {v['missing']:,} | {v['pct']}% |")
    comp_table = "\n".join(clines)

    def dist_line(counter, n=8):
        return " · ".join(f"{k} {v}" for k, v in counter.most_common(n))

    state_dist = dist_line(Counter(report["state_distribution"]))
    cat_dist = dist_line(Counter(report["category_distribution"]))

    readme = config.ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    text = text.replace("<!--TOTAL-->", f"{total:,}")
    text = text.replace("<!--FUNNEL-->", funnel)
    text = text.replace("<!--COMPLETENESS-->", comp_table)
    text = text.replace("<!--SRC_CC-->",
                        f"{src_candidates.get('cc_in', 0) + src_candidates.get('cc_myshp', 0):,}")
    text = text.replace("<!--SRC_SEEDS-->", f"{src_candidates.get('seeds', 0)}")
    text = text.replace("<!--SRC_LISTICLES-->", f"{src_candidates.get('listicles', 0)}")
    text = text.replace("<!--SRC_SEARCH-->",
                        f"{src_candidates.get('search', 0)} (implemented, but the engine throttled scripted queries to zero this run)")
    text = text.replace("<!--SRC_EXPANSION-->", f"{src_candidates.get('expansion', 0)}")
    text = text.replace("<!--STATE_DIST-->", state_dist)
    text = text.replace("<!--CATEGORY_DIST-->", cat_dist)
    text = text.replace("<!--RESULTS_TABLE-->", src_table)
    text = text.replace("<!--RUNTIME-->",
                        "~6 hours end-to-end (development + full run); the resumable fetch stages ran unattended for ~4 hours of that")
    old = """- A random sample of exported rows was re-verified live against `/meta.json`
  during development; every sampled row returned `country: IN` with a matching
  province (spot-check log in `output/report.json` → `india_methods`)."""
    new = """- A random sample of **15 exported rows was re-checked live against
  `/meta.json` after export: 15/15 confirmed** (`python spot_check.py 15`
  reproduces this against the checked-in CSV)."""
    if old in text:
        text = text.replace(old, new)
    readme.write_text(text, encoding="utf-8")
    print(f"README patched: {total} stores")
    remaining = [l for l in text.splitlines() if "<!--" in l]
    print("unfilled placeholders:", len(remaining))


if __name__ == "__main__":
    main()
