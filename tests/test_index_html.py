"""Static checks on the single-page web UI's script."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.resources import files

import pytest

# Interpolations that are not server values: HTML fragments the script builds itself (their own
# template literals are checked too), numbers it computed, and constants.
_SAFE_NAMES = frozenset(
    {"badge", "badges", "items", "lights", "opts", "reason", "smooth", "MAX_ZIGBEE_LIGHTS"}
)
_SAFE_CALL = re.compile(r"(esc|Number|encodeURIComponent)\(")


@dataclass(frozen=True)
class _Template:
    """A template literal: its offset in the script and its ``${...}`` expressions."""

    start: int
    expressions: list[str]


def test_server_values_are_escaped_in_html() -> None:
    """Every value interpolated into the UI's HTML goes through esc(), or is static or numeric."""
    script = _script()
    checked = {
        expression.strip()
        for template in _templates(script)
        if _builds_html(script, template)
        for expression in template.expressions
    }
    unsafe = sorted(expression for expression in checked if not _is_safe(expression))
    assert not unsafe, f"Interpolate server values into HTML through esc(): {unsafe}"
    # The scan must have reached the TV cards and skipped plain text and URLs.
    assert {'esc(tv.devicetype || "TV")', "esc(a.name)"} <= checked
    assert not {"s.host", "encodeURIComponent(tv.username)"} & checked


@pytest.mark.parametrize(
    ("expression", "safe"),
    [
        ("esc(tv.devicetype)", True),
        ('tv.created ? "paired " + esc(tv.created) : ""', True),
        ('items || (ok ? \'<b>\' + "x</b>" : "")', True),
        ("Number(tv.stream_priority ?? 0)", True),
        ("smooth.toFixed(2)", True),
        ("tv.devicetype", False),
        ('tv.devicetype || "TV"', False),
        ('tv.created ? "paired " + tv.created : ""', False),
        ("esc(a.id) + a.name", False),
        ("esc(a.id).concat(a.name)", False),
    ],
)
def test_escape_check_classifies_expressions(expression: str, safe: bool) -> None:
    """The escape check accepts escaped, static and numeric values and rejects raw ones."""
    assert _is_safe(expression) is safe


def _script() -> str:
    """Return the UI's inline script."""
    html = (files("ambilight_hue_bridge.web") / "static" / "index.html").read_text("utf-8")
    scripts: list[str] = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    (script,) = scripts
    return script


def _templates(code: str) -> list[_Template]:
    """Return every template literal in ``code``, including ones nested in an interpolation."""
    templates: list[_Template] = []
    _scan_code(code, 0, templates, until_brace=False)
    return templates


def _builds_html(code: str, template: _Template) -> bool:
    """Return False for a template that is plain text (``textContent``) or an ``api()`` URL."""
    # The statement so far; string contents can only shorten it, which keeps a template checked.
    begin = max(code.rfind(char, 0, template.start) for char in ";{}") + 1
    statement = code[begin : template.start]
    return not (re.search(r"\.textContent\s*=", statement) or statement.endswith("api("))


def _is_safe(expression: str) -> bool:
    """Return True when an interpolated expression can only produce escaped or static text."""
    expr = expression.strip()
    masked = _mask_strings(expr)
    if masked.startswith("(") and _closing_paren(masked, 0) == len(masked) - 1:
        return _is_safe(expr[1:-1])
    if (ternary := _ternary(masked)) is not None:
        question, colon = ternary
        return _is_safe(expr[question + 1 : colon]) and _is_safe(expr[colon + 1 :])
    parts = _split_operands(masked)
    if len(parts) > 1:
        return all(_is_safe(expr[begin:end]) for begin, end in parts)
    # A nested template literal counts as static: the scan checks its interpolations itself.
    if re.fullmatch(r"(['\"`])_*\1|\d+(\.\d+)?", masked):
        return True
    call = _SAFE_CALL.match(masked)
    if call and _closing_paren(masked, call.end() - 1) == len(masked) - 1:
        return True
    return expr in _SAFE_NAMES or re.fullmatch(r"\w+\.toFixed\(\d*\)", expr) is not None


def _scan_code(code: str, i: int, templates: list[_Template], *, until_brace: bool) -> int:
    """Collect the template literals in code from ``i``; stop after an unmatched ``}``."""
    depth = 0
    while i < len(code):
        char = code[i]
        if char in "'\"":
            i = _skip_string(code, i)
        elif code.startswith("//", i):
            i = code.index("\n", i)
        elif code.startswith("/*", i):
            i = code.index("*/", i) + 2
        elif char == "`":
            i = _scan_template(code, i, templates)
        else:
            if char == "{":
                depth += 1
            elif char == "}":
                if until_brace and depth == 0:
                    return i + 1
                depth -= 1
            i += 1
    return i


def _scan_template(code: str, start: int, templates: list[_Template]) -> int:
    """Collect the template literal starting at ``start``; return the index after it."""
    expressions: list[str] = []
    i = start + 1
    while code[i] != "`":
        if code[i] == "\\":
            i += 2
        elif code.startswith("${", i):
            end = _scan_code(code, i + 2, templates, until_brace=True)
            expressions.append(code[i + 2 : end - 1])
            i = end
        else:
            i += 1
    templates.append(_Template(start, expressions))
    return i + 1


def _skip_string(code: str, start: int) -> int:
    """Return the index after the quoted string literal starting at ``start``."""
    i = start + 1
    while code[i] != code[start]:
        i += 2 if code[i] == "\\" else 1
    return i + 1


def _mask_strings(expr: str) -> str:
    """Return ``expr`` with the contents of its string and template literals replaced by ``_``."""
    masked = []
    i = 0
    while i < len(expr):
        if expr[i] in "'\"`":
            end = _skip_string(expr, i) if expr[i] != "`" else _scan_template(expr, i, [])
            masked.append(expr[i] + "_" * (end - i - 2) + expr[i])
            i = end
        else:
            masked.append(expr[i])
            i += 1
    return "".join(masked)


def _top_level(masked: str) -> list[int]:
    """Return the indexes of ``masked`` that are outside any brackets."""
    indexes = []
    depth = 0
    for i, char in enumerate(masked):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0:
            indexes.append(i)
    return indexes


def _closing_paren(masked: str, start: int) -> int:
    """Return the index of the bracket closing the one at ``start``."""
    depth = 0
    for i in range(start, len(masked)):
        depth += {"(": 1, "[": 1, "{": 1, ")": -1, "]": -1, "}": -1}.get(masked[i], 0)
        if depth == 0:
            return i
    return -1


def _ternary(masked: str) -> tuple[int, int] | None:
    """Return the indexes of the top-level ``?`` and its ``:``, or None when not a ternary."""
    question = None
    nested = 0
    for i in _top_level(masked):
        if masked[i] == "?":
            # ?? and ?. are not the conditional operator.
            if masked[i + 1 : i + 2] in ("?", ".") or masked[i - 1 : i] == "?":
                continue
            if question is None:
                question = i
            else:
                nested += 1
        elif masked[i] == ":" and question is not None:
            if not nested:
                return question, i
            nested -= 1
    return None


def _split_operands(masked: str) -> list[tuple[int, int]]:
    """Split ``masked`` at its top-level ``||``, ``??`` and ``+`` into (begin, end) ranges."""
    parts = []
    begin = 0
    top = set(_top_level(masked))
    i = 0
    while i < len(masked):
        if i in top and masked[i : i + 2] in ("||", "??"):
            parts.append((begin, i))
            begin = i = i + 2
        elif i in top and masked[i] == "+":
            parts.append((begin, i))
            begin = i = i + 1
        else:
            i += 1
    parts.append((begin, len(masked)))
    return parts
