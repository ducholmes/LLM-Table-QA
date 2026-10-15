"""JSONL event logging shared by the runner and provider telemetry."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable


def append_jsonl(path: Path, event: dict[str, Any], run_id: str,
                 *, default: Callable[[Any], Any] | None = None) -> None:
    record = dict(event)
    record.setdefault("run_id", run_id)
    record.setdefault("timestamp_utc", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False, default=default) + "\n")
