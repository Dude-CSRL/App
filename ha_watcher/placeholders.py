"""Recipe placeholder syntax (finalized in Phase 3). See docs/profile-schema.md.

A placeholder is `{token}` where token is an identifier (`area`, `name`, ...), `entity:<domain>`,
or a digit (`{1}`). Anything else in braces (for example Home Assistant Jinja `{{ states('x') }}`)
is literal text and is never touched.
"""
from __future__ import annotations

import re
import unicodedata

TOKEN_RE = re.compile(r"\{([a-z_][a-z0-9_]*(?::[a-z0-9_]+)?|\d+)\}")

# Allowed placeholders per recipe field.
NAME_ALLOWED = frozenset({"area", "area_name", "manufacturer", "model"})
ID_ALLOWED = frozenset({"domain", "object_id", "area", "area_name", "name", "device_name",
                        "manufacturer", "model"})  # plus {1}, {2}, ... (captures of * and ? in `from`)
CARD_ALLOWED = frozenset({"area", "area_name", "name", "device_name", "manufacturer", "model",
                          "device_id"})  # plus {entity:<domain>}


class PlaceholderError(Exception):
    def __init__(self, token: str, kind: str):
        super().__init__(f"{kind} placeholder {{{token}}}")
        self.token, self.kind = token, kind  # kind: "unknown" | "unresolved"


def slugify(value) -> str:
    s = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text)


def is_allowed(token: str, allowed: frozenset, allow_digits: bool = False, allow_entity: bool = False) -> bool:
    if token in allowed:
        return True
    if allow_digits and token.isdigit():
        return True
    if allow_entity and token.startswith("entity:") and ":" not in token[len("entity:"):]:
        return True
    return False


def render(text: str, values: dict, allowed: frozenset, *, allow_digits: bool = False,
           allow_entity: bool = False, slug_values: bool = False) -> str:
    """Substitute placeholders. Raises PlaceholderError (unknown token, or known but no value)."""
    def sub(m: re.Match) -> str:
        tok = m.group(1)
        if not is_allowed(tok, allowed, allow_digits, allow_entity):
            raise PlaceholderError(tok, "unknown")
        val = values.get(tok)
        if val is None:
            raise PlaceholderError(tok, "unresolved")
        return slugify(val) if slug_values else str(val)
    return TOKEN_RE.sub(sub, text)


def validate(text: str, allowed: frozenset, *, max_capture: int | None = None,
             allow_entity: bool = False) -> list[str]:
    """Return human-readable problems for a template string (empty list if fine)."""
    problems = []
    for tok in tokens(text):
        if tok.isdigit():
            if max_capture is None:
                problems.append(f"{{{tok}}} is not allowed here")
            elif not 1 <= int(tok) <= max_capture:
                problems.append(f"{{{tok}}} refers to a wildcard that 'from' does not have "
                                f"('from' has {max_capture})")
        elif not is_allowed(tok, allowed, False, allow_entity):
            problems.append(f"unknown placeholder {{{tok}}}")
    return problems


def walk_strings(obj):
    """Yield every string leaf of a nested dict/list."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_strings(v)


def render_deep(obj, render_str):
    """Apply render_str to every string leaf, returning a new structure."""
    if isinstance(obj, str):
        return render_str(obj)
    if isinstance(obj, dict):
        return {k: render_deep(v, render_str) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render_deep(v, render_str) for v in obj]
    return obj
