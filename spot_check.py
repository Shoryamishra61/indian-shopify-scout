"""Post-export quality gate: re-check a random sample of exported rows live.

Usage: python spot_check.py [sample_size]

Re-fetches /meta.json for N random rows from the exported CSV and confirms:
  1. the storefront is still live Shopify
  2. the merchant-reported country is IN
  3. the exported state matches the reported province
Prints a pass/fail table for the README.
"""

from __future__ import annotations

import csv
import json
import random
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from scout.states import normalize_province


def main(n: int = 15) -> None:
    with config.CSV_FILE.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    sample = random.sample(rows, min(n, len(rows)))

    passed, failed = 0, []
    for row in sample:
        domain = row["domain_url"]
        try:
            req = urllib.request.Request(
                f"{domain}/meta.json",
                headers={"User-Agent": config.USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                meta = json.loads(resp.read())
            country = (meta.get("country") or "").upper()
            state_match = normalize_province(meta.get("province")) == row["state"]
            if country == "IN" and state_match:
                passed += 1
            else:
                failed.append((domain, country, meta.get("province"), row["state"]))
        except Exception as exc:  # noqa: BLE001
            failed.append((domain, f"error:{type(exc).__name__}", "", row["state"]))

    print(f"spot-check: {passed}/{len(sample)} passed")
    for domain, country, province, exported in failed:
        print(f"  FAIL {domain}: country={country} province={province!r} exported_state={exported!r}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 15)
