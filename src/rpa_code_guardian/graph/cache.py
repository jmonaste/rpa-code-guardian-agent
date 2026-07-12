"""Disk cache for per-workflow summaries.

Keyed by (content hash, model, prompt version): re-running the agent on a
project only re-summarizes workflows whose XAML actually changed. This is the
"write context to disk" leg of the context strategy and doubles as crash
recovery for the expensive map phase.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from ..model.summaries import WorkflowSummary

PROMPT_VERSION = "1"


class SummaryCache:
    def __init__(self, path: Path, model: str, enabled: bool = True) -> None:
        self.path = path
        self.model = model
        self.enabled = enabled
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if enabled and path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._data = {}

    def _key(self, content_hash: str) -> str:
        return f"{content_hash}:{self.model}:{PROMPT_VERSION}"

    def get(self, content_hash: str) -> WorkflowSummary | None:
        if not self.enabled:
            return None
        with self._lock:
            raw = self._data.get(self._key(content_hash))
        if raw is None:
            return None
        try:
            return WorkflowSummary.model_validate(raw)
        except Exception:  # noqa: BLE001 - stale schema
            return None

    def put(self, content_hash: str, summary: WorkflowSummary) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._data[self._key(content_hash)] = summary.model_dump()

    def save(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._data), encoding="utf-8")
            except OSError:
                pass
