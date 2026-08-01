"""GOLDEN PAIRS — question in, expected query and output out.

    python golden.py              # run every pair, show pass/fail
    python golden.py --show       # also print the full output of each
    python golden.py sales        # only pairs whose question contains "sales"

Each pair says what the LLM SHOULD choose and what the answer SHOULD look like.
Edit ``expect`` and ``output`` below — that is the point of this file. Whatever
shape you write into ``output`` becomes the target the code has to hit.

Two things are checked, separately, because they fail for different reasons:

    ARGS    did the LLM pick the right metric, period and grouping?
            wrong here = the prompt needs work
    OUTPUT  do the figures and their shape match what you wrote?
            wrong here = the query or the summarise step needs work

``output`` may be partial. Only the keys you write are compared, so you can pin
``net_sales`` and ignore everything else until you care about it.
"""

from __future__ import annotations

import json
import sys

from main import Router, table
from odoo_db import QueryError, build_query, summarise

# --------------------------------------------------------------------- pairs
#
# tool:   False means the LLM should NOT query — it should just reply.
# expect: the tool arguments it should choose.
# output: the figures that should come back. Partial is fine.
#         Set to None while you are still deciding the shape.

GOLDEN = [
    # ---------------------------------------------------- not data questions
    {"q": "hi", "tool": False},
    {"q": "thanks", "tool": False},
    {"q": "what can you do?", "tool": False},

    # ------------------------------------------------------------- one number
    {"q": "total sales",
     "expect": {"metric": "sales", "period": "all_time"},
     "output": {"overall": {"net_sales": 961654.64,
                            "gross_sales": 961654.64,
                            "returns": 0.0,
                            "orders": 318,
                            "average_ticket": 3024.07}}},

    {"q": "how many orders did we take",
     "expect": {"metric": "sales", "period": "all_time"},
     "output": {"overall": {"orders": 318}}},

    {"q": "what is the average ticket",
     "expect": {"metric": "sales", "period": "all_time"},
     "output": {"overall": {"average_ticket": 3024.07}}},

    {"q": "how much VAT did we collect",
     "expect": {"metric": "sales", "period": "all_time"},
     "output": {"overall": {"vat": 144218.53}}},

    {"q": "sales last month",
     "expect": {"metric": "sales", "period": "last_month"},
     "output": {"overall": {"net_sales": 539161.0, "orders": 55}}},

    {"q": "sales yesterday",
     "expect": {"metric": "sales", "period": "yesterday"},
     "output": {"overall": {"net_sales": 23422.0}}},

    {"q": "sales for december 2025",
     "expect": {"metric": "sales", "period": "custom",
                "start_date": "2025-12-01", "end_date": "2025-12-31"},
     "output": {"overall": {"net_sales": 411122.0, "orders": 45}}},

    # -------------------------------------------------------------- breakdowns
    {"q": "sales by month",
     "expect": {"metric": "sales", "period": "all_time", "group_by": ["month"]},
     "output": None},

    {"q": "which branch sells the most",
     "expect": {"metric": "sales", "period": "all_time", "group_by": ["branch"]},
     "output": {"RUH-TWN-A": {"net_sales": 960165.08, "orders": 251}}},

    {"q": "sales by order type",
     "expect": {"metric": "sales", "period": "all_time",
                "group_by": ["order_type"]},
     "output": None},

    # --------------------------------------------------------------- products
    {"q": "top selling products",
     "expect": {"metric": "products", "period": "all_time",
                "group_by": ["product"]},
     "output": {"Cappuccino": {"quantity": 115.0}}},

    {"q": "which extras do customers add most",
     "expect": {"metric": "modifiers", "period": "all_time",
                "group_by": ["product"]},
     "output": None},

    # --------------------------------------------------------------- payments
    {"q": "how much did we take by payment method",
     "expect": {"metric": "payments", "period": "all_time",
                "group_by": ["payment_method"]},
     "output": {"Mada": {"amount": 1108740.08}}},

    {"q": "how much cash did we collect",
     "expect": {"metric": "payments", "period": "all_time"},
     "output": None},

    # ------------------------------------------------------------ operational
    {"q": "how much came back as returns",
     "expect": {"metric": "returns", "period": "all_time"},
     "output": {"overall": {"amount_untaxed": 0.0, "records": 0}}},

    {"q": "total discounts given",
     "expect": {"metric": "discounts", "period": "all_time"},
     "output": {"overall": {"discount_amount": 197.79,
                            "coupon_amount": 0.0,
                            "voucher_amount": 0.0}}},

    {"q": "how many orders were missed in sales posting",
     "expect": {"metric": "unposted_orders", "period": "all_time"},
     "output": {"overall": {"records": 67}}},

    # -------------------------------------------------------- must be refused
    #
    # "profit margin" is not refused by the whitelist — the tool schema's enum
    # already stops the LLM naming an invalid metric, so it never gets that
    # far. The risk is the opposite one: the LLM quietly answering with `sales`
    # instead. So the correct behaviour is no tool call and a plain "I cannot
    # do that", which is what tool: False checks.
    {"q": "show me profit margin", "tool": False},
    {"q": "what is our staff cost", "tool": False},
    {"q": "top products per day", "expect_error": "carry no date"},
]

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, RED, YELLOW = "\033[32m", "\033[31m", "\033[33m"
OK, BAD, WARN = f"{GREEN}PASS{RESET}", f"{RED}FAIL{RESET}", f"{YELLOW}DIFF{RESET}"


def subset_matches(want: dict, got: dict) -> list:
    """Compare only the keys written in ``want``. Returns a list of problems."""
    problems = []
    for group, fields in want.items():
        if group not in got:
            problems.append(f"missing group {group!r}, got {list(got)[:4]}")
            continue
        for key, expected in fields.items():
            actual = got[group].get(key)
            if actual is None:
                problems.append(f"{group}.{key} missing")
            elif isinstance(expected, float):
                if abs(actual - expected) > 0.01:
                    problems.append(f"{group}.{key} = {actual}, want {expected}")
            elif actual != expected:
                problems.append(f"{group}.{key} = {actual}, want {expected}")
    return problems


def run() -> None:
    show = "--show" in sys.argv
    needle = next((a for a in sys.argv[1:] if not a.startswith("--")), "")
    pairs = [p for p in GOLDEN if needle.lower() in p["q"].lower()]

    router = Router()
    print(f"{DIM}{len(pairs)} pairs · db={router.odoo.uid and 'connected'}{RESET}\n")

    passed = failed = 0
    notes: list = []

    for i, pair in enumerate(pairs, 1):
        q = pair["q"]
        print(f"{BOLD}{i:>2}. {q}{RESET}")

        try:
            d = router.decide(q)
        except Exception as exc:                        # noqa: BLE001
            print(f"    {BAD}  LLM call failed: {str(exc)[:90]}")
            failed += 1
            continue

        # -- should it have queried at all?
        wants_tool = pair.get("tool", True)
        got_tool = "tool_args" in d
        if wants_tool != got_tool:
            print(f"    {BAD}  ROUTER  expected "
                  f"{'a query' if wants_tool else 'no query'}, "
                  f"got {'a query' if got_tool else 'no query'}")
            failed += 1
            continue
        if not wants_tool:
            print(f"    {OK}  ROUTER  no query, replied: "
                  f"{DIM}{d['reply'][:60]}{RESET}")
            passed += 1
            continue

        args = d["tool_args"]

        # -- should it have been refused?
        try:
            query = build_query(args, router.lookups)
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

        # -- did it choose the right arguments?
        want = pair.get("expect")
        arg_ok = True
        if want:
            diffs = [f"{k}: got {args.get(k)!r}, want {v!r}"
                     for k, v in want.items() if args.get(k) != v]
            extra = [k for k in args if k not in want]
            if diffs or extra:
                arg_ok = False
                if extra:
                    diffs.append(f"unasked: {extra}")
                print(f"    {WARN}  ARGS    " + "; ".join(diffs))
            else:
                print(f"    {OK}  ARGS    {json.dumps(args)}")

        # -- did the figures come back right?
        rows, ms = router.odoo.run(query)
        got = summarise(rows, query, router.lookups)

        if pair.get("output"):
            problems = subset_matches(pair["output"], got)
            if problems:
                print(f"    {BAD}  OUTPUT  " + "; ".join(problems[:3]))
                failed += 1
            else:
                print(f"    {OK}  OUTPUT  matches "
                      f"{DIM}({len(rows)} buckets, {ms:.0f}ms){RESET}")
                passed += 1 if arg_ok else 0
                failed += 0 if arg_ok else 1
        else:
            print(f"    {DIM}  OUTPUT  not pinned yet — "
                  f"{len(got)} group(s), {ms:.0f}ms{RESET}")
            notes.append(q)
            passed += 1 if arg_ok else 0
            failed += 0 if arg_ok else 1

        if show:
            for line in table(got, query.metric).splitlines():
                print(f"        {line}")

    print(f"\n{'─' * 70}")
    print(f"{GREEN}{passed} passed{RESET}   {RED}{failed} failed{RESET}   "
          f"of {len(pairs)}")
    if notes:
        print(f"{DIM}output not pinned yet ({len(notes)}): "
              f"{', '.join(notes[:5])}{RESET}")
        print(f"{DIM}run with --show to see them, then write the shape you want "
              f"into GOLDEN.{RESET}")


if __name__ == "__main__":
    run()
