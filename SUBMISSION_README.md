# Submission package

This ZIP contains the pipeline source, prompts, schemas, locked competition
configuration, and dependency list. It deliberately excludes query data,
workbooks, public labels, API keys, run artifacts, and model weights.

The scoring environment must mount the inputs at these paths relative to the
extracted package root:

- `data/Public data 1/queries.json`
- `data/Public data 1/xlsx_files/`

Install the dependencies with `python3 -m pip install -r requirements-submission.txt`,
set `OPENROUTER_API_KEY`, then run `sh run_submission.sh`. The command accepts no
competition CLI overrides. A successful run writes `submission.csv` and
`summary.json` under a unique directory in `runs/`.
