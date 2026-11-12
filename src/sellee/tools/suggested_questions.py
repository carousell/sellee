"""suggested_questions: the common buyer questions to offer when a listing is confirmed, read
from a packaged file keyed by item-policy category token. A category with no list gets none."""

from __future__ import annotations

import json
from functools import lru_cache

from sellee.paths import PACKAGE_DATA_DIR
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)

QUESTIONS_PATH = PACKAGE_DATA_DIR / "suggested_questions.json"
MAX_SUGGESTED = 3


@lru_cache(maxsize=1)
def questions_by_category() -> dict:
    return json.loads(QUESTIONS_PATH.read_text())


def _suggested_questions(ctx: ToolContext, params: dict) -> dict:
    item_id = params["item_id"]
    item = ctx.store.get_item(item_id)
    if item is None:
        raise ToolError(f"no item with id {item_id!r}")
    candidates = questions_by_category().get(item.get("category") or "", [])
    answered = ctx.store.qa_questions(item_id)
    questions = [q for q in candidates if q not in answered][:MAX_SUGGESTED]
    return {"item_id": item_id, "questions": questions}


register(
    ToolSpec(
        name="suggested_questions",
        description="The questions buyers commonly ask about this item's category, to offer the "
        "seller as optional when confirming a new listing: at most 3, leaving out any already "
        "banked word for word, for this item or for everything the seller sells. Empty means say "
        "nothing about them. Drop one that plainly doesn't fit the item (a battery question for a "
        "wired speaker). Bank each answer the seller gives with add_qa_entry on this item, "
        "on_listing true, the question word for word as returned and the answer as they said it, "
        "never one you read off the photos. A skipped question is not banked.",
        input_schema={
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
            "additionalProperties": False,
        },
        handler=_suggested_questions,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_CHANNEL}),
    )
)
