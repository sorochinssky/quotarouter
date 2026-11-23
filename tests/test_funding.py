"""Funding: which stage would spend money, and which would only spend an allowance.

The router never guesses this. "Has quota left" and "has free quota left" are different questions,
and only the stage can answer the second one, so every stage declares `funding` and the budget
filter reads that declaration instead of inferring it from a price.
"""

from __future__ import annotations

from quotarouter.stages import STAGE_CLASSES
from quotarouter.stages.base import Stage

FUNDING_KINDS = {"free", "balance"}


def test_every_stage_declares_a_funding_kind():
    for name, cls in STAGE_CLASSES.items():
        declared = getattr(cls, "funding", None)
        assert declared in FUNDING_KINDS, f"{name} declares funding={declared!r}"


def test_the_default_is_free_and_deepseek_is_the_one_that_spends_money():
    assert Stage.funding == "free", "a stage that says nothing must not be entered under a free budget"
    spending = {name for name, cls in STAGE_CLASSES.items() if cls.funding == "balance"}
    assert spending == {"deepseek"}, (
        "deepseek is prepaid money; if another stage starts billing real money it must say so here"
    )
