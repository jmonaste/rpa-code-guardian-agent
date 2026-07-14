"""Corporate style lint applied to every generated document.

The deliverable must be professional and emoji-free regardless of what the
model wrote, so this is enforced here deterministically rather than requested
politely in prompts.
"""

from __future__ import annotations

import re

# Pictographs, emoji, dingbats, transport symbols, flags, variation selectors.
_EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"
    "\U00002700-\U000027bf"
    "\U00002600-\U000026ff"
    "\U0001f1e6-\U0001f1ff"
    "\U0000fe0e-\U0000fe0f"
    "\U0000200d"
    "\U000020e3"
    "\U00002b00-\U00002bff"
    "]+"
)

_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_EXTRA_BLANK_RE = re.compile(r"\n{3,}")

_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
_TABLE_RE = re.compile(r"^\s*\|")
_ODD_BULLET_RE = re.compile(r"^(\s*)[•‣⁃◦·–—*]\s+(?=\S)")
_WRAPPING_FENCE_RE = re.compile(r"^\s*(?:```|~~~)[a-zA-Z]*\s*\n(.*)\n\s*(?:```|~~~)\s*$", re.DOTALL)


def lint_markdown(text: str) -> str:
    """Strip emojis, trailing whitespace and excess blank lines."""
    text = _EMOJI_RE.sub("", text)
    text = _TRAILING_WS_RE.sub("", text)
    text = _EXTRA_BLANK_RE.sub("\n\n", text)
    return text.strip() + "\n"


def normalize_prose(text: str) -> str:
    """Clean one model-written prose block so it slots into the document.

    The document skeleton is deterministic, but the LLM prose dropped into each
    section is not: local models wrap the whole answer in a ```` ```markdown ````
    fence, emit headings that collide with the document's own hierarchy, use odd
    bullet characters, and glue lists or headings to the preceding paragraph
    (which strict Markdown / Obsidian then refuse to render). These are fixed
    deterministically here. Content inside fenced code blocks is left untouched.
    """
    if not text or not text.strip():
        return ""
    wrap = _WRAPPING_FENCE_RE.match(text.strip())
    if wrap:
        text = wrap.group(1)
    lines = text.replace("\r\n", "\n").split("\n")

    # Demote stray headings so they never rise above the document's own ###
    # subsections; preserve their relative depth by shifting to start at level 4.
    levels = [len(m.group(1)) for line, m in _headings(lines) if m]
    offset = 4 - min(levels) if levels and min(levels) < 4 else 0

    cleaned: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            cleaned.append(line)
            continue
        if in_fence:
            cleaned.append(line)
            continue
        heading = _HEADING_RE.match(line)
        if heading and offset:
            line = "#" * min(len(heading.group(1)) + offset, 6) + " " + heading.group(2)
        elif _ODD_BULLET_RE.match(line) and not _LIST_RE.match(line):
            line = _ODD_BULLET_RE.sub(r"\1- ", line)
        cleaned.append(line)

    spaced = _ensure_block_spacing(cleaned)
    text = "\n".join(spaced)
    text = _EXTRA_BLANK_RE.sub("\n\n", text)
    return text.strip()


def _headings(lines: list[str]):
    """Yield (line, heading-match) pairs for heading lines outside code fences."""
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        yield line, (None if in_fence else _HEADING_RE.match(line))


def _line_kind(line: str) -> str:
    if not line.strip():
        return "blank"
    if _HEADING_RE.match(line):
        return "heading"
    if _TABLE_RE.match(line):
        return "table"
    if _LIST_RE.match(line):
        return "list"
    return "text"


def _ensure_block_spacing(lines: list[str]) -> list[str]:
    """Insert the blank lines that block elements need to render, fence-aware."""
    out: list[str] = []
    in_fence = False
    need_blank_before_next = False  # set right after a closing fence

    def sep() -> None:
        if out and out[-1].strip():
            out.append("")

    for line in lines:
        if _FENCE_RE.match(line):
            if not in_fence:  # opening fence: separate from preceding paragraph
                sep()
            out.append(line)
            in_fence = not in_fence
            need_blank_before_next = not in_fence  # just closed -> blank before next
            continue
        if in_fence:
            out.append(line)
            continue
        if need_blank_before_next and line.strip():
            sep()
            need_blank_before_next = False

        kind = _line_kind(line)
        prev = _line_kind(out[-1]) if out else "blank"
        block = {"heading", "list", "table"}
        starting_block = kind in block and prev in {"text"}
        # A non-indented paragraph after a list/table closes that block; an
        # indented line is a list-item continuation and must stay attached.
        leaving_block = (
            kind == "text" and prev in {"list", "table"} and not line[:1].isspace()
        )
        if kind == "heading" or prev == "heading" or starting_block or leaving_block:
            sep()
        out.append(line)
    return out


def md_cell(text: str) -> str:
    """Make a string safe inside a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def md_anchor(heading: str) -> str:
    """GitHub/Obsidian-style anchor for a heading."""
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"[\s]+", "-", slug)
