"""XAML -> :class:`WorkflowIR`: the deterministic compression layer.

A UiPath ``.xaml`` is Windows Workflow Foundation XML where the signal
(arguments, variables, activity structure, invocations, log messages,
annotations) is buried under designer noise (view state, geometry, debug
symbols, namespace clutter). This parser extracts only the signal, typically
shrinking a workflow 10-50x before it is ever shown to an LLM.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from ..model.ir import Argument, Invocation, LogLine, Variable, WorkflowIR

XAML_NS = "http://schemas.microsoft.com/winfx/2006/xaml"
SAP2010_NS = "http://schemas.microsoft.com/netfx/2010/xaml/activities/presentation"
SAP_NS = "http://schemas.microsoft.com/netfx/2009/xaml/activities/presentation"
SADS_NS = "http://schemas.microsoft.com/netfx/2010/xaml/activities/debugger"

ANNOTATION_ATTR = f"{{{SAP2010_NS}}}Annotation.AnnotationText"

# Property elements whose subtree is pure designer/debug noise.
_NOISE_PREFIXES = {
    "WorkflowViewStateService",
    "VirtualizedContainerService",
    "Annotation",  # handled separately via the attribute
    "DebugSymbol",
    "TextExpression",
    "WorkflowViewState",
}

# Structural wrappers that should not appear as outline nodes but whose
# children must still be walked.
_TRANSPARENT = {"FlowStep", "ActivityAction", "CancellationScope"}

# Elements that are data, not activities.
_NON_ACTIVITY = {
    "Variable", "DelegateInArgument", "DelegateOutArgument", "Collection",
    "Dictionary", "ViewStateData", "ViewStateManager", "TypeArguments",
    "String", "Boolean", "Int32", "Null", "Reference",
    "InArgument", "OutArgument", "InOutArgument", "Literal",
}

MAX_OUTLINE_DEPTH = 12
MAX_OUTLINE_NODES = 350

_CONFIG_KEY_RE = re.compile(r'Config\((?:&quot;|")([^"&]{1,80})(?:&quot;|")\)')
_HARDCODED_PATH_RE = re.compile(r'[A-Za-z]:\\[^"&<>\r\n\\]{2}[^"&<>\r\n]*')
_LITERAL_TIMESPAN_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}(\.\d+)?$")
_SELECTOR_TOKEN_RE = re.compile(r"(?:app|title|cls)='([^']{1,80})'")


def _local(tag: str) -> str:
    """Strip the namespace from an element tag."""
    return tag.rsplit("}", 1)[-1]


def _ns(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _clean_type(raw: str) -> str:
    """``InArgument(scg:Dictionary(x:String, x:Object))`` -> ``Dictionary(String, Object)``."""
    return re.sub(r"\b\w+:", "", raw)


def _is_activity(elem: ET.Element) -> bool:
    tag = elem.tag
    if _ns(tag) in (XAML_NS, SAP_NS, SAP2010_NS, SADS_NS):
        return False
    local = _local(tag)
    if "." in local:
        return False
    return local not in _NON_ACTIVITY


def _display(elem: ET.Element) -> str:
    return elem.get("DisplayName", "")


class _OutlineWalker:
    """Recursive activity walk building the outline and per-activity stats."""

    def __init__(self, ir: WorkflowIR, workflow_dir: str) -> None:
        self.ir = ir
        self.workflow_dir = workflow_dir
        self.lines: list[str] = []
        self.count = 0
        self.max_depth = 0

    def walk(self, elem: ET.Element, depth: int) -> None:
        local = _local(elem.tag)

        if "." in local:  # property element: recurse or prune
            prefix = local.split(".", 1)[0]
            if prefix in _NOISE_PREFIXES:
                return
            for child in elem:
                self.walk(child, depth)
            return

        if _ns(elem.tag) in (XAML_NS, SAP_NS, SAP2010_NS, SADS_NS) or local in _NON_ACTIVITY:
            return

        if local in _TRANSPARENT:
            for child in elem:
                self.walk(child, depth)
            return

        self.count += 1
        self.max_depth = max(self.max_depth, depth)
        self._observe(elem, local)

        if self.count <= MAX_OUTLINE_NODES and depth <= MAX_OUTLINE_DEPTH:
            name = _display(elem)
            note = elem.get(ANNOTATION_ATTR, "")
            line = "  " * depth + f"- {local}"
            if name and name != local:
                line += f' "{name}"'
            if note:
                line += f"  // {note.splitlines()[0][:100]}"
            self.lines.append(line)

        for child in elem:
            self.walk(child, depth + 1)

    # -- per-activity extraction -------------------------------------------

    def _observe(self, elem: ET.Element, local: str) -> None:
        ir = self.ir
        if local == "InvokeWorkflowFile":
            ir.invocations.append(self._invocation(elem))
        elif local == "LogMessage":
            level = elem.get("Level", "Info").strip("[]").split(".")[-1]
            message = elem.get("Message", "")
            if not message:  # message may live in a property child
                for child in elem.iter():
                    if _local(child.tag) == "LogMessage.Message":
                        message = "".join(t.strip() for t in child.itertext())
                        break
            ir.log_messages.append(LogLine(level=level, message=message.strip("[]").strip().strip('"')))
        elif local == "TryCatch":
            ir.try_catch_count += 1
        elif local == "Catch":
            body = [
                d for d in elem.iter()
                if d is not elem and _is_activity(d)
                and _local(d.tag) not in ({"Sequence", "Comment"} | _TRANSPARENT)
            ]
            if not body:
                ir.empty_catches += 1
        elif local == "CommentOut":
            ir.disabled_activities += 1
        elif local == "Delay":
            duration = elem.get("Duration", "")
            if _LITERAL_TIMESPAN_RE.match(duration) and duration not in ("00:00:00", "0:00:00"):
                ir.hardcoded_delays.append(duration)
        elif local == "State":
            name = _display(elem)
            if name:
                self.ir.states.append(name)

        selector = elem.get("Selector", "")
        if selector:
            ir.selector_apps.extend(_SELECTOR_TOKEN_RE.findall(selector))

    def _invocation(self, elem: ET.Element) -> Invocation:
        raw = elem.get("WorkflowFileName", "")
        dynamic = raw.startswith("[") or not raw
        target = raw if dynamic else raw.replace("\\", "/")
        if dynamic:
            target = raw.strip("[]")[:80]
        args: dict[str, str] = {}
        for child in elem:
            if _local(child.tag) == "InvokeWorkflowFile.Arguments":
                for arg in child:
                    key = arg.get(f"{{{XAML_NS}}}Key", "")
                    value = (arg.text or "").strip() or "".join(t.strip() for t in arg.itertext())
                    if key:
                        args[key] = value[:120]
        return Invocation(target=target, dynamic=dynamic, arguments=args)


def parse_xaml(file_path: Path, rel_path: str) -> WorkflowIR:
    """Parse one workflow file into its IR. Never raises: parse failures are
    recorded in ``parse_error`` with whatever could be salvaged via regex."""
    raw = file_path.read_text(encoding="utf-8", errors="replace")
    ir = WorkflowIR(path=rel_path, display_name=Path(rel_path).stem, raw_chars=len(raw))
    ir.content_hash = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]

    ir.config_keys_used = sorted(set(_CONFIG_KEY_RE.findall(raw)))
    ir.hardcoded_paths = sorted(
        {m[:120] for m in _HARDCODED_PATH_RE.findall(raw) if "UiPath" not in m and "Microsoft" not in m}
    )[:10]

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        ir.parse_error = f"XML parse error: {exc}"
        return ir

    ir.annotation = root.get(ANNOTATION_ATTR, "")

    # Arguments from x:Members
    for member in root.iter(f"{{{XAML_NS}}}Property"):
        raw_type = member.get("Type", "")
        direction = "property"
        m = re.match(r"(In|Out|InOut)Argument\((.+)\)$", raw_type)
        inner = raw_type
        if m:
            direction = {"In": "in", "Out": "out", "InOut": "io"}[m.group(1)]
            inner = m.group(2)
        ir.arguments.append(
            Argument(
                name=member.get("Name", "?"),
                direction=direction,
                type=_clean_type(inner),
                annotation=member.get(ANNOTATION_ATTR, ""),
            )
        )

    # Variables anywhere in the tree
    for var in root.iter():
        if _local(var.tag) == "Variable" and "." not in _local(var.tag):
            type_args = var.get(f"{{{XAML_NS}}}TypeArguments", "")
            ir.variables.append(
                Variable(
                    name=var.get("Name", "?"),
                    type=_clean_type(type_args),
                    default=(var.get("Default") or "")[:80],
                )
            )

    # Root activity = first activity child of <Activity>
    root_activity = None
    for child in root:
        if _is_activity(child):
            root_activity = child
            break

    walker = _OutlineWalker(ir, workflow_dir=str(Path(rel_path).parent))
    if root_activity is not None:
        ir.root_type = _local(root_activity.tag)
        if not ir.annotation:
            ir.annotation = root_activity.get(ANNOTATION_ATTR, "")
        walker.walk(root_activity, 0)
        if walker.count > MAX_OUTLINE_NODES:
            walker.lines.append(f"  (outline capped at {MAX_OUTLINE_NODES} of {walker.count} activities)")
    else:
        ir.parse_error = ir.parse_error or "no root activity found"

    ir.outline = "\n".join(walker.lines)
    ir.activity_count = walker.count
    ir.max_depth = walker.max_depth
    ir.selector_apps = sorted(set(ir.selector_apps))
    if root_activity is not None and _display(root_activity):
        ir.display_name = _display(root_activity)
    return ir
