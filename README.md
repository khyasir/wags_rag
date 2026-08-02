---
title: WAGS Insight
emoji: 📊
colorFrom: blue
colorTo: indigo
sdk: gradio
sdk_version: 6.16.0
app_file: app_frontend.py
pinned: false
---

# WAGS Insight — Task 01

Chat app that answers POS sales questions from Odoo 16. The model never writes a
query: it picks a metric and parameters from a fixed whitelist, Python builds and
runs the real query, and the model only writes the final sentence.

## Run it

```bash
.venv/bin/python main.py          # http://localhost:7860
```

Health check, no tokens spent: <http://localhost:7860/health>

```bash
.venv/bin/python -m pytest tests/ -q   # 94 tests, no network needed
.venv/bin/python stage2_report.py      # every metric against Odoo, with timings
.venv/bin/python scenarios.py          # 22 real questions + 10 attack cases, no Groq
.venv/bin/python golden_run.py         # 47 questions through the FULL pipeline
```

## Read the golden run first

`golden_results.md` is the review artefact. One section per question: the tool
arguments the model chose, the figures the database returned, the caveats
attached, the sentence written, token counts, and a status.

| Status | Meaning |
|---|---|
| PASS | answered with figures |
| BLOCKED | the query layer refused, on purpose — reason shown |
| CLARIFY | the model asked back instead of guessing |
| ERROR | something actually broke |

**BLOCKED is usually correct.** Six questions are meant to be refused. Each row
records what was expected, so a correct refusal is never mistaken for a failure,
and mismatches are flagged ⚠️ in a table at the top.

`golden_pairs.json` holds the same data machine-readably. Correct `tool_args`
there to turn it into a golden test set.

## Groq's free tier is 100,000 tokens per DAY

Not per minute. This is the binding constraint, and it is easy to trip over.

- ~1,970 tokens per question ⇒ roughly **44 questions a day**
- A full 47-question golden run costs ~94,000 — nearly the whole allowance
- `run_until_done.sh` retries every 30 minutes and `golden_run.py` resumes from
  `golden_pairs.json`, so tokens are never spent twice

Token counts stored in Supabase come from Groq and are exact. `cost_usd` is
deliberately left NULL until a per-million rate is confirmed — see
`known_limits.token_cost_rate`.

## Layout

| File | Role |
|---|---|
| `catalog.py` | **The whitelist.** Metrics, group-by matrix, filters, lookup cache, prompt generator. Nothing else decides what may be queried. |
| `builder.py` | Pure. Arguments in, `QueryPlan` out, plus the returns arithmetic. No network — enforced by a test. |
| `dates.py` | Riyadh↔UTC, named periods, optional date floor. |
| `known_limits.py` | Open questions about the data, injected into the prompt so the model warns instead of guessing. |
| `query.py` | Builder + Odoo + returns maths + bucket cap. |
| `rpc.py` | Odoo client. **JSON-RPC, not XML-RPC** — see below. |
| `llm.py` | Two Groq calls, tool schema generated from the catalog, rate-limit backoff. |
| `store.py` | Supabase writes, best-effort with a visible error list. |
| `main.py` | `App.ask()` orchestration, FastAPI `/health` and `/ask`, Gradio chat. |
| `config.py` | Environment loading. |

## Things that will surprise you

**Local `today` is faked.** Local data ends 2025-12-11 while the clock says 2026,
so every relative period would be empty. With no date floor set, `main.py`
treats today as 2025-12-11. With `DATA_START` set, the real date is used.

**`DATA_START` blank means no floor.** Production ships `2026-01-01`; the local
`.env` leaves it empty so all history is queryable. Both paths are tested.

**JSON-RPC, not XML-RPC.** Odoo 16 hardcodes `allow_none=False` when marshalling
XML-RPC replies, so a `read_group` sum over an all-NULL column crashes the server
with `cannot marshal None`. The `discounts` metric hits this immediately.
`/jsonrpc` is Odoo's own endpoint and carries null natively.

**Lookups load with `active_test=False`.** The only order type flagged
`is_return` is archived, and so is the only `is_cash` payment method. Without
this the cache reports zero return types and return orders get silently counted
into gross sales.

**`read_group` cannot group by a related field.** So line metrics cannot be
grouped by date at all, and payments cannot be grouped by branch. Both are
explicit refusals, never a silent substitution. Date *filtering* through the
parent works fine.

**Call two gets a leaner prompt than call one.** A deliberate deviation from the
spec's "same system prompt". Call two cannot query, so the metric and filter
lists were dead weight, and sending them doubled the cost of every question —
capping the app at ~11 questions a day. Every rule that shapes the answer is
still in the lean prompt.

## Known data problems

`known_limits.py` holds 16 entries, 11 of which the model states out loud. The
ones needing a decision:

- **`category_id` is NULL on every line row.** Bare "category" is refused as
  ambiguous; the model must ask accounting or POS.
- **Branch names contradict across languages.** Branch 2 is "Madinah Branch" in
  English and "الفرع الرياض" (Riyadh Branch) in Arabic, while a separate branch 5
  is named "Riyadh" and has no orders. Every answer names the branch it matched.
- **Delivery-app commission is not deducted.** Figures are what the customer
  paid. There is no commission field to subtract.
- **34% of line rows have no `app_line_id`.** Counted as products, per decision,
  and surfaced in a note.

## Not built

`aggregator_sales` — skipped by decision, design recorded in
`.claude/tasks/task-01-insight-mvp-query-layer.md`.

## Recommended production indexes

Not created. `pos_wags_payment_method` has only a primary key, so at 12M rows
every payments question is a full scan.

```sql
create index idx_pwpm_order_datetime on pos_wags_payment_method (order_datetime);
create index idx_pwpm_pos_id         on pos_wags_payment_method (pos_id);
create index idx_pwt_pos_app         on pos_wags_tree (pos_id, app_line_id);
create index idx_pwt_branch          on pos_wags_tree (branch_id);
create index idx_pwt_category        on pos_wags_tree (category_id);
create index idx_pwt_pos_category    on pos_wags_tree (pos_category_id);
```
