#!/bin/bash
# Keep running the golden suite until every question is answered.
#
# Groq's free tier allows 100,000 tokens a day and a full run costs roughly
# 94,000, so the run can legitimately hit the ceiling partway through.
# golden_run.py resumes from golden_pairs.json, so each attempt only pays for
# questions that have not been answered yet.
#
#   ./run_until_done.sh
#
# Progress goes to golden_run.log. Stops as soon as the suite is complete, or
# after MAX_ATTEMPTS.

cd "$(dirname "$0")" || exit 1

MAX_ATTEMPTS=40
SLEEP_SECONDS=1800          # 30 min between attempts while the quota refills
LOG=golden_run.log

for attempt in $(seq 1 $MAX_ATTEMPTS); do
  echo "=== attempt $attempt at $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"
  .venv/bin/python golden_run.py >> "$LOG" 2>&1

  run=$(.venv/bin/python -c "
import json
try:
    d = json.load(open('golden_pairs.json'))
    print(d.get('questions_run', 0), d.get('questions_total', 0))
except Exception:
    print(0, 0)
  ")
  set -- $run
  echo "attempt $attempt: $1 of $2 answered" >> "$LOG"

  if [ "$1" != "0" ] && [ "$1" = "$2" ]; then
    echo "=== COMPLETE at $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"
    exit 0
  fi

  sleep $SLEEP_SECONDS
done

echo "=== gave up after $MAX_ATTEMPTS attempts ===" >> "$LOG"
exit 1
