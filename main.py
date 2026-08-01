"""ROUTER — the LLM decides what to do, then the code does it safely.

    python main.py "total sales"          # one question in the terminal
    python main.py -i                      # keep asking
    python main.py --raw "sales by branch" # also dump the raw Odoo rows

For every message the LLM makes one of three choices:

    reply        not a data question (greeting, refusal, clarify) — no Odoo call
    query_data   fill a spec (model, measures, group_by, filters, period), which
                 the code validates against fields.py and runs
    find_order   look up one order by reference or transaction id

The LLM never writes a query. It only fills the form; build_query turns the form
into the real Odoo call and rejects anything outside the whitelist.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Optional

from openai import OpenAI

import fields as F
import odoo_db
from odoo_db import (DATE_GRAINS, PERIODS, Lookups, Odoo, OdooError, Query,
                     QueryError, build_query, summarise)

# ------------------------------------------------------------------ provider

def _provider() -> tuple:
    """(client, model, label). OpenRouter wins when its key is set."""
    or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if or_key:
        model = os.environ.get("OPENROUTER_MODEL",
                               "meta-llama/llama-3.3-70b-instruct")
        return (OpenAI(api_key=or_key,
                       base_url="https://openrouter.ai/api/v1"),
                model, f"openrouter:{model}")

    key = os.environ.get("GROQ_API_KEY", "").strip()
    model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    return (OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1"),
            model, f"groq:{model}")


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


#: Longest group label shown. Product names carry codes and Arabic and run long.
NAME_W = 34
#: Rows shown before "+N more".
TOP_N = 15


def sort_col(q: Query) -> str:
    """The column a grouped table is sorted by, biggest first."""
    return "net_sales" if q.is_orders else (q.measures[0] if q.measures else "")


def table(data: dict, sort_key: str = "", top_n: int = TOP_N) -> str:
    """One row per group. Sorted biggest first, trimmed, aligned."""
    if not data:
        return "(no rows)"
    if list(data) == ["overall"]:
        d = data["overall"]
        pad = max(len(k) for k in d)
        return "\n".join(f"{k:<{pad}}  {_num(v)}" for k, v in d.items())

    cols = list(next(iter(data.values())))
    sc = sort_key if sort_key in cols else cols[0]
    rows = sorted(data.items(), key=lambda kv: -(kv[1].get(sc) or 0))
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
        out.append(f"{DIM}… and {hidden} more, sorted by {sc}{RESET}")
    return "\n".join(out)


def _num(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


# ---------------------------------------------------------------------- tools
#
# Both schemas are generated from fields.py, so the LLM is only ever offered a
# real field name in its correct role. It cannot name a field that is not here.

def _all_measures() -> list:
    return sorted({f.name for m in F.MODELS.values() for f in m.measures})


def _all_dimensions() -> list:
    return sorted({f.name for m in F.MODELS.values() for f in m.dimensions})


def tool_schemas() -> list:
    dims = _all_dimensions()
    return [
        {"type": "function", "function": {
            "name": "query_data",
            "description": "Fetch POS figures from the database. Use only when "
                           "the person asks for a number or a breakdown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "enum": list(F.MODELS),
                              "description": "pos.wags = orders/sales, "
                              "pos.wags.tree = product lines, "
                              "pos.wags.payment.method = payments"},
                    "measures": {"type": "array",
                                 "items": {"type": "string", "enum": _all_measures()},
                                 "description": "Numbers to total. Optional."},
                    "group_by": {"type": "array",
                                 "items": {"type": "string",
                                           "enum": dims + list(DATE_GRAINS)},
                                 "description": "At most 2. Only if a breakdown "
                                 "was asked for."},
                    "filters": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string", "enum": dims},
                            "value": {"type": "string"}},
                        "required": ["field", "value"]},
                        "description": "Narrow to a branch, order type, etc."},
                    "period": {"type": "string", "enum": list(PERIODS)},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD, custom only"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD, custom only"},
                    "line_type": {"type": "string", "enum": ["product", "modifier"],
                                  "description": "pos.wags.tree only. Default product."},
                    "unposted": {"type": "boolean",
                                 "description": "pos.wags only. Orders never posted "
                                 "to a session."},
                },
                "required": ["model"],
                "additionalProperties": False,
            }}},
        {"type": "function", "function": {
            "name": "find_order",
            "description": "Look up ONE order by its reference (e.g. POS-5525156) "
                           "or transaction id. Use for a single specific order.",
            "parameters": {
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"],
                "additionalProperties": False,
            }}},
    ]


def _models_doc() -> str:
    out = []
    for name, m in F.MODELS.items():
        dims = ", ".join(f.name for f in m.dimensions)
        meas = ", ".join(f.name for f in m.measures)
        out.append(f"- {name} — {m.about}\n"
                   f"    group/filter by: {dims}\n"
                   f"    totals: {meas}")
    return "\n".join(out)


def system_prompt(lookups: Lookups) -> str:
    floor = odoo_db.DATA_START
    floor_line = (f"Only data from {floor} onward exists. Politely refuse any "
                  f"request for an earlier date — do not call a tool."
                  if floor else "The whole history is available.")
    return f"""You help WAGS staff read their POS data.

You never write a query. You pick a model and fill a small form (measures to
total, group_by, filters, period); the server runs the real, safe query.

Today is {odoo_db.TODAY}. Times are {odoo_db.TZ_NAME}. A week runs Monday to Sunday.
{floor_line}

MODELS AND THEIR FIELDS  (you may only use these names)
{_models_doc()}

PERIODS
{', '.join(PERIODS)}   (use all_time when no time is mentioned)

BRANCHES      {lookups.names('branches')}
ORDER TYPES   {lookups.names('order_types')}

HOW TO CHOOSE
- A number or breakdown about sales/orders  -> query_data, model pos.wags.
- Products sold ("top products")            -> query_data, model pos.wags.tree,
  line_type product. Extras/modifiers use line_type modifier.
- Money by payment method / cash            -> query_data, model pos.wags.payment.method.
- Orders not posted to a session            -> query_data, model pos.wags, unposted true.
- ONE specific order by its number/txn id   -> find_order.
- Add group_by only when a breakdown is asked ("by", "per", "each", "which",
  "top", "trend"). "How much / how many / total" need no group_by.
- Narrow with filters when a branch or type is named ("of TWN branch").

WHEN NOT TO CALL A TOOL (reply in words instead)
- Greetings, thanks, small talk, "what can you do".
- Profit, margin, cost of goods, stock levels, staff hours, forecasts: there is
  no data. Say so plainly and offer what you can do. Never answer these with a
  sales figure.
- Anything that changes data (delete, update, create): refuse.
- If the question is unclear, ask ONE short clarifying question. If it is still
  unclear after a few tries, apologise and stop.
"""


# -------------------------------------------------------------------- router

class Router:
    def __init__(self):
        self.odoo = Odoo().connect()
        self.lookups = self.odoo.load_lookups()
        self.client, self.model, self.label = _provider()
        self.tools = tool_schemas()

    def decide(self, question: str, history: Optional[list] = None) -> dict:
        """One LLM call. Returns one of: {reply}, {query_spec}, {find_ref}."""
        messages = [{"role": "system", "content": system_prompt(self.lookups)}]
        if history:
            messages += history[-6:]
        messages.append({"role": "user", "content": question})

        started = time.time()
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages, tools=self.tools,
            tool_choice="auto", temperature=0, max_tokens=300)
        ms = (time.time() - started) * 1000
        u = resp.usage
        msg = resp.choices[0].message
        calls = getattr(msg, "tool_calls", None) or []
        out = {"ms": int(ms), "prompt_tokens": u.prompt_tokens,
               "completion_tokens": u.completion_tokens}

        if calls:
            call = calls[0]
            args = json.loads(call.function.arguments or "{}")
            if call.function.name == "find_order":
                out["find_ref"] = (args.get("ref") or "").strip()
            else:
                out["query_spec"] = {k: v for k, v in args.items()
                                     if v not in (None, "", [], {})}
        else:
            out["reply"] = (msg.content or "").strip()
        return out


# ------------------------------------------------------------- order display

def order_lines(res: dict) -> tuple:
    """(header dict, list of product-line dicts) from a find_order result."""
    o = res["order"]
    header = {
        "reference": o.get("reference"),
        "transaction": o.get("transaction_id"),
        "branch": _name(o.get("branch_id")),
        "order_type": _name(o.get("order_type")),
        "datetime": o.get("order_datetime"),
        "net_sales": o.get("amount_untaxed"),
        "vat": o.get("amount_tax"),
        "discount": o.get("discount_amount"),
        "coupon": o.get("coupon_amount"),
        "total_with_vat": o.get("amount_total"),
    }
    lines = [{
        "product": _name(l.get("product_id")),
        "kind": "modifier" if (l.get("app_line_id") or 0) and
                _idval(l.get("app_line_id")) >= 100000 else "product",
        "qty": l.get("quantity"),
        "unit_price": l.get("price_unit"),
        "subtotal": l.get("price_subtotal"),
        "total": l.get("price_total"),
    } for l in res["lines"]]
    return header, lines


def _name(v):
    if isinstance(v, (list, tuple)) and len(v) > 1:
        return v[1]
    return v if v not in (False, None) else "Not set"


def _idval(v):
    return v[0] if isinstance(v, (list, tuple)) and v else (v or 0)


# ----------------------------------------------------------------------- run

def ask(router: Router, question: str, show_raw: bool = False) -> None:
    head(f'QUESTION:  "{question}"')

    try:
        d = router.decide(question)
    except Exception as exc:                            # noqa: BLE001
        block("LLM CALL FAILED", str(exc)[:300], RED)
        return

    meta = (f"{d['prompt_tokens']}+{d['completion_tokens']} tokens · {d['ms']}ms")

    if "reply" in d:
        block("ROUTER", f"no tool call   {DIM}{meta}{RESET}", YELLOW)
        block("REPLY", d["reply"])
        print(f"\n  {DIM}Odoo was not contacted.{RESET}")
        return

    if "find_ref" in d:
        block("ROUTER", f"find_order '{d['find_ref']}'   {DIM}{meta}{RESET}", GREEN)
        try:
            res = router.odoo.find_order(d["find_ref"])
        except OdooError as exc:
            block("ODOO FAILED", str(exc), RED)
            return
        if not res:
            block("NOT FOUND", f"No order matches '{d['find_ref']}'.", RED)
            return
        header, lines = order_lines(res)
        block("ORDER", "\n".join(f"{k:<15} {_num(v)}" for k, v in header.items()),
              GREEN)
        block("LINES", "\n".join(
            f"{l['kind']:<9} {str(l['product'])[:34]:<34} "
            f"qty {_num(l['qty'])}  total {_num(l['total'])}" for l in lines))
        return

    spec = d["query_spec"]
    block("ROUTER", f"query_data   {DIM}{meta}{RESET}", GREEN)
    block("SPEC — what the LLM chose", json.dumps(spec, indent=2))

    try:
        q = build_query(spec, router.lookups)
    except QueryError as exc:
        block("REJECTED BY THE WHITELIST", str(exc), RED)
        return

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

    block("OUTPUT", table(summarise(rows, q, router.lookups), sort_col(q)), GREEN)


USAGE = f"""{BOLD}WAGS Insight — router{RESET}

  {BOLD}python app_frontend.py{RESET}          chat in the browser
  {BOLD}python main.py "your question"{RESET}  one question in the terminal
  {BOLD}python main.py -i{RESET}               keep asking in the terminal
  {BOLD}python golden.py{RESET}                run the golden pairs

  --raw   also print the untouched Odoo rows
"""


def main() -> None:
    show_raw = "--raw" in sys.argv
    interactive = "-i" in sys.argv or "--interactive" in sys.argv
    questions = [a for a in sys.argv[1:] if not a.startswith("-")]

    if not questions and not interactive:
        print(USAGE)
        return

    router = Router()
    print(f"{DIM}db={odoo_db.ODOO_DB} uid={router.odoo.uid} "
          f"today={odoo_db.TODAY} floor={odoo_db.DATA_START} "
          f"model={router.label}{RESET}")

    for q in questions:
        ask(router, q, show_raw)

    if interactive:
        print(f"\n{DIM}Type a question, or 'q' to quit.{RESET}")
        while True:
            try:
                q = input(f"\n{BOLD}> {RESET}").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q.lower() in ("q", "quit", "exit"):
                break
            if q:
                ask(router, q, show_raw)
    print()


if __name__ == "__main__":
    main()
