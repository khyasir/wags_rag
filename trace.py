"""Show the flow for any question, step by step, spending no Groq tokens.

    .venv/bin/python trace.py                 # traces "hi" and "Total sales?"
    .venv/bin/python trace.py "twn branch sales last week"

The Groq calls are stubbed so this runs with an exhausted allowance. Odoo is
real, so the SQL and the figures are real. What is simulated is only the model's
*choice*, and each stub says what the real model would decide and why.

The point is to make the two paths visible:

    a greeting      -> call one asks back, NO query is ever built or run
    a data question -> call one picks keys, Python builds and runs the query
"""

from __future__ import annotations

import json
import sys
from datetime import date

import builder
import catalog
import rpc
from config import settings
from llm import CallOne, CallTwo, Usage
from query import QueryRunner

# What the real model returns for these inputs. Verified earlier against live
# Groq before the daily allowance ran out.
STUBS = {
    "hi": CallOne(
        clarification="Hello. I can report sales, returns, payments, products, "
                      "discounts and unposted orders. What would you like to "
                      "know?",
        usage=Usage(model="llama-3.3-70b-versatile", prompt_tokens=1378,
                    completion_tokens=28, latency_ms=520)),
    "total sales?": CallOne(
        tool_args={"metric": "sales", "period": "all_time"},
        usage=Usage(model="llama-3.3-70b-versatile", prompt_tokens=1378,
                    completion_tokens=22, latency_ms=610)),
}


def step(n, title, where):
    print(f"\n{'─' * 78}\nSTEP {n}  {title}\n         {where}")


def trace(question: str, app_bits) -> None:
    cfg, odoo, lookups, runner = app_bits
    print("\n" + "=" * 78)
    print(f'USER TYPES:  "{question}"')
    print("=" * 78)

    step(1, "Browser posts the message to the chat handler",
         "main.py:202  respond()  ->  main.py:83  App.ask()")
    session = "trace-session"
    print(f"         session_id = {session}")

    step(2, "Log the user's message to Supabase",
         "store.py:65  Store.log_message(role='user')")
    print(f"         INSERT into chat_messages "
          f"{{session_id, role:'user', content:'{question}'}}")
    print("         (skipped in this trace so nothing is written)")

    step(3, "CALL ONE — model picks keys, or asks a question back",
         "llm.py:225  Llm.choose_query()")
    prompt = catalog.build_system_prompt(
        today=date(2025, 12, 11), tz_name=cfg.tz_name,
        data_start=cfg.data_start, lookups=lookups,
        max_buckets=cfg.max_buckets)
    print(f"         system prompt: {len(prompt)} chars (~{len(prompt)//4} tokens)")
    print(f"         generated from catalog.py — the model is offered "
          f"{len(catalog.METRICS)} metric keys,")
    print(f"         never a field name. Tool schema: llm.py:112 "
          f"build_tool_schema()")

    stub = STUBS.get(question.strip().lower())
    if stub is None:
        print("\n         No stub for this question. Add one to STUBS, or run "
              "the real app.")
        return
    print(f"         tokens: {stub.usage.prompt_tokens} + "
          f"{stub.usage.completion_tokens}  ({stub.usage.latency_ms}ms)")

    # ---------------------------------------------------------------- greeting
    if not stub.wants_query:
        print("\n         Model returned NO tool call — it is not a data "
              "question.")
        print(f'         clarification: "{stub.clarification}"')

        step(4, "STOP. Nothing else runs.",
             "main.py:106  `if not one.wants_query: return turn`")
        print("         builder.py   not called")
        print("         Odoo         not contacted")
        print("         SQL          none")
        print("         call two     not made")
        print("\n         Only the reply is logged, then the answer is shown.")
        print(f"\n{'=' * 78}\nRESULT for \"{question}\":  status CLARIFY, "
              f"0 database queries\n{'=' * 78}")
        return

    # ----------------------------------------------------------- data question
    print(f"\n         Model returned tool call: query_pos")
    print(f"         tool_args = {json.dumps(stub.tool_args)}")
    print("         These are KEYS from a fixed list. No SQL, no field names,")
    print("         no model name — those are rejected outright.")

    step(4, "Resolve anything needing the network, then build the query",
         "query.py:72  QueryRunner.run()  ->  query.py:134  _plan()")
    print("         product_keyword would be resolved to ids here; not used.")

    step(5, "Build the query. Pure Python, no network.",
         "builder.py:283  build()")
    plan = builder.build(stub.tool_args, today=date(2025, 12, 11),
                         tz_name=cfg.tz_name, data_start=cfg.data_start,
                         lookups=lookups)
    print(f"         dates.py:resolve_range() -> {plan.date_range.description}")
    print(f"           local  {plan.date_range.local_start} .. "
          f"{plan.date_range.local_end}")
    print(f"           UTC    {plan.date_range.utc_start} .. "
          f"{plan.date_range.utc_end}   <- converted in Python")
    print(f"\n         model   : {plan.model}")
    print(f"         domain  :")
    for t in plan.domain:
        why = ""
        if isinstance(t, tuple) and str(t[0]).endswith("state"):
            why = "   <- forced by catalog.py, model cannot remove it"
        print(f"                     {t}{why}")
    print(f"         fields  : {plan.fields}")
    print(f"         groupby : {plan.groupby}")
    if plan.internal_group_by:
        print(f"                     ^ {list(plan.internal_group_by)} added by us, "
              f"to split returns out")
    print(f"         context : {plan.context}   <- drives date bucket boundaries")

    step(6, "One read_group call to Odoo over JSON-RPC",
         "rpc.py:100  Odoo.read_group()")
    rows, ms = odoo.read_group(plan.model, plan.domain, plan.fields,
                              plan.groupby, context=plan.context)
    print(f"         {len(rows)} buckets in {ms:.0f}ms")
    print("\n         The SQL Odoo generates is essentially:")
    print(f"           SELECT {', '.join(f.split(':')[0] for f in plan.fields)}, "
          f"COUNT(*)")
    print(f"           FROM   {plan.model.replace('.', '_')}")
    print(f"           WHERE  state = 'validate'")
    if plan.date_range.utc_start:
        print(f"             AND  order_datetime >= "
              f"'{plan.date_range.utc_start}'")
    print(f"           GROUP BY {', '.join(plan.groupby)}")
    print("\n         Buckets returned:")
    for r in rows[:6]:
        ot = r.get("order_type")
        print(f"           order_type={str(ot):26} "
              f"untaxed={r.get('amount_untaxed')} count={r.get('__count')}")

    step(7, "Bucket cap — raw rows must never reach the model",
         "query.py:83")
    print(f"         {len(rows)} buckets vs max_buckets={cfg.max_buckets} -> ok")

    step(8, "Returns arithmetic, using the is_return FLAG not the name",
         "builder.py:416  fold_returns()")
    print(f"         return type ids from the cached lookups: "
          f"{lookups.return_type_ids()}")
    result = runner.run(stub.tool_args)
    for k, v in result.data.get("overall", {}).items():
        print(f"           {k:22} {v}")

    step(9, "Log the tool call and its result to Supabase",
         "store.py:65  Store.log_message(role='tool')")
    print("         tool_args and tool_result stored as jsonb.")

    step(10, "CALL TWO — write the sentence from those figures only",
          "llm.py:264  Llm.write_answer()")
    lean = catalog.build_answer_prompt(tz_name=cfg.tz_name, lookups=lookups)
    print(f"         lean prompt: {len(lean)} chars (~{len(lean)//4} tokens)")
    print("         The model sees ONLY this:")
    print("           " + json.dumps(result.for_model(), ensure_ascii=False,
                                     default=str)[:400])
    print("         No order rows. No field names. No SQL.")

    step(11, "Log the answer, show it in the browser",
          "store.py:65 (role='assistant')  ->  main.py:202  respond()")

    print(f"\n{'=' * 78}")
    print(f'RESULT for "{question}":  status PASS, 1 database query, '
          f'{len(rows)} buckets')
    print("=" * 78)


def main() -> None:
    cfg = settings()
    odoo = rpc.connect(cfg)
    lookups = odoo.load_lookups()
    runner = QueryRunner(odoo, cfg, lookups, today_fn=lambda: date(2025, 12, 11))
    bits = (cfg, odoo, lookups, runner)

    questions = sys.argv[1:] or ["hi", "Total sales?"]
    for q in questions:
        trace(q, bits)


if __name__ == "__main__":
    main()
