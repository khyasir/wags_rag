"""ROUTER — the LLM decides: is this a data question or not?

    python main.py                          # a few sample questions
    python main.py "give me sales"          # one question
    python main.py "hi" "total sales"       # several
    python main.py --raw "sales by branch"  # also dump the raw Odoo rows

Augmentation is deliberately not here yet. There is no second LLM call and no
prose answer. The point right now is to see, for any question:

    what the LLM chose  ->  the exact query  ->  what came back

Once the query and the output shape are right, the answer-writing goes on top.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Optional

from groq import Groq

import odoo_db
from odoo_db import (METRICS, PERIODS, Lookups, Odoo, OdooError, Query,
                     QueryError, build_query, summarise)

MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

W = 78
DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, RED, YELLOW, CYAN = "\033[32m", "\033[31m", "\033[33m", "\033[36m"


# ------------------------------------------------------------------ printing

def head(text: str, colour: str = CYAN) -> None:
    print(f"\n{colour}{'━' * W}\n{text}\n{'━' * W}{RESET}")


def block(title: str, body: str, colour: str = "") -> None:
    print(f"\n{BOLD}{colour}{title}{RESET}")
    for line in str(body).rstrip().splitlines():
        print(f"  {line}")


#: Longest group label shown. Product names carry codes and Arabic text and run
#: past 70 characters, which wrecks the alignment.
NAME_W = 34

#: Rows shown before "+N more". 97 unsorted products is not an answer.
TOP_N = 15

#: Column to sort a grouped table by, biggest first, per metric.
SORT_BY = {"sales": "net_sales", "returns": "amount_untaxed",
           "discounts": "discount_amount", "unposted_orders": "amount_untaxed",
           "payments": "amount", "products": "quantity",
           "modifiers": "quantity"}


def table(data: dict, metric: str = "", top_n: int = TOP_N) -> str:
    """One row per group. Sorted biggest first, trimmed, aligned."""
    if not data:
        return "(no rows)"
    if list(data) == ["overall"]:
        d = data["overall"]
        pad = max(len(k) for k in d)
        return "\n".join(f"{k:<{pad}}  {_num(v)}" for k, v in d.items())

    cols = list(next(iter(data.values())))
    sort_col = SORT_BY.get(metric) if SORT_BY.get(metric) in cols else cols[0]
    rows = sorted(data.items(),
                  key=lambda kv: -(kv[1].get(sort_col) or 0))
    hidden = max(0, len(rows) - top_n)
    rows = rows[:top_n]

    labels = {k: (str(k)[:NAME_W - 1] + "…" if len(str(k)) > NAME_W else str(k))
              for k, _ in rows}
    key_w = max(len("group"), max(len(v) for v in labels.values()))
    widths = [max(len(c), max(len(_num(v.get(c))) for _, v in rows))
              for c in cols]

    out = ["  ".join([f"{'group':<{key_w}}"] +
                     [f"{c:>{w}}" for c, w in zip(cols, widths)]),
           "  ".join(["─" * key_w] + ["─" * w for w in widths])]
    for k, v in rows:
        out.append("  ".join([f"{labels[k]:<{key_w}}"] +
                             [f"{_num(v.get(c)):>{w}}"
                              for c, w in zip(cols, widths)]))
    if hidden:
        out.append(f"{DIM}… and {hidden} more, sorted by {sort_col}{RESET}")
    return "\n".join(out)


def _num(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


# ---------------------------------------------------------------------- tool

def tool_schema() -> dict:
    """Generated from the whitelist, so the LLM is never offered a field name."""
    groups = sorted({g for m in METRICS.values() for g in m.groups})
    return {
        "type": "function",
        "function": {
            "name": "query_pos",
            "description": "Fetch POS sales figures from the database. Use this "
                           "only when the person is asking for a number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "enum": list(METRICS)},
                    "period": {"type": "string", "enum": list(PERIODS)},
                    "start_date": {"type": "string",
                                   "description": "YYYY-MM-DD, custom only"},
                    "end_date": {"type": "string",
                                 "description": "YYYY-MM-DD, custom only"},
                    "group_by": {"type": "array",
                                 "items": {"type": "string", "enum": groups},
                                 "description": "At most 2, only if a "
                                                "breakdown was asked for"},
                },
                # period is required so the model states its choice rather than
                # omitting it and silently inheriting a default.
                "required": ["metric", "period"],
                "additionalProperties": False,
            },
        },
    }


def system_prompt(lookups: Lookups) -> str:
    metrics = "\n".join(
        f"- {k}: {m.about}\n  groups: {', '.join(m.groups)}"
        for k, m in METRICS.items())
    return f"""You answer questions about POS sales for WAGS.

You never write a query. You choose a metric and parameters, and the server
runs the real query.

Today is {odoo_db.TODAY}. Times are {odoo_db.TZ_NAME}. A week runs Monday to Sunday.

METRICS
{metrics}

PERIODS
{', '.join(PERIODS)}

BRANCHES      {lookups.names('branches')}
ORDER TYPES   {lookups.names('order_types')}

NOT EVERY MESSAGE IS A DATA QUESTION
Do not call the tool for greetings, thanks, small talk, or "what can you do".
Reply in words instead. Answering "hi" with sales figures is wrong.

ASK FOR THE LEAST YOU NEED
Send no group_by unless a breakdown was actually asked for. "by", "per",
"each", "which branch", "trend" ask for one. "How much", "how many", "what is",
"total" do not. One question, one figure.

WHEN NO PERIOD IS MENTIONED, USE all_time
"total sales", "how much cash did we collect", "how many orders" carry no time
window, so they mean everything on record. Do not assume today. Only use a
narrower period when the question names one: "today", "last month", "in
December", "this week".

THE sales METRIC ALREADY RETURNS ALL OF THESE
net sales, gross sales, returns, VAT, total with VAT, discounts, order count,
return count, average ticket. So "how much VAT", "what is the average ticket"
and "how many orders" are all metric=sales. Do not reach for payments to get
VAT, and do not refuse these — they come back automatically.

ONLY THESE ARE GENUINELY UNAVAILABLE
Profit, margin, cost of goods, stock levels, staff hours, forecasts. There is
no data for them. Do not answer with a different metric — a sales figure
offered as a profit figure is worse than no answer. Say in words that you
cannot do it and what you can do instead. Do not call the tool.
"""


# -------------------------------------------------------------------- router

class Router:
    def __init__(self):
        self.odoo = Odoo().connect()
        self.lookups = self.odoo.load_lookups()
        self.client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))
        self.tool = tool_schema()

    def decide(self, question: str) -> dict:
        """LLM call one. Returns {tool_args} or {reply}."""
        started = time.time()
        resp = self.client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system",
                       "content": system_prompt(self.lookups)},
                      {"role": "user", "content": question}],
            tools=[self.tool], tool_choice="auto",
            temperature=0, max_tokens=200)
        ms = (time.time() - started) * 1000
        u = resp.usage
        msg = resp.choices[0].message
        calls = getattr(msg, "tool_calls", None) or []
        out = {"ms": int(ms), "prompt_tokens": u.prompt_tokens,
               "completion_tokens": u.completion_tokens}
        if calls:
            args = json.loads(calls[0].function.arguments or "{}")
            out["tool_args"] = {k: v for k, v in args.items()
                                if v not in (None, "", [], {})}
        else:
            out["reply"] = (msg.content or "").strip()
        return out


# ----------------------------------------------------------------------- run

def ask(router: Router, question: str, show_raw: bool = False) -> None:
    head(f'QUESTION:  "{question}"')

    # -- 1. does this need data at all?
    try:
        d = router.decide(question)
    except Exception as exc:                            # noqa: BLE001
        block("LLM CALL FAILED", str(exc)[:300], RED)
        return

    meta = (f"{d['prompt_tokens']}+{d['completion_tokens']} tokens · "
            f"{d['ms']}ms")

    if "reply" in d:
        block("ROUTER", f"no tool call — not a data question   {DIM}{meta}{RESET}",
              YELLOW)
        block("REPLY", d["reply"])
        print(f"\n  {DIM}Odoo was not contacted. No query ran.{RESET}")
        return

    args = d["tool_args"]
    block("ROUTER", f"tool call — data needed   {DIM}{meta}{RESET}", GREEN)
    block("INPUT — what the LLM chose", json.dumps(args, indent=2))

    # -- 2. keys become a real query
    try:
        q = build_query(args, router.lookups)
    except QueryError as exc:
        block("REJECTED BY THE WHITELIST", str(exc), RED)
        return

    block("QUERY — exactly what is sent to Odoo",
          f"model    {q.model}\n"
          f"domain   " + "\n         ".join(str(t) for t in q.domain) + "\n"
          f"fields   {q.fields}\n"
          f"groupby  {q.groupby}\n"
          f"context  {q.context}\n"
          f"period   {q.period_label}")
    block("QUERY — equivalent SQL", q.as_sql())

    # -- 3. run it
    try:
        rows, ms = router.odoo.run(q)
    except OdooError as exc:
        block("ODOO FAILED", str(exc), RED)
        return

    print(f"\n  {DIM}{len(rows)} raw buckets in {ms:.0f}ms "
          f"via {router.odoo.last_transport}{RESET}")

    if show_raw:
        block("RAW — exactly what Odoo returned",
              "\n".join(json.dumps({k: v for k, v in r.items()
                                    if k not in ("__domain", "__range")},
                                   default=str) for r in rows[:12]))

    # -- 4. the output shape
    block("OUTPUT", table(summarise(rows, q, router.lookups), q.metric), GREEN)


SAMPLES = ["hi", "total sales", "sales last month", "sales by branch",
           "top selling products", "how much cash did we take"]


def main() -> None:
    show_raw = "--raw" in sys.argv
    questions = [a for a in sys.argv[1:] if not a.startswith("--")] or SAMPLES
    router = Router()
    print(f"{DIM}db={odoo_db.ODOO_DB} uid={router.odoo.uid} "
          f"today={odoo_db.TODAY} model={MODEL}{RESET}")
    for q in questions:
        ask(router, q, show_raw)
    print()


if __name__ == "__main__":
    main()
