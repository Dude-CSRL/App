"""Profile matching (Phase 2): minimal, deterministic, no rules engine.

Rules (see docs/profile-schema.md):
1. Only `status: active` profiles are considered; drafts are skipped.
2. manufacturer AND model must equal the device's, case-insensitive exact match
   (surrounding whitespace ignored).
3. If the profile lists `match.entity_patterns`, EVERY pattern (glob, case-insensitive)
   must match at least one of the device's entity IDs. If entity IDs are unknown
   (None), a profile with patterns does not match.
4. If several profiles match, the one with more entity patterns wins (more specific),
   then the earliest by file name.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Iterable

from .profiles import Profile


def _norm(value) -> str:
    return str(value).strip().casefold() if value is not None else ""


def model_candidates(device: dict | None, profiles: Iterable[Profile]) -> list[Profile]:
    """Active profiles whose manufacturer + model equal the device's (patterns not checked)."""
    if not device:
        return []
    mfr, model = _norm(device.get("manufacturer")), _norm(device.get("model"))
    if not mfr or not model:
        return []
    return [p for p in profiles
            if p.active and _norm(p.manufacturer) == mfr and _norm(p.model) == model]


def needs_entities(device: dict | None, profiles: Iterable[Profile]) -> bool:
    """True if matching this device may depend on its entity IDs."""
    return any(p.entity_patterns for p in model_candidates(device, profiles))


def patterns_match(patterns: Iterable[str], entity_ids: list[str] | None) -> bool:
    patterns = list(patterns)
    if not patterns:
        return True
    if entity_ids is None:
        return False
    ids = [_norm(e) for e in entity_ids]
    return all(any(fnmatchcase(e, _norm(pat)) for e in ids) for pat in patterns)


def match_device(device: dict | None, entity_ids: list[str] | None,
                 profiles: Iterable[Profile]) -> Profile | None:
    """Return the matching active profile, or None."""
    cands = model_candidates(device, profiles)
    cands.sort(key=lambda p: (-len(p.entity_patterns), p.file))
    for p in cands:
        if patterns_match(p.entity_patterns, entity_ids):
            return p
    return None
