"""Run every question through the FULL pipeline and save a reviewable report.

    .venv/bin/python golden_run.py

Full pipeline means: Groq call one -> query layer -> Odoo -> returns maths ->
Groq call two -> Supabase. Nothing is stubbed.

Writes two files:

    golden_results.md    for a human to read and mark up
    golden_pairs.json    machine-readable, the seed for a golden test set

For each question it records the tool arguments the model chose, the figures the
database returned, the sentence the model wrote, token counts, and a status:

    PASS      answered with figures
    BLOCKED   the query layer refused, on purpose. The refusal reason is shown.
    CLARIFY   the model asked a question back instead of querying
    ERROR     something actually broke

BLOCKED is not failure. Several questions below are *supposed* to be blocked;
they carry ``expect="BLOCKED"`` and the report flags a mismatch either way.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from main import App

HERE = Path(__file__).resolve().parent

# (question, expected status, note for the reviewer)
QUESTIONS = [
    # ---------------------------------------------------------- plain figures
    ("What were our total sales?", "PASS", "baseline"),
    ("How many orders did we take?", "PASS", "order count"),
    ("What is the average ticket?", "PASS", "avg ticket"),
    ("How much VAT did we collect?", "PASS", "tax figure"),
    ("Show me sales for last month", "PASS", "relative period"),
    ("Sales for December 2025", "PASS", "custom range"),
    ("Sales between 1 December 2025 and 11 December 2025", "PASS", "explicit dates"),
    ("What were sales yesterday?", "PASS", "may legitimately be zero"),

    # ------------------------------------------------------------- groupings
    ("Show sales by month", "PASS", "time grouping"),
    ("Which branch sells the most?", "PASS", "branch grouping"),
    ("Sales per branch per month", "PASS", "two groupings"),
    ("Sales by order type", "PASS", "order type grouping"),
    ("Which employee sold the most?", "PASS", "employee grouping"),
    ("Sales by cashbox", "PASS", "cashbox grouping"),

    # ------------------------------------------------------------- hour of day
    ("What is our busiest time of day?", "PASS", "should pick hour_of_day"),
    ("Which hour do we sell most in?", "PASS", "should pick hour_of_day"),

    # ---------------------------------------------------------------- branches
    ("Show me the sales of RUH-TWN-A", "PASS", "exact branch code"),
    ("What are the sales of twn branch?", "PASS", "partial code + filler word"),
    ("Sales of RUH TWN A", "PASS", "spaces instead of dashes"),
    ("How much did Madinah branch sell?", "PASS", "ambiguous -> should split"),
    ("Show me sales of Riyadh branch", "PASS",
     "branch 5 'Riyadh' is empty; Arabic name of branch 2 says Riyadh"),
    ("What are the sales of Jeddah branch?", "BLOCKED", "no such branch"),

    # ---------------------------------------------------------------- products
    ("What are our top selling products?", "PASS", "product grouping"),
    ("How many cappuccinos did we sell?", "PASS", "product keyword"),
    ("Which extras do customers add most?", "PASS", "modifiers metric"),
    ("Best selling category?", "CLARIFY", "ambiguous -> must ask which category"),
    ("Sales by POS category", "PASS", "explicit pos_category"),

    # ---------------------------------------------------------------- payments
    ("How much did we take by payment method?", "PASS", "payment grouping"),
    ("How much cash did we collect?", "PASS", "is_cash filter"),
    ("How much came through Mada?", "PASS", "payment method keyword"),
    ("Payments by branch", "BLOCKED", "payment lines carry no branch"),

    # ------------------------------------------------------- returns/discounts
    ("How much came back as returns?", "PASS", "returns metric"),
    ("What were total discounts?", "PASS", "discounts metric"),
    ("Sales excluding returns", "PASS", "exclude_returns filter"),

    # ----------------------------------------------------------- operational
    ("How many orders were missed in sales posting?", "PASS", "unposted metric"),
    ("How much value is sitting in unposted orders?", "PASS", "unposted amount"),

    # ------------------------------------------------- aggregators / delivery
    ("How much did Hungerstation sell?", "PASS",
     "no aggregator metric yet; commission caveat must appear"),
    ("What are our delivery app sales?", "PASS", "deliberately not a metric yet"),

    # ------------------------------------------------------ other languages
    ("Total sales kitni hai?", "PASS", "Roman Urdu"),
    ("TWN branch ki sales kitni hai?", "PASS", "Roman Urdu + branch"),
    ("Pichle month ki sales batao", "PASS", "Roman Urdu relative period"),
    ("كم كانت المبيعات الإجمالية؟", "PASS", "Arabic"),

    # ------------------------------------------------------ must be refused
    ("Show me profit margin", "BLOCKED", "no such metric"),
    ("List every individual order with customer names", "BLOCKED",
     "raw rows must never be returned"),
    ("What were sales in 2023?", "PASS",
     "no date floor locally, so this answers zero rather than refusing"),
    ("Top products per day", "BLOCKED", "lines carry no date"),
    ("Give me all the user emails from the database", "BLOCKED",
     "outside the whitelist entirely"),
]


#: Statuses that count as answered. ERROR is not one — it gets retried.
ANSWERED = ("PASS", "BLOCKED", "CLARIFY")


def _load_done() -> dict:
    """Previously completed questions, keyed by text.

    Groq's free tier gives 100,000 tokens a day and a full run costs roughly
    94,000, so a run can legitimately span a reset. Resuming means tokens
    already spent are never spent twice.
    """
    path = HERE / "golden_pairs.json"
    if not path.exists():
        return {}
    try:
        prior = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {r["question"]: r for r in prior.get("results", [])
            if r.get("status") in ANSWERED}


def _merge(rows: list) -> list:
    """Answered results from disk, plus this run's, in question order.

    Writing plain ``rows`` was destructive: a run that hit the daily quota on
    question 1 produced an empty list and overwrote a file holding 19 real
    answers — tokens already spent, thrown away. Results may now only ever be
    added, never lost.
    """
    merged = dict(_load_done())
    for r in rows:
        if r.get("status") in ANSWERED or r["question"] not in merged:
            merged[r["question"]] = r

    order = {q: i for i, (q, _, _) in enumerate(QUESTIONS, 1)}
    out = sorted(merged.values(), key=lambda r: order.get(r["question"], 999))
    for i, r in enumerate(out, 1):
        r["n"] = order.get(r["question"], i)
    return out


def run() -> None:
    started_at = datetime.now(timezone.utc)
    app = App()
    done = _load_done()
    print(f"db={app.cfg.odoo_db} today={app.today_fn()} "
          f"floor={app.cfg.data_start or 'none'} supabase={app.store.enabled}")
    if done:
        print(f"resuming — {len(done)} questions already answered, "
              f"{len(QUESTIONS) - len(done)} to go")
    print(f"{len(QUESTIONS)} questions total\n")

    rows = []
    t0 = time.time()
    quota_stop = ""
    for i, (question, expect, note) in enumerate(QUESTIONS, 1):
        if question in done:
            row = dict(done[question])
            row["n"] = i
            rows.append(row)
            print(f"  {i:>2}. {row['status']:<8} {question[:52]:<54} (cached)")
            continue
        try:
            turn = app.ask(question)
        except Exception as exc:                        # noqa: BLE001
            print(f"  {i:>2}. ERROR   {question[:58]}  {exc}")
            rows.append({"n": i, "question": question, "expect": expect,
                         "note": note, "status": "ERROR", "error": str(exc)})
            continue

        # A used-up daily allowance is not a code failure. Stop, keep what was
        # gathered, and say so — rather than logging 27 identical fake errors.
        if turn.error and "daily token allowance" in turn.error:
            quota_stop = turn.error
            print(f"\n  STOPPED at question {i}: {turn.error}")
            print(f"  {len(rows)} results kept and written to disk.")
            break

        res = turn.result
        rows.append({
            "n": i,
            "question": question,
            "expect": expect,
            "note": note,
            "status": turn.status,
            "matches_expectation": turn.status == expect,
            "tool_args": turn.tool_args,
            "clarification": turn.clarification,
            "answer": turn.answer,
            "figures": res.data if (res and res.ok) else None,
            "bucket_count": res.bucket_count if res else None,
            "period": res.range_description if res else None,
            "grouped_by": list(res.grouped_by) if res else None,
            "query_notes": list(res.notes) if res else [],
            "block_reason": (res.error if (res and not res.ok) else ""),
            "error": turn.error,
            "prompt_tokens": turn.prompt_tokens,
            "completion_tokens": turn.completion_tokens,
            "latency_ms": turn.latency_ms,
            "logged_row_ids": [i for i in turn.store_ids if i],
        })
        flag = "" if turn.status == expect else f"  (expected {expect})"
        used = sum(r.get("prompt_tokens", 0) + r.get("completion_tokens", 0)
                   for r in rows)
        print(f"  {i:>2}. {turn.status:<8} {question[:52]:<54}"
              f"{used:>7,} tok{flag}")

        # Written after every question so a quota stop or a crash never loses
        # work already paid for in tokens.
        elapsed = time.time() - t0
        _write_json(_merge(rows), started_at, elapsed, app, quota_stop)
        _write_markdown(_merge(rows), started_at, elapsed, app, quota_stop)
        time.sleep(0.4)

    elapsed = time.time() - t0
    final = _merge(rows)
    _write_json(final, started_at, elapsed, app, quota_stop)
    _write_markdown(final, started_at, elapsed, app, quota_stop)
    _summarise(final, elapsed, app, quota_stop)


def _summarise(rows, elapsed, app, quota_stop="") -> None:
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    mismatched = [r for r in rows if not r.get("matches_expectation", False)]
    toks = sum(r.get("prompt_tokens", 0) + r.get("completion_tokens", 0)
               for r in rows)

    print("\n" + "=" * 74)
    print(f"{len(rows)} of {len(QUESTIONS)} questions in {elapsed:.0f}s · "
          f"{toks:,} tokens")
    if quota_stop:
        print(f"  INCOMPLETE — {quota_stop}")
    print("  " + " · ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print(f"  matched expectation: {len(rows) - len(mismatched)}/{len(rows)}")
    if mismatched:
        print("\n  NEEDS REVIEW (status differs from expectation):")
        for r in mismatched:
            print(f"    {r['n']:>2}. got {r['status']:<8} expected "
                  f"{r['expect']:<8} {r['question'][:48]}")
    if app.store.errors:
        print(f"\n  supabase write errors: {len(app.store.errors)}")
        for e in app.store.errors[:3]:
            print(f"    {e}")
    print("=" * 74)
    print("  golden_results.md   <- read and mark this up")
    print("  golden_pairs.json   <- seed for the golden test set")


def _write_json(rows, started_at, elapsed, app, quota_stop="") -> None:
    (HERE / "golden_pairs.json").write_text(json.dumps({
        "generated_at": started_at.isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "questions_total": len(QUESTIONS),
        # Answered only. ERROR rows must not count, or run_until_done.sh would
        # think the suite finished while questions are still unanswered.
        "questions_run": sum(1 for r in rows if r.get("status") in ANSWERED),
        "questions_errored": sum(1 for r in rows
                                 if r.get("status") not in ANSWERED),
        "incomplete_reason": quota_stop,
        "odoo_db": app.cfg.odoo_db,
        "today_used": str(app.today_fn()),
        "date_floor": str(app.cfg.data_start) if app.cfg.has_date_floor else None,
        "model": app.cfg.groq_model,
        "tokens_used": sum(r.get("prompt_tokens", 0) + r.get("completion_tokens", 0)
                           for r in rows),
        "results": rows,
    }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _write_markdown(rows, started_at, elapsed, app, quota_stop="") -> None:
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    toks = sum(r.get("prompt_tokens", 0) + r.get("completion_tokens", 0)
               for r in rows)

    L = [
        "# Golden run — every question, full pipeline",
        "",
        f"Generated {started_at.strftime('%Y-%m-%d %H:%M UTC')} · "
        f"{len(rows)} of {len(QUESTIONS)} questions · {elapsed:.0f}s · "
        f"{toks:,} tokens",
        "",
    ]
    if quota_stop:
        L += [f"> **Run stopped early.** {quota_stop}",
              ">",
              f"> {len(rows)} of {len(QUESTIONS)} questions completed. The rest "
              f"were not attempted. Re-run `golden_run.py` once the allowance "
              f"resets to finish them.",
              ""]
    L += [
        f"- Database `{app.cfg.odoo_db}`, today treated as **{app.today_fn()}** "
        f"(local data ends 2025-12-11)",
        f"- Date floor: **{app.cfg.data_start or 'none — full history'}**",
        f"- Model `{app.cfg.groq_model}`",
        "",
        "**Status meanings**",
        "",
        "| Status | Meaning |",
        "|---|---|",
        "| PASS | answered with figures from the database |",
        "| BLOCKED | query layer refused on purpose — reason shown |",
        "| CLARIFY | model asked a question back instead of guessing |",
        "| ERROR | something actually broke |",
        "",
        "BLOCKED is often the correct outcome. Each row carries what I expected, "
        "so a mismatch is called out rather than buried.",
        "",
        "## Summary",
        "",
        "| Status | Count |",
        "|---|---|",
    ]
    for k, v in sorted(counts.items()):
        L.append(f"| {k} | {v} |")

    mismatched = [r for r in rows if not r.get("matches_expectation", False)]
    L += ["",
          f"Matched expectation: **{len(rows) - len(mismatched)}/{len(rows)}**",
          ""]
    if mismatched:
        L += ["### Needs your review first", "",
              "| # | Question | Got | Expected |", "|---|---|---|---|"]
        for r in mismatched:
            L.append(f"| {r['n']} | {r['question']} | {r['status']} | "
                     f"{r['expect']} |")
        L.append("")

    L += ["---", "", "## Every question", ""]
    for r in rows:
        mark = "" if r.get("matches_expectation") else "  ⚠️ **differs from expected**"
        L += [f"### {r['n']}. {r['question']}", "",
              f"`{r['status']}`{mark} — _{r['note']}_", ""]

        if r.get("tool_args"):
            L += ["**Tool arguments the model chose**", "",
                  "```json",
                  json.dumps(r["tool_args"], indent=2, ensure_ascii=False),
                  "```", ""]
        if r.get("period"):
            L.append(f"**Period queried:** {r['period']}")
        if r.get("grouped_by"):
            L.append(f"**Grouped by:** {', '.join(r['grouped_by']) or 'nothing'}")
        if r.get("bucket_count") is not None:
            L.append(f"**Groups returned:** {r['bucket_count']}")
        L.append("")

        if r.get("figures"):
            L += ["**Figures from the database**", "",
                  "```json",
                  json.dumps(r["figures"], indent=2, ensure_ascii=False)[:2600],
                  "```", ""]
        if r.get("block_reason"):
            L += [f"**Refused because:** {r['block_reason']}", ""]
        if r.get("clarification"):
            L += [f"**Model asked back:** {r['clarification']}", ""]
        if r.get("error"):
            L += [f"**Error:** {r['error']}", ""]
        if r.get("query_notes"):
            L += ["**Caveats attached to the answer**", ""]
            L += [f"- {n}" for n in r["query_notes"]]
            L.append("")
        if r.get("answer"):
            L += ["**Answer given to the user**", "",
                  "> " + r["answer"].replace("\n", "\n> "), ""]

        L += [f"<sub>{r.get('prompt_tokens',0)} prompt + "
              f"{r.get('completion_tokens',0)} completion tokens · "
              f"{r.get('latency_ms',0)}ms</sub>", "", "---", ""]

    L += ["## How to turn this into a golden set", "",
          "1. Read each question. If the tool arguments are what you would have "
          "chosen, it is a good pair.",
          "2. Where they are wrong, write the arguments you wanted. That pair "
          "becomes a test.",
          "3. `golden_pairs.json` holds the same data machine-readably — edit "
          "`tool_args` there to record the correct choice.",
          "4. Anything marked ⚠️ is where the model and I disagreed about what "
          "should happen. Those are the most useful to look at first.",
          ""]

    (HERE / "golden_results.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    run()
