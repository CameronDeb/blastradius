#!/bin/sh
# Run the selector natively on the sample set and score it.
ROOT=$(cd "$(dirname "$0")/.." && pwd)
W=/c/Users/Owner/br-work
BR_DATA=$ROOT/kit/sample BR_SRC=$W/sqlglot BR_NOFORK=${BR_NOFORK:-1} PYTHONUTF8=1 \
  $W/venv/Scripts/python "$ROOT/selector.py" > "$ROOT/out.jsonl" 2> "$ROOT/out.log"
python -I "$ROOT/kit/score.py" "$ROOT/kit/sample" "$ROOT/kit/sample/answers.json" "$ROOT/out.jsonl"
