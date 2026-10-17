"""Device profile loading and validation (Phase 2). See docs/profile-schema.md.

Profiles are YAML files in a profiles directory. Loading never raises for bad files:
every problem is collected in `LoadResult.errors` so the watcher can report it and keep
running. The `recipe` section is only parsed/validated here; it is executed in Phase 3.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import placeholders as ph

STATUSES = ("draft", "active")
TOP_KEYS = {"name", "description", "status", "match", "recipe", "source"}
MATCH_KEYS = {"manufacturer", "model", "entity_patterns"}
RECIPE_KEYS = {"name", "area", "entity_id_cleanup", "dashboard_card", "welcome_automation"}
DASHBOARD_KEYS = {"dashboard", "view", "card"}
CLEANUP_KEYS = {"from", "to"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class Profile:
    name: str
    file: str
    status: str
    manufacturer: str | None
    model: str | None
    entity_patterns: tuple[str, ...] = ()
    recipe: dict = field(default_factory=dict)
    description: str | None = None

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class ProfileError:
    file: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}: {self.message}"


@dataclass
class LoadResult:
    directory: str | None
    profiles: list[Profile] = field(default_factory=list)
    errors: list[ProfileError] = field(default_factory=list)
    warnings: list[ProfileError] = field(default_factory=list)  # loaded, but a recipe step will not run as written
    directory_found: bool = False

    @property
    def active(self) -> list[Profile]:
        return [p for p in self.profiles if p.active]

    @property
    def drafts(self) -> list[Profile]:
        return [p for p in self.profiles if not p.active]


def _is_str(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _opt_str(v, label: str, problems: list[str]) -> None:
    if v is not None and not _is_str(v):
        problems.append(f"{label} must be a non-empty string or null")


def _check_recipe(recipe: Any, problems: list[str]) -> dict:
    if recipe is None:
        return {}
    if not isinstance(recipe, dict):
        problems.append("recipe must be a mapping")
        return {}
    for k in sorted(set(recipe) - RECIPE_KEYS, key=str):
        problems.append(f"recipe: unknown key {k!r} (allowed: {', '.join(sorted(RECIPE_KEYS))})")
    _opt_str(recipe.get("name"), "recipe.name", problems)
    _opt_str(recipe.get("area"), "recipe.area", problems)
    _opt_str(recipe.get("welcome_automation"), "recipe.welcome_automation", problems)

    cleanup = recipe.get("entity_id_cleanup")
    if cleanup is not None:
        if not isinstance(cleanup, list):
            problems.append("recipe.entity_id_cleanup must be a list of {from, to} rules")
        else:
            for i, rule in enumerate(cleanup):
                if not isinstance(rule, dict) or set(rule) != CLEANUP_KEYS \
                        or not _is_str(rule.get("from")) or not _is_str(rule.get("to")):
                    problems.append(f"recipe.entity_id_cleanup[{i}] must be a mapping with string keys 'from' and 'to'")

    card = recipe.get("dashboard_card")
    if card is not None:
        if not isinstance(card, dict):
            problems.append("recipe.dashboard_card must be a mapping")
        else:
            for k in sorted(set(card) - DASHBOARD_KEYS, key=str):
                problems.append(f"recipe.dashboard_card: unknown key {k!r}")
            _opt_str(card.get("dashboard"), "recipe.dashboard_card.dashboard", problems)
            _opt_str(card.get("view"), "recipe.dashboard_card.view", problems)
            if card.get("card") is not None and not isinstance(card.get("card"), dict):
                problems.append("recipe.dashboard_card.card must be a mapping (a Lovelace card)")
    return dict(recipe)


def validate_profile(data: Any, file: str) -> tuple[Profile | None, list[str]]:
    """Return (profile, problems). profile is None when there are any problems."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return None, ["top level must be a mapping"]
    for k in sorted(set(data) - TOP_KEYS, key=str):
        problems.append(f"unknown top-level key {k!r} (allowed: {', '.join(sorted(TOP_KEYS))})")

    stem = Path(file).stem
    name = data.get("name", stem)
    if not isinstance(name, str) or not NAME_RE.match(name):
        problems.append(f"name must match {NAME_RE.pattern} (default: file name without extension)")
    status = data.get("status")
    if status not in STATUSES:
        problems.append(f"status is required and must be one of {list(STATUSES)}")
    desc = data.get("description")
    if desc is not None and not isinstance(desc, str):
        problems.append("description must be a string")
    if "source" in data and data["source"] is not None and not isinstance(data["source"], dict):
        problems.append("source must be a mapping")

    match = data.get("match")
    manufacturer = model = None
    patterns: tuple[str, ...] = ()
    if not isinstance(match, dict):
        problems.append("match is required and must be a mapping with manufacturer and model")
    else:
        for k in sorted(set(match) - MATCH_KEYS, key=str):
            problems.append(f"match: unknown key {k!r} (allowed: {', '.join(sorted(MATCH_KEYS))})")
        manufacturer, model = match.get("manufacturer"), match.get("model")
        for label, v in (("manufacturer", manufacturer), ("model", model)):
            if v is None:
                # draft stubs generated from partial captures may leave these null
                if status != "draft":
                    problems.append(f"match.{label} is required for non-draft profiles")
            elif not _is_str(v):
                problems.append(f"match.{label} must be a non-empty string")
        ep = match.get("entity_patterns")
        if ep is not None:
            if not isinstance(ep, list) or not all(_is_str(x) for x in ep):
                problems.append("match.entity_patterns must be a list of non-empty glob strings")
            else:
                patterns = tuple(ep)

    recipe = _check_recipe(data.get("recipe"), problems)
    if problems:
        return None, problems
    return Profile(name=name, file=file, status=status, manufacturer=manufacturer, model=model,
                   entity_patterns=patterns, recipe=recipe, description=desc), []


def _wildcards(pattern: str) -> int:
    return pattern.count("*") + pattern.count("?")


def recipe_warnings(recipe: dict) -> list[str]:
    """Placeholder problems in a recipe. Warnings, not errors: the profile still loads and
    matches; the affected recipe step is skipped at provision time with the same message."""
    out: list[str] = []
    name = recipe.get("name")
    if isinstance(name, str):
        out += [f"recipe.name: {m}" for m in ph.validate(name, ph.NAME_ALLOWED)]
    for i, rule in enumerate(recipe.get("entity_id_cleanup") or []):
        if not isinstance(rule, dict):
            continue
        to, frm = rule.get("to"), rule.get("from")
        if isinstance(to, str) and isinstance(frm, str):
            out += [f"recipe.entity_id_cleanup[{i}].to: {m}"
                    for m in ph.validate(to, ph.ID_ALLOWED, max_capture=_wildcards(frm))]
            if "*" in to or "?" in to:
                out.append(f"recipe.entity_id_cleanup[{i}].to: wildcards are not supported in 'to'; "
                           "use {1}, {2}, ... for what the wildcards in 'from' matched")
    card = recipe.get("dashboard_card")
    if isinstance(card, dict):
        for text in ph.walk_strings(card.get("card")):
            out += [f"recipe.dashboard_card.card: {m}"
                    for m in ph.validate(text, ph.CARD_ALLOWED, allow_entity=True)]
    return out


def load_profiles(profiles_dir: str | os.PathLike | None) -> LoadResult:
    """Load every *.yaml / *.yml in profiles_dir. Never raises for bad files."""
    res = LoadResult(directory=str(profiles_dir) if profiles_dir else None)
    if not profiles_dir or not os.path.isdir(profiles_dir):
        return res
    res.directory_found = True
    files = sorted(p for p in Path(profiles_dir).iterdir()
                   if p.is_file() and p.suffix.lower() in (".yaml", ".yml"))
    names: dict[str, str] = {}
    for path in files:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            first = str(exc).strip().splitlines()[0] if str(exc).strip() else "invalid YAML"
            res.errors.append(ProfileError(path.name, f"YAML syntax error: {first}"))
            continue
        except (OSError, UnicodeDecodeError) as exc:
            res.errors.append(ProfileError(path.name, f"cannot read file: {exc}"))
            continue
        if data is None:
            res.errors.append(ProfileError(path.name, "file is empty"))
            continue
        profile, problems = validate_profile(data, path.name)
        if profile is None:
            res.errors.append(ProfileError(path.name, "; ".join(problems)))
            continue
        if profile.name in names:
            res.errors.append(ProfileError(
                path.name, f"duplicate profile name {profile.name!r} (already used by {names[profile.name]})"))
            continue
        names[profile.name] = path.name
        res.profiles.append(profile)
        for w in recipe_warnings(profile.recipe):
            res.warnings.append(ProfileError(path.name, w))
    return res
