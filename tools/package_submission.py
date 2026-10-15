"""Build a minimal, validated competition source package ZIP."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from table_qa.config import load_config  # noqa: E402
from table_qa.contracts import PipelineError  # noqa: E402
from table_qa.query_loader import load_queries  # noqa: E402


MAX_ZIP_BYTES = 100_000_000
FIXED_FILES = (
    "SUBMISSION_README.md",
    "README.md",
    "pyproject.toml",
    "requirements-submission.txt",
    "run_submission.sh",
    "config/submission.yaml",
)


def _package_sources() -> list[Path]:
    root = ROOT / "src" / "table_qa"
    return sorted(path for path in root.rglob("*")
                  if path.is_file() and path.suffix in {".py", ".txt", ".json"}
                  and "__pycache__" not in path.parts)


def _validate_source_data() -> None:
    config = load_config(ROOT / "config" / "submission.yaml")
    if config.run_profile != "competition":
        raise ValueError("config/submission.yaml must use competition profile")
    work = load_queries(ROOT / config.queries, ROOT / config.data_dir)
    if (len(work) != config.expected_query_count
            or len({path for _, path in work}) != config.expected_workbook_count):
        raise ValueError("Submission input must resolve to 20 queries across 10 workbooks")


def build_submission_zip(output: Path, *, max_bytes: int = MAX_ZIP_BYTES) -> int:
    _validate_source_data()
    files = [ROOT / relative for relative in FIXED_FILES]
    files.extend(_package_sources())
    missing = [path.relative_to(ROOT).as_posix() for path in files
               if not path.is_file() or path.is_symlink()
               or not path.resolve().is_relative_to(ROOT.resolve())]
    if missing:
        raise FileNotFoundError("Missing package files: " + ", ".join(missing))

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".zip.tmp", delete=False) as temp_file:
        temporary = Path(temp_file.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in files:
                archive.write(path, path.relative_to(ROOT).as_posix())

        size = temporary.stat().st_size
        if size >= max_bytes:
            raise ValueError(f"Submission ZIP is {size} bytes; limit is {max_bytes} bytes")

        with zipfile.ZipFile(temporary) as archive:
            names = set(archive.namelist())
            required = set(FIXED_FILES)
            if not required <= names:
                raise ValueError("Submission ZIP is missing required files")
            forbidden = [name for name in names if (
                name.endswith("sample_submission.csv") or name.endswith(".env")
                or name.startswith("data/") or name.startswith("runs/")
                or name.startswith("tests/") or name.startswith(".git/")
            )]
            if forbidden:
                raise ValueError("Submission ZIP includes excluded content: " + ", ".join(sorted(forbidden)))
        temporary.chmod(0o644)
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "submission.zip")
    args = parser.parse_args(argv)
    try:
        size = build_submission_zip(args.output)
    except (OSError, ValueError, PipelineError) as exc:
        print(f"PACKAGE_ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Submission ZIP: {args.output} ({size} bytes; limit {MAX_ZIP_BYTES} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
