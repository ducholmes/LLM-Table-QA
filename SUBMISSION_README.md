# Submission package

This ZIP contains the pipeline source, prompts, schemas, submission
configuration, and dependency list. It deliberately excludes query data,
workbooks, labels, API keys, run artifacts, and model weights.

The input directory must contain `queries.json` and the `.xlsx` files referenced
by its `file_name` fields:

```text
private_input/
  queries.json
  workbook-a.xlsx
  workbook-b.xlsx
```

Install the dependencies with `python3 -m pip install -r requirements-submission.txt`,
set `OPENROUTER_API_KEY`, then run:

```bash
sh run_submission.sh /absolute/path/to/private_input /absolute/path/to/submission.csv
```

When invoking the Python CLI directly, `--input-dir` also accepts an unquoted
path split by the shell, for example `--input-dir data/Public data 2`.

The output CSV has exactly the columns `id,answer` and preserves query order.
Diagnostics are written to an `artifacts/` directory beside the output. A non-zero exit means one or more queries
failed, but the submission profile still writes one output row for every input
ID, using an empty answer for failed queries.

Requirements: Python 3.11 or newer, network access to OpenRouter, and a writable
output parent directory. The program may be launched from any working directory;
the shell entrypoint resolves the package location itself.
