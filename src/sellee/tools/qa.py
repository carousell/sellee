"""The Q&A bank: answers the seller has taught, so the same question is never escalated twice.

This is the compounding loop. A buyer asks something only the seller knows; the reply flow escalates
it; the seller answers on their own channel; that answer is banked, and every later buyer who asks
the same thing gets it immediately. So the seller's work goes down over time rather than staying
flat.

Search is deliberately dumb — a substring filter over question and answer, capped. The rows come
back for the model to match semantically, which it does better than any index we would write;
narrowing them here would only hide the entry that actually answers the question.

The two halves are tiered apart. Searching is what a reply pass does, so it has it. Banking is a
record of what the *seller* said, so it belongs to the flows that talk to the seller — a reply pass
handling a buyer's message must never be able to write into the bank it reads from.
"""

from __future__ import annotations

from sellee.store import QA_GLOBAL_ITEM, StoreError
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_REPLY,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)


def _search_qa_bank(ctx: ToolContext, params: dict) -> dict:
    entries = ctx.store.qa_search(params["item_id"], params.get("query"))
    return {"entries": entries, "count": len(entries)}


def _add_qa_entry(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.qa_add(
            params["item_id"], params["question"], params["answer"], params["source"]
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


register(
    ToolSpec(
        name="search_qa_bank",
        description="Look up answers the seller has already taught for this item, plus the ones "
        f"that apply to everything they sell. item_id {QA_GLOBAL_ITEM!r} means the global entries "
        "only. Returns the item's whole bank for you to match yourself — `query` only orders the "
        "rows, it never filters any out, so read them all before deciding. An empty result "
        "therefore means nothing is banked for this item, and only then ask the seller.",
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": ["item_id"],
            "additionalProperties": False,
        },
        handler=_search_qa_bank,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_REPLY, TIER_PASS_CHANNEL}),
    )
)
register(
    ToolSpec(
        name="add_qa_entry",
        description="Bank an answer the seller gave, so the same buyer question answers itself "
        f"next time. Scope it to one item, or use {QA_GLOBAL_ITEM!r} for something true of "
        "everything they sell. Only ever record what the seller actually said.",
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "question": {"type": "string"},
                "answer": {"type": "string"},
                "source": {"type": "string", "enum": ["seller"]},
            },
            "required": ["item_id", "question", "answer", "source"],
            "additionalProperties": False,
        },
        handler=_add_qa_entry,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_CHANNEL}),
    )
)
