"""Turning API responses into table rows and readable JSON."""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

MAX_COLUMNS = 8
MAX_CELL = 60
TAG_NAME_COLUMN = "Name (tag)"


def to_json(value: Any, indent: int | None = 2) -> str:
    return json.dumps(value, indent=indent, default=_json_default, ensure_ascii=False)


def _json_default(value: Any) -> Any:
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return f"<{len(value)} bytes>"
    if isinstance(value, set):
        return sorted(value, key=str)
    return str(value)


def tag_name(item: dict[str, Any]) -> str | None:
    """Value of the ``Name`` tag in EC2-style ``Tags: [{Key, Value}]`` lists."""
    for key in ("Tags", "tags", "TagList", "TagSet"):
        tags = item.get(key)
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, dict) and tag.get("Key") == "Name":
                    return str(tag.get("Value", ""))
        elif isinstance(tags, dict) and isinstance(tags.get("Name"), str):
            return tags["Name"]
    return None


def cell_value(value: Any) -> Any:
    """Collapse a value into something that fits in a table cell (``None`` if it doesn't)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, _dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        # EC2 ``State: {Code, Name}`` and similar small status objects.
        for key in ("Name", "Status", "State", "Value", "Code"):
            if key in value and isinstance(value[key], (str, int)) and len(value) <= 3:
                return value[key]
        return None
    if isinstance(value, list):
        if value and all(isinstance(v, (str, int)) for v in value):
            return ", ".join(str(v) for v in value)
        return None
    return None


def _column_score(key: str, nouns: set[str]) -> int:
    k = key.lower()
    if key == TAG_NAME_COLUMN:
        return 0
    if k in ("name", "title", "key", "alias", "id"):
        return 1
    if any(k in (n + "name", n + "id", n + "identifier") for n in nouns):
        return 1
    if any(k.startswith(n) and not k.endswith(("arn", "url")) for n in nouns if len(n) > 2):
        return 2
    if k in ("state", "status") or k.endswith(("state", "status")):
        return 3
    if k.endswith(("name", "identifier", "id")):
        return 4
    if k.endswith(("type", "class")) or k in ("size", "engine", "runtime", "version"):
        return 5
    if "creat" in k or "time" in k or "date" in k or "modified" in k:
        return 6
    if k.endswith(("arn", "url")):
        return 8
    return 7


def row_view(item: Any) -> dict[str, Any]:
    """Flat dict of displayable cells for an item."""
    if not isinstance(item, dict):
        return {"Value": cell_value(item) if cell_value(item) is not None else str(item)}
    row: dict[str, Any] = {}
    name = tag_name(item)
    if name is not None and "Name" not in item:
        row[TAG_NAME_COLUMN] = name
    for key, value in item.items():
        cell = cell_value(value)
        if cell is not None:
            row[key] = cell
    return row


def choose_columns(rows: list[dict[str, Any]], max_columns: int = MAX_COLUMNS, nouns: set[str] | None = None) -> list[str]:
    """Pick the most useful columns; ``nouns`` (e.g. ``{"instance"}``) boost the resource's own fields."""
    nouns = {n for n in (nouns or set()) if n}
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for row in rows[:100]:
        for key in row:
            counts[key] = counts.get(key, 0) + 1
            first_seen.setdefault(key, len(first_seen))
    if not counts:
        return []
    sample = min(len(rows), 100)
    keys = [k for k in counts if counts[k] * 4 >= sample] or list(counts)
    keys.sort(key=lambda k: (_column_score(k, nouns), first_seen[k]))
    return keys[:max_columns]


def format_cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) > MAX_CELL:
        text = text[: MAX_CELL - 1] + "…"
    return text


def human_size(num: int | None) -> str:
    if num is None:
        return ""
    size = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(num)
