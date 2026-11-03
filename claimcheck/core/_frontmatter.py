"""Minimal YAML frontmatter reader — stdlib only.

The core loaders import pydantic and stdlib, nothing else. PyYAML is
therefore an optional extra: when it is installed we use it, and when it is not
we fall back to the reader here, which covers the subset frontmatter actually
uses — scalars, inline lists, block lists, and folded multi-line strings.

This is deliberately not a general YAML implementation. A construct it cannot
parse raises, rather than guessing.
"""

from __future__ import annotations

from typing import Any

__all__ = ["split_frontmatter", "parse_simple_yaml", "load_yaml"]

_DELIM = "---"


def split_frontmatter(text: str) -> tuple[str, str]:
    """Split ``text`` into (frontmatter_source, body).

    Returns an empty frontmatter string when the document has none.
    """
    stripped = text.lstrip("﻿")
    lines = stripped.splitlines()
    if not lines or lines[0].strip() != _DELIM:
        return "", stripped

    for index in range(1, len(lines)):
        if lines[index].strip() == _DELIM:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :]).lstrip("\n")

    # Opening delimiter with no close — treat the whole document as body rather
    # than silently swallowing it as frontmatter.
    return "", stripped


def _coerce(raw: str) -> Any:
    """Convert a scalar token to bool/int/float/None/str."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    lowered = value.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "~", ""):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _split_top_level(inner: str) -> list[str]:
    """Split on commas that are not nested inside brackets, braces or quotes."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    for char in inner:
        if quote:
            if char == quote:
                quote = None
            current.append(char)
            continue
        if char in "\"'":
            quote = char
            current.append(char)
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current))
    return [p for p in (part.strip() for part in parts) if p]


def _parse_flow(raw: str) -> Any:
    """Parse an inline flow collection: ``[a, b]`` or ``{k: v, k2: v2}``."""
    value = raw.strip()
    inner = value[1:-1].strip()
    if value.startswith("["):
        return [_parse_scalar_or_flow(part) for part in _split_top_level(inner)]
    result: dict[str, Any] = {}
    for part in _split_top_level(inner):
        key, sep, rest = part.partition(":")
        if not sep:
            raise ValueError(f"Cannot parse flow mapping entry: {part!r}")
        result[key.strip()] = _parse_scalar_or_flow(rest)
    return result


def _parse_scalar_or_flow(raw: str) -> Any:
    value = raw.strip()
    if (value.startswith("[") and value.endswith("]")) or (
        value.startswith("{") and value.endswith("}")
    ):
        return _parse_flow(value)
    return _coerce(value)


def parse_simple_yaml(source: str) -> dict[str, Any]:
    """Parse the frontmatter subset: scalars, inline/block lists, nested maps.

    Multi-line scalars written as a continued indented block (the style used for
    long ``description:`` values) are joined with single spaces.
    """
    result: dict[str, Any] = {}
    lines = source.splitlines()
    index = 0

    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue

        indent = len(line) - len(line.lstrip())
        if ":" not in line:
            raise ValueError(f"Cannot parse frontmatter line: {line!r}")

        key, _, remainder = line.strip().partition(":")
        key = key.strip()
        remainder = remainder.strip()
        index += 1

        if (remainder.startswith("[") and remainder.endswith("]")) or (
            remainder.startswith("{") and remainder.endswith("}")
        ):
            result[key] = _parse_flow(remainder)
            continue

        if remainder in ("|", ">", "|-", ">-"):
            block: list[str] = []
            while index < len(lines):
                nxt = lines[index]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                    break
                block.append(nxt.strip())
                index += 1
            joiner = "\n" if remainder.startswith("|") else " "
            result[key] = joiner.join(block).strip()
            continue

        if remainder:
            # A scalar that continues on more-indented following lines.
            parts = [remainder]
            while index < len(lines):
                nxt = lines[index]
                if not nxt.strip():
                    break
                nxt_indent = len(nxt) - len(nxt.lstrip())
                if nxt_indent <= indent or nxt.lstrip().startswith("- ") or ":" in nxt:
                    break
                parts.append(nxt.strip())
                index += 1
            result[key] = _coerce(" ".join(parts)) if len(parts) == 1 else " ".join(parts)
            continue

        # Key with no inline value: a block list or a nested map follows.
        items: list[Any] = []
        nested: list[str] = []
        while index < len(lines):
            nxt = lines[index]
            if not nxt.strip():
                index += 1
                continue
            nxt_indent = len(nxt) - len(nxt.lstrip())
            if nxt_indent <= indent:
                break
            if nxt.lstrip().startswith("- "):
                items.append(_coerce(nxt.lstrip()[2:]))
            else:
                nested.append(nxt)
            index += 1

        if items:
            result[key] = items
        elif nested:
            dedent = min(len(n) - len(n.lstrip()) for n in nested)
            result[key] = parse_simple_yaml("\n".join(n[dedent:] for n in nested))
        else:
            result[key] = None

    return result


def load_yaml(source: str) -> dict[str, Any]:
    """Parse YAML with PyYAML when available, else the stdlib fallback."""
    if not source.strip():
        return {}
    try:
        import yaml  # noqa: PLC0415 - optional dependency, probed at call time
    except ImportError:
        return parse_simple_yaml(source)
    loaded = yaml.safe_load(source)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("Frontmatter must be a mapping")
    return loaded
