"""Read UiPath ``project.json`` metadata."""

from __future__ import annotations

import json
from pathlib import Path

from ..model.ir import ProjectMeta


def parse_project_json(path: Path) -> ProjectMeta:
    """Parse project.json; tolerate missing keys and schema drift across Studio versions."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return ProjectMeta()

    design = data.get("designOptions", {}) or {}
    deps = data.get("dependencies", {}) or {}
    return ProjectMeta(
        name=str(data.get("name", "")),
        description=str(data.get("description", "")),
        main=str(data.get("main", "Main.xaml")).replace("\\", "/"),
        project_version=str(data.get("projectVersion", "")),
        studio_version=str(data.get("studioVersion", "")),
        schema_version=str(data.get("schemaVersion", "")),
        target_framework=str(data.get("targetFramework", design.get("targetFramework", ""))),
        expression_language=str(
            data.get("expressionLanguage", design.get("expressionLanguage", ""))
        ),
        dependencies={str(k): str(v).strip("[]") for k, v in deps.items()},
    )
