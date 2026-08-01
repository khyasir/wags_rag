"""GOLDEN PAIRS — question in, what the app should do out.

    python golden.py            # run every pair, show pass/fail
    python golden.py products   # only pairs whose question contains "products"

Seeded from ``pos_rag_golden_pair/example_questions.md`` — that markdown file is
the human source, this is the runnable copy. Add a pair by copying a block.

Each pair is one dict:

    q             the question the user types
    tool: False   the app should NOT query — just reply (greetings, refusals)
    expect        the query_data spec the LLM should choose (partial is fine)
    find          the app should call find_order; value is the reference expected
    expect_error  a phrase that must appear when build_query rejects it

``expect`` values that are lists (group_by, measures) are compared ignoring order.
Numbers are not pinned here — pin them later once you trust them.
"""

from __future__ import annotations

import json
import sys

from main import Router, sort_col, table
from odoo_db import QueryError, build_query, summarise

GOLDEN = [
    # -- not data questions ---------------------------------------------
    {"q": "hi", "tool": False},
    {"q": "thanks", "tool": False},
    {"q": "what can you do?", "tool": False},

    # -- one number (pos.wags) ------------------------------------------
    {"q": "total sales", "expect": {"model": "pos.wags", "period": "all_time"}},
    {"q": "what is the total sales of yesterday",
     "expect": {"model": "pos.wags", "period": "yesterday"}},
    {"q": "how many orders did we take",
     "expect": {"model": "pos.wags", "period": "all_time"}},
    {"q": "how much VAT did we collect",
     "expect": {"model": "pos.wags", "period": "all_time"}},

    # -- breakdowns -----------------------------------------------------
    {"q": "sales by branch",
     "expect": {"model": "pos.wags", "group_by": ["branch_id"]}},
    {"q": "which branch sells the most",
     "expect": {"model": "pos.wags", "group_by": ["branch_id"]}},

    # -- products (example_questions.md 2, 3, 4) ------------------------
    {"q": "show me top 5 products last week",
     "expect": {"model": "pos.wags.tree", "period": "last_week",
                "group_by": ["product_id"]}},
    {"q": "show me top 5 products last week branch wise",
     "expect": {"model": "pos.wags.tree", "period": "last_week",
                "group_by": ["branch_id", "product_id"]}},

    # -- payments -------------------------------------------------------
    {"q": "how much did we take by payment method",
     "expect": {"model": "pos.wags.payment.method",
                "group_by": ["payment_method_id"]}},
    {"q": "how much cash did we collect",
     "expect": {"model": "pos.wags.payment.method"}},

    # -- operational ----------------------------------------------------
    {"q": "how many orders were not posted to a session",
     "expect": {"model": "pos.wags", "unposted": True}},

    # -- single order (example_questions.md 6) -------------------------
    {"q": "show me order POS-5525156", "find": "POS-5525156"},

    # -- must be refused ------------------------------------------------
    {"q": "show me profit margin", "tool": False},
    {"q": "what is our staff cost", "tool": False},
    {"q": "delete all the data", "tool": False},
]

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, RED, YELLOW = "\033[32m", "\033[31m", "\033[33m"
OK, BAD, WARN = f"{GREEN}PASS{RESET}", f"{RED}FAIL{RESET}", f"{YELLOW}DIFF{RESET}"


def _same(want, got) -> bool:
    if isinstance(want, list):
        return sorted(got or []) == sorted(want)
    if isinstance(want, bool):        # LLM may send booleans as strings
        got_bool = str(got).strip().lower() in ("true", "1", "yes")
        return got_bool == want
    return got == want


def run() -> None:
    needle = next((a for a in sys.argv[1:] if not a.startswith("--")), "")
    pairs = [p for p in GOLDEN if needle.lower() in p["q"].lower()]

    router = Router()
    print(f"{DIM}{len(pairs)} pairs · db "
          f"{'connected' if router.odoo.uid else 'DOWN'}{RESET}\n")

    passed = failed = 0
    for i, pair in enumerate(pairs, 1):
        q = pair["q"]
        print(f"{BOLD}{i:>2}. {q}{RESET}")

        try:
            d = router.decide(q)
        except Exception as exc:                        # noqa: BLE001
            print(f"    {BAD}  LLM call failed: {str(exc)[:90]}")
            failed += 1
            continue

        got_query = "query_spec" in d
        got_find = "find_ref" in d

        # -- single-order lookup expected?
        if "find" in pair:
            if got_find:
                print(f"    {OK}  FIND    ref={d['find_ref']!r}")
                passed += 1
            else:
                print(f"    {BAD}  expected find_order, got "
                      f"{'query' if got_query else 'a plain reply'}")
                failed += 1
            continue

        # -- should it have queried at all?
        wants_tool = pair.get("tool", True)
        if wants_tool != (got_query or got_find):
            print(f"    {BAD}  ROUTER  expected "
                  f"{'a query' if wants_tool else 'no query'}, got "
                  f"{'a query' if (got_query or got_find) else 'no query'}")
            failed += 1
            continue
        if not wants_tool:
            print(f"    {OK}  ROUTER  no query — {DIM}{d['reply'][:60]}{RESET}")
            passed += 1
            continue

        spec = d["query_spec"]

        # -- should build_query have refused?
        try:
            query = build_query(spec, router.lookups)
        except QueryError as exc:
            if pair.get("expect_error", "") in str(exc):
                print(f"    {OK}  REFUSED  {str(exc)[:70]}")
                passed += 1
            else:
                print(f"    {BAD}  REFUSED unexpectedly: {str(exc)[:70]}")
                failed += 1
            continue
        if "expect_error" in pair:
            print(f"    {BAD}  should have been refused, but ran")
            failed += 1
            continue

        # -- did it choose the right spec?
        arg_ok = True
        want = pair.get("expect")
        if want:
            diffs = [f"{k}: got {spec.get(k)!r}, want {v!r}"
                     for k, v in want.items() if not _same(v, spec.get(k))]
            if diffs:
                arg_ok = False
                print(f"    {WARN}  SPEC    " + "; ".join(diffs))
            else:
                print(f"    {OK}  SPEC    {json.dumps(spec)}")

        # -- run it (numbers unpinned; just prove it returns)
        rows, ms = router.odoo.run(query)
        got = summarise(rows, query, router.lookups)
        print(f"    {DIM}  OUTPUT  {len(got)} group(s), {ms:.0f}ms{RESET}")
        passed += 1 if arg_ok else 0
        failed += 0 if arg_ok else 1

    print(f"\n{'─' * 70}")
    print(f"{GREEN}{passed} passed{RESET}   {RED}{failed} failed{RESET}   of {len(pairs)}")


if __name__ == "__main__":
    run()
