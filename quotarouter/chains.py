"""Named chains: what the operator wrote down, parsed once, validated once.

A chain is a *pair* of stage lists, not one list. The vendor that is cheapest for a query is not
the cheapest for a page (a search costs two Firecrawl credits, a page costs one, and Jina serves
pages for nothing), so `search` and `extract` are declared separately. A chain may also carry a
description, a budget and a strictness flag.

The file is TOML because Python 3.11 parses it in the standard library: a configuration format
should not be the reason to add a dependency.

    [chains.free]
    description = "Nothing that can cost money"
    search  = ["keenable", "exa", "firecrawl"]
    extract = ["jina", "firecrawl"]
    budget  = "free"

Everything here is pure: a mapping in, immutable records out, no environment reads of its own.
The caller passes the resolver, so this module never has to know whether the setting came from
the process environment, a dotenv file or a host's own configuration.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

#: The setting that names an alternative file, and the setting that carries a one-line shorthand.
#: The names callers pass to the resolver, which owns the ``QUOTAROUTER_`` prefix (see
#: ``config._env``). The full variable names appear only in messages.
SHORTHAND_SETTING = "CHAINS"
FILE_SETTING = "CHAINS_FILE"
#: Only for messages: a reader of the log needs the variable it can actually set.
SHORTHAND_ENV = "QUOTAROUTER_" + SHORTHAND_SETTING
DEFAULT_FILE = "~/.config/quotarouter/chains.toml"

#: Accepted chain-level budget values. `free` means "this chain does not spend money"; the
#: absence of a budget means "no restriction beyond the ledger".
BUDGET_FREE = "free"
BUDGETS = (BUDGET_FREE,)

ALLOWED_KEYS = frozenset({"description", "search", "extract", "budget", "max_cost_usd", "strict"})
CAPABILITIES = ("search", "extract")


@dataclass(frozen=True)
class ChainSpec:
    """One named chain, as the operator declared it."""

    name: str
    search: tuple[str, ...] = ()
    extract: tuple[str, ...] = ()
    description: str = ""
    budget: Optional[str] = None
    max_cost_usd: Optional[float] = None
    #: A non-strict chain continues into the default order once its own stages are exhausted.
    #: Strict is the default: a name that silently grows a tail is not a name.
    strict: bool = True

    def stages_for(self, capability: str) -> tuple[str, ...]:
        return self.search if capability == "search" else self.extract

    def declares(self, capability: str) -> bool:
        return bool(self.stages_for(capability))

    def capabilities(self) -> tuple[str, ...]:
        return tuple(c for c in CAPABILITIES if self.declares(c))


@dataclass(frozen=True)
class ChainProblem:
    """One entry that did not survive parsing, and why. Reported, never fatal."""

    entry: str
    reason: str

    def as_line(self) -> str:
        return f"{self.entry}: {self.reason}"


@dataclass(frozen=True)
class ChainSet:
    """Every chain this process knows about, plus what was dropped on the way in."""

    specs: Mapping[str, ChainSpec]
    problems: tuple[ChainProblem, ...] = ()
    #: `file`, `shorthand` or `none` — the audit surface says which one was in force.
    source: str = "none"
    path: Optional[str] = None

    def names(self) -> tuple[str, ...]:
        return tuple(self.specs)

    def get(self, name: str) -> Optional[ChainSpec]:
        return self.specs.get(name)

    def as_dict(self) -> dict[str, dict[str, Any]]:
        """Machine-readable form for `status --json` and the protocol's status tool."""
        return {
            name: {
                "search": list(spec.search),
                "extract": list(spec.extract),
                "description": spec.description,
                "budget": spec.budget,
                "max_cost_usd": spec.max_cost_usd,
                "strict": spec.strict,
            }
            for name, spec in self.specs.items()
        }


def _stage_list(value: Any, known: Iterable[str], where: str) -> tuple[tuple[str, ...], Optional[str]]:
    """Normalise one stage list. Returns `(names, problem)` — at most one problem per list."""
    if not isinstance(value, list):
        return (), f"{where} must be a list of stage names"
    known_set = set(known)
    names: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            return (), f"{where} contains a non-string entry"
        name = raw.strip().lower()
        if name not in known_set:
            return (), (f"{where} names {name!r}, which is not a stage this build has "
                        f"({', '.join(sorted(known_set))})")
        if name not in names:  # a duplicate would dial the same vendor twice
            names.append(name)
    if not names:
        return (), f"{where} is empty"
    return tuple(names), None


def _parse_entry(name: str, table: Any, known: Iterable[str]) -> tuple[Optional[ChainSpec], Optional[str]]:
    if not isinstance(table, dict):
        return None, "a chain must be a table"
    unknown = sorted(set(table) - ALLOWED_KEYS)
    if unknown:
        return None, f"unknown key(s) {', '.join(unknown)}; allowed: {', '.join(sorted(ALLOWED_KEYS))}"

    lists: dict[str, tuple[str, ...]] = {}
    for capability in CAPABILITIES:
        if capability not in table:
            continue
        names, problem = _stage_list(table[capability], known, capability)
        if problem:
            return None, problem
        lists[capability] = names
    if not lists:
        return None, "declares neither search nor extract stages"

    description = table.get("description", "")
    if not isinstance(description, str):
        return None, "description must be a string"

    budget = table.get("budget")
    if budget is not None:
        if not isinstance(budget, str) or budget.strip().lower() not in BUDGETS:
            return None, f"budget must be one of {', '.join(BUDGETS)} (got {budget!r})"
        budget = budget.strip().lower()

    ceiling = table.get("max_cost_usd")
    if ceiling is not None:
        if isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)) or ceiling < 0:
            return None, f"max_cost_usd must be a non-negative number (got {ceiling!r})"
        ceiling = float(ceiling)

    strict = table.get("strict", True)
    if not isinstance(strict, bool):
        return None, f"strict must be true or false (got {strict!r})"

    return ChainSpec(
        name=name,
        search=lists.get("search", ()),
        extract=lists.get("extract", ()),
        description=description,
        budget=budget,
        max_cost_usd=ceiling,
        strict=strict,
    ), None


def parse_document(data: Mapping[str, Any], known: Iterable[str]) -> ChainSet:
    """Turn a parsed TOML document into chains, keeping every valid entry.

    A configuration typo must never stop a service, and must never be silent either: the entry is
    dropped with a reason that names it, and the rest of the file still works.
    """
    specs: dict[str, ChainSpec] = {}
    problems: list[ChainProblem] = []

    table = data.get("chains")
    if table is None:
        return ChainSet({}, (ChainProblem("chains", "the file has no [chains.*] tables"),), "file")
    if not isinstance(table, dict):
        return ChainSet({}, (ChainProblem("chains", "[chains] must be a table of tables"),), "file")

    for name, entry in table.items():
        spec, problem = _parse_entry(str(name), entry, known)
        if spec is None:
            problems.append(ChainProblem(f"chains.{name}", problem or "invalid"))
            continue
        specs[str(name)] = spec
    return ChainSet(specs, tuple(problems), "file")


def parse_shorthand(value: str, known: Iterable[str]) -> ChainSet:
    """One line, one list per name, applied to both capabilities.

    Kept for a throwaway setup (`QUOTAROUTER_CHAINS="fast=exa,firecrawl"`). It cannot express a
    pair of lists, so it does not try: the same list is offered to search and extract, and a stage
    that cannot serve a capability is refused at call time like any other capability mismatch.
    """
    specs: dict[str, ChainSpec] = {}
    problems: list[ChainProblem] = []
    for segment in (part.strip() for part in value.split(";")):
        if not segment:
            continue
        name, sep, raw_names = segment.partition("=")
        name = name.strip().lower()
        if not sep or not name or not raw_names.strip():
            problems.append(ChainProblem(segment, "expected name=stage,stage"))
            continue
        names, problem = _stage_list(
            [part.strip() for part in raw_names.split(",") if part.strip()], known, "stages")
        if problem:
            problems.append(ChainProblem(name, problem))
            continue
        specs[name] = ChainSpec(name=name, search=names, extract=names)
    return ChainSet(specs, tuple(problems), "shorthand")


def load(resolve: Callable[[str], str], known: Iterable[str],
         path: Optional[Path] = None) -> ChainSet:
    """Read the chains this process will use: the shorthand if set, else the file, else nothing.

    `resolve` reads a setting by name (the caller owns the environment and the dotenv fallback).
    A missing file is not an error — it is the normal state of a fresh install. An unreadable or
    unparsable one is reported once and leaves the default order in charge: a configuration
    problem must not be an outage.
    """
    shorthand = (resolve(SHORTHAND_SETTING) or "").strip()
    configured_path = path or Path(resolve(FILE_SETTING) or DEFAULT_FILE).expanduser()
    if shorthand:
        if configured_path.exists():
            logger.warning(
                "quotarouter: %s is set, so %s was ignored — the shorthand replaces the file",
                SHORTHAND_ENV, configured_path,
            )
        loaded = parse_shorthand(shorthand, known)
        return ChainSet(loaded.specs, loaded.problems, "shorthand", str(configured_path))

    if not configured_path.exists():
        return ChainSet({}, (), "none", str(configured_path))
    try:
        text = configured_path.read_text(encoding="utf-8")
    except OSError as exc:
        reason = "permission denied" if isinstance(exc, PermissionError) else str(exc)
        logger.warning("quotarouter: cannot read %s (%s); using the default order",
                       configured_path, reason)
        return ChainSet({}, (ChainProblem(str(configured_path), f"unreadable: {reason}"),),
                        "file", str(configured_path))
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        logger.warning("quotarouter: %s is not valid TOML (%s); using the default order",
                       configured_path, exc)
        return ChainSet({}, (ChainProblem(str(configured_path), f"invalid TOML: {exc}"),),
                        "file", str(configured_path))

    loaded = parse_document(data, known)
    for problem in loaded.problems:
        logger.warning("quotarouter: chain %s — %s", problem.entry, problem.reason)
    return ChainSet(loaded.specs, loaded.problems, "file", str(configured_path))
