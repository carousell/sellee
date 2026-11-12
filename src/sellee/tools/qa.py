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
from sellee.tools import revise
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_REPLY,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)
from sellee.tools.suggested_questions import questions_by_category


def _search_qa_bank(ctx: ToolContext, params: dict) -> dict:
    entries = ctx.store.qa_search(params["item_id"], params.get("query"))
    return {"entries": entries, "count": len(entries)}


def _require_an_offered_question(item: dict | None, question: str) -> None:
    # Only a suggested question goes on the listing; an escalated buyer question stays in the bank.
    offered = questions_by_category().get((item or {}).get("category") or "", [])
    if question.strip() not in offered:
        if not offered:
            raise ToolError(
                "on_listing is only for a question suggested_questions offered for this item, "
                "and it offered none; bank the answer without it"
            )
        raise ToolError(
            "on_listing is only for a question suggested_questions offered for this item, word "
            f"for word: retry with one of {', '.join(map(repr, offered))}. Bank an answer to "
            "any other question without it"
        )


def _add_qa_entry(ctx: ToolContext, params: dict) -> dict:
    item_id, question = params["item_id"], params["question"]
    on_listing = bool(params.get("on_listing"))
    item = ctx.store.get_item(item_id) if item_id != QA_GLOBAL_ITEM else None
    # A new answer to a question the listing already shows replaces it there, flag or no flag.
    shown = item is not None and any(
        row["question"] == question.strip() for row in ctx.store.qa_listing_answers(item_id)
    )
    if on_listing and not shown and item_id != QA_GLOBAL_ITEM:
        _require_an_offered_question(item, question)
    on_listing = on_listing or shown
    live = on_listing and item is not None and revise.is_live(ctx, item)
    if live and ctx.store.is_paused():
        raise ToolError("the agent is paused — resume before changing an answer on a listing")
    try:
        entry = ctx.store.qa_add(
            item_id, question, params["answer"], params["source"], on_listing=on_listing
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    if live:
        entry["markets"] = revise.revise_live_listings(
            ctx, ctx.store.get_item(item_id), ["description"]
        )
    return entry


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
        "everything they sell. Only ever record what the seller actually said. on_listing true "
        "also shows the answer at the end of the item's listing description; set it only for a "
        "question suggested_questions offered. A new answer to a question the listing already "
        "shows replaces it there, flag or no flag. Either way, on an item already listed this "
        "updates every marketplace it is on: report `markets` exactly as update_live_listing's.",
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "question": {"type": "string"},
                "answer": {"type": "string"},
                "source": {"type": "string", "enum": ["seller"]},
                "on_listing": {"type": "boolean"},
            },
            "required": ["item_id", "question", "answer", "source"],
            "additionalProperties": False,
        },
        handler=_add_qa_entry,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_CHANNEL}),
    )
)
