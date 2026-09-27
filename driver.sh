#!/usr/bin/env bash
# Endgame driver: verify/expand cycles until the store target is reached,
# then enrichment and export. Safe to re-run; every stage is idempotent.
set -u
cd "$(dirname "$0")"

TARGET=2200
LOG=data/driver.log
count_records() {
  python -c "
import json
seen = set()
for line in open('data/stores.jsonl', encoding='utf-8'):
    try:
        seen.add(json.loads(line)['host'])
    except Exception:
        pass
print(len(seen))
"
}

for cycle in 1 2 3 4 5 6; do
  verified=$(count_records)
  echo "=== cycle $cycle start: $verified verified ===" >> "$LOG"
  [ "$verified" -ge "$TARGET" ] && break

  before_rej=$(grep -c "" data/rejected.jsonl 2>/dev/null || echo 0)
  python run.py verify >> "$LOG" 2>&1
  after_rej=$(grep -c "" data/rejected.jsonl 2>/dev/null || echo 0)
  verified=$(count_records)
  echo "=== cycle $cycle verify done: $verified verified, rejected $before_rej->$after_rej ===" >> "$LOG"
  [ "$verified" -ge "$TARGET" ] && break

  # if verify had nothing new to chew, expand first and re-verify
  if [ "$after_rej" -le "$((before_rej + 5))" ]; then
    python run.py expand >> "$LOG" 2>&1
    python run.py verify >> "$LOG" 2>&1
    verified=$(count_records)
    echo "=== cycle $cycle expand+verify: $verified verified ===" >> "$LOG"
    [ "$verified" -ge "$TARGET" ] && break
    # still stuck -> no more candidates
    if [ "$verified" -le "$verified" ] && [ "$after_rej" -le "$((before_rej + 5))" ]; then
      echo "=== candidate pool exhausted at $verified ===" >> "$LOG"
      break
    fi
  fi

  # periodic expansion between cycles to feed high-prior candidates
  python run.py expand >> "$LOG" 2>&1
done

python run.py enrich >> "$LOG" 2>&1
python run.py export >> "$LOG" 2>&1
echo "=== driver complete: $(count_records) records ===" >> "$LOG"
