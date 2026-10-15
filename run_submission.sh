#!/bin/sh
set -eu
cd "$(dirname "$0")"
PYTHONPATH=src${PYTHONPATH:+:$PYTHONPATH} python3 -m table_qa.main --config config/submission.yaml
