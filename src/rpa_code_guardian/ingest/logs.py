"""Bounded sampling of execution log files: levels, span, error samples."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..model.ir import LogDigest

_LEVEL_RE = re.compile(r"\b(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL)\b", re.IGNORECASE)
_MAX_FILES = 5
_MAX_BYTES_PER_FILE = 2_000_000  # tail of very large logs
_MAX_ERROR_SAMPLES = 15


def digest_logs(files: list[Path], project_root: Path) -> LogDigest:
    """Summarize log files without ever shipping them whole to the LLM."""
    digest = LogDigest()
    for path in files[:_MAX_FILES]:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) > _MAX_BYTES_PER_FILE:
            data = data[-_MAX_BYTES_PER_FILE:]
        text = data.decode("utf-8", errors="replace")
        digest.files.append(str(path.relative_to(project_root)).replace("\\", "/"))

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            digest.total_lines += 1
            level, timestamp, message = _parse_line(line)
            if level:
                digest.level_counts[level] = digest.level_counts.get(level, 0) + 1
            if timestamp:
                if not digest.first_timestamp:
                    digest.first_timestamp = timestamp
                digest.last_timestamp = timestamp
            if level in ("ERROR", "FATAL", "WARN") and len(digest.error_samples) < _MAX_ERROR_SAMPLES:
                digest.error_samples.append(f"[{level}] {message[:220]}")
    return digest


def _parse_line(line: str) -> tuple[str, str, str]:
    """Return (LEVEL, timestamp, message) for a JSON or plain-text log line."""
    if line.startswith("{"):
        try:
            obj = json.loads(line)
            level = str(obj.get("level", obj.get("Level", ""))).upper()
            level = {"WARNING": "WARN", "INFORMATION": "INFO"}.get(level, level)
            ts = str(obj.get("timeStamp", obj.get("timestamp", "")))[:19]
            msg = str(obj.get("message", obj.get("Message", "")))
            return level, ts, msg or line
        except (json.JSONDecodeError, TypeError):
            pass
    m = _LEVEL_RE.search(line[:120])
    level = m.group(1).upper() if m else ""
    level = {"WARNING": "WARN"}.get(level, level)
    return level, "", line
