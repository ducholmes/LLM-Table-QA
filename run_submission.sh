#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ "$#" -ne 2 ]; then
    echo "Usage: sh run_submission.sh INPUT_DIR OUTPUT_CSV" >&2
    exit 2
fi
PYTHONPATH=src${PYTHONPATH:+:$PYTHONPATH} python3 -m table_qa.main \
    --config config/submission.yaml \
    --input-dir "$1" \
    --output "$2"
