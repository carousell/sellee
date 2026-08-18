"""The deterministic installer: setup's voice, preflight gates, layout materialization, update.

No LLM anywhere in this package — install and update are machine steps a person watches in one
terminal. The modules split by concern: `ui` owns everything printed, `preflight` answers
machine questions, `materialize` owns the versioned layout, `update` owns fetch/verify/swap.
"""
