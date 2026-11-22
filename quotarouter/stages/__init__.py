"""Stage registry — the single list that decides which vendors exist.

`build_stages()` returns fresh instances keyed by name; the router asks for a stage only
when the configured order names it, so an unused vendor costs nothing.
"""

from __future__ import annotations

from .base import Capability, Stage  # noqa: F401  (re-exported for adapters/tests)
from .deepseek import DeepSeekStage
from .exa import ExaStage
from .firecrawl import FirecrawlStage
from .jina import JinaStage
from .keenable import KeenableStage
from .keyless import KeylessStage
from .tavily import TavilyStage

STAGE_CLASSES = {
    "exa": ExaStage,
    "firecrawl": FirecrawlStage,
    "tavily": TavilyStage,
    "keenable": KeenableStage,
    "deepseek": DeepSeekStage,
    "jina": JinaStage,
    "keyless": KeylessStage,
}


def build_stages() -> dict[str, Stage]:
    return {name: cls() for name, cls in STAGE_CLASSES.items()}


__all__ = ["STAGE_CLASSES", "build_stages", "Stage", "Capability"]
