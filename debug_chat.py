"""Dump everything that goes to and from the LLM, verbatim. Nothing hidden.

    .venv/bin/python debug_chat.py "hi"
    .venv/bin/python debug_chat.py "give me sales"
    .venv/bin/python debug_chat.py "hi" --live      # actually call Groq

Without --live nothing is sent, so it costs no tokens: you still see the exact
system prompt, the exact messages array, and the exact tool schema that WOULD
be transmitted. With --live it also shows the raw response.

Output goes to the terminal and to debug_chat.txt, because the system prompt is
about 6,500 characters and scrolls off screen.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import builder
import catalog
import rpc
from config import settings
from llm import Llm
from query import QueryRunner

OUT = Path(__file__).resolve().parent / "debug_chat.txt"
_buf: list = []


def w(line: str = "") -> None:
    print(line)
    _buf.append(line)


def rule(title: str) -> None:
    w("")
    w("=" * 78)
    w(title)
    w("=" * 78)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    live = "--live" in sys.argv
    question = args[0] if args else "hi"

    cfg = settings()
    odoo = rpc.connect(cfg)
    lookups = odoo.load_lookups()
    today = date(2025, 12, 11)
    llm = Llm(cfg, lookups, today_fn=lambda: today)
    runner = QueryRunner(odoo, cfg, lookups, today_fn=lambda: today)

    rule(f'USER TYPES:  "{question}"')
    w(f"live mode: {live}   (without --live nothing is sent to Groq)")

    # ---------------------------------------------------------------- CALL ONE
    system = llm.system_prompt()
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": question}]

    rule("CALL ONE — THE EXACT SYSTEM PROMPT SENT")
    w(f"[{len(system)} characters, roughly {len(system)//4} tokens]")
    w("")
    w(system)

    rule("CALL ONE — THE EXACT TOOL SCHEMA SENT")
    w("This is the ONLY way the model can request data. Note there is no field")
    w("name, no model name, and no SQL anywhere in it — only keys.")
    w("")
    w(json.dumps(llm.tool, indent=2))

    rule("CALL ONE — THE EXACT MESSAGES ARRAY SENT")
    w(json.dumps(
        [{"role": m["role"],
          "content": (m["content"] if m["role"] != "system"
                      else f"<the {len(system)}-char system prompt above>")}
         for m in messages], indent=2))
    w("")
    w(f"model       : {cfg.groq_model}")
    w("temperature : 0")
    w("max_tokens  : 200")
    w("tool_choice : auto      <- model decides: call the tool, or reply in words")

    if not live:
        rule("WHAT HAPPENS NEXT (not sent — rerun with --live to actually call)")
        w("The model returns ONE of two things:")
        w("")
        w("  A) No tool call, just text.  -> a greeting or a clarifying question.")
        w("     main.py:106 returns immediately. builder is not called, Odoo is")
        w("     never contacted, no SQL runs, call two never happens.")
        w("")
        w("  B) A tool call with arguments, e.g.")
        w('     {\"metric\": \"sales\", \"period\": \"all_time\"}')
        w("     -> those keys go to builder.py, which writes the real query.")
        _finish()
        return

    # ------------------------------------------------------------------- live
    rule("CALL ONE — RAW RESPONSE FROM GROQ")
    try:
        one = llm.choose_query(question)
    except Exception as exc:                            # noqa: BLE001
        w(f"FAILED: {exc}")
        _finish()
        return

    w(f"prompt_tokens     : {one.usage.prompt_tokens}")
    w(f"completion_tokens : {one.usage.completion_tokens}")
    w(f"latency           : {one.usage.latency_ms}ms")
    w("")
    w("raw content returned by the model:")
    w(f"  {one.raw_text!r}")
    w("")
    w(f"tool call made?   : {one.wants_query}")

    if not one.wants_query:
        rule("NO TOOL CALL — THE CHAIN STOPS HERE")
        w(f'clarification: "{one.clarification}"')
        w("")
        w("  builder.py   NOT called")
        w("  Odoo         NOT contacted")
        w("  SQL          none")
        w("  CALL TWO     NOT made")
        w("")
        w("Only this text is logged and shown. Zero database access.")
        _finish()
        return

    w(f"parsed tool_args  : {json.dumps(one.tool_args)}")

    # ------------------------------------------------------------ build + run
    rule("BUILDER — THE KEYS BECOME A REAL QUERY (pure Python, no network)")
    plan = builder.build(one.tool_args, today=today, tz_name=cfg.tz_name,
                         data_start=cfg.data_start, lookups=lookups,
                         branch_scope=cfg.branch_ids)
    w(f"model   : {plan.model}")
    w("domain  :")
    for t in plan.domain:
        tag = "   <- forced, model cannot remove" \
            if isinstance(t, tuple) and str(t[0]).endswith("state") else ""
        w(f"            {t}{tag}")
    w(f"fields  : {plan.fields}")
    w(f"groupby : {plan.groupby}")
    if plan.internal_group_by:
        w(f"            ^ {list(plan.internal_group_by)} added by us for the "
          f"returns split")
    w(f"context : {plan.context}")
    w(f"period  : {plan.date_range.description}")
    w(f"UTC     : {plan.date_range.utc_start} .. {plan.date_range.utc_end}")

    rule("ODOO — READ_GROUP OVER JSON-RPC")
    rows, ms = odoo.read_group(plan.model, plan.domain, plan.fields,
                               plan.groupby, context=plan.context)
    w(f"{len(rows)} buckets in {ms:.0f}ms")
    w("")
    w("raw buckets returned by Odoo:")
    for r in rows[:10]:
        w(f"  {json.dumps(r, default=str)[:150]}")

    result = runner.run(one.tool_args)

    rule("CALL TWO — THE EXACT SYSTEM PROMPT SENT")
    answer_prompt = llm.answer_prompt()
    w(f"[{len(answer_prompt)} characters, roughly {len(answer_prompt)//4} tokens]")
    w("Note this is SHORTER than call one — call two cannot query, so the")
    w("metric list and filter keys are left out.")
    w("")
    w(answer_prompt)

    rule("CALL TWO — THE EXACT DATA HANDED TO THE MODEL")
    w("This is everything it sees. No order rows, no field names, no SQL.")
    w("")
    w(json.dumps(result.for_model(), indent=2, ensure_ascii=False, default=str))

    rule("CALL TWO — RAW RESPONSE FROM GROQ")
    try:
        two = llm.write_answer(question, result.for_model(),
                               tool_args=one.tool_args)
    except Exception as exc:                            # noqa: BLE001
        w(f"FAILED: {exc}")
        _finish()
        return
    w(f"prompt_tokens     : {two.usage.prompt_tokens}")
    w(f"completion_tokens : {two.usage.completion_tokens}")
    w(f"latency           : {two.usage.latency_ms}ms")
    w("")
    w("ANSWER SHOWN TO THE USER:")
    w(two.answer)

    rule("TOTALS")
    w(f"tokens : {one.usage.prompt_tokens + two.usage.prompt_tokens} prompt + "
      f"{one.usage.completion_tokens + two.usage.completion_tokens} completion")
    w(f"queries: 1 read_group against {plan.model}")
    _finish()


def _finish() -> None:
    OUT.write_text("\n".join(_buf), encoding="utf-8")
    print(f"\n[full output also written to {OUT.name}]")


if __name__ == "__main__":
    main()
