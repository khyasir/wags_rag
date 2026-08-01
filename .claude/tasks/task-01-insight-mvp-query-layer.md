# Task 01 — WAGS Insight MVP query layer

Status: **plan written, awaiting approval**
Repo changed: `wags_rag/` only. No edits in `wags/odoo/`, `odoo_dashboards_saas/`, or any Odoo module.

---

## Context

We need a chat app that answers POS business questions from Odoo 16 without ever letting the model touch a query. The model's only job is to pick a metric key plus parameters from a fixed whitelist, and later to write the sentence. Python owns the domain, the RPC, and the returns arithmetic. This keeps wrong numbers structurally impossible: if the model hallucinates a field name, code rejects it instead of querying it.

Everything below was verified against the live local database `step2_farhan` and a live XML-RPC session before writing this plan. What was verified, and what turned out to be wrong, is in the two sections that follow.

---

## Pre-flight findings (already verified, not assumptions)

Environment:
- `start-odoo` → `~/.start-odoo.sh`, venv `~/venv/odoo16`, port 8069, foreground with tee to `wags/logs/`. Odoo 16.0 currently running, PID on 8069.
- Postgres `/tmp:5432`, user `yasirrauf`, no password. Only one non-template DB: `step2_farhan`.
- XML-RPC login `admin` / `admin` → uid **2**. Confirmed working.

Schema — every field named in the task exists:
- `pos_wags`: all 18 exposed columns present. `order_datetime` and `order_datetime_dup` both `timestamp without time zone`; `date` and `effective_date` are `date`. Confirms the "only `order_datetime`" rule is meaningful — the decoys are real.
- `pos_wags_tree`: all 14 exposed columns present.
- `pos_wags_payment_method`: all 6 exposed columns present.
- Lookups all present with the flags the task depends on: `order_type_wags(is_return, is_aggregator)`, `pos_payment_method_wags(is_cash, is_span, is_aggregator)`, `branch_wags(name)`, `product_category_wags(name)`.
- `branch_wags.name` is **jsonb** (`{"en_US": "...", "ar_001": "..."}`). Keyword search must handle jsonb, not plain text.

read_group behaviour — **timezone question is settled**:
- `read_group` with `context={'tz':'Asia/Riyadh'}` and `order_datetime:day` returns `__range` = `{from: '2025-11-30 21:00:00', to: '2025-12-01 21:00:00'}` for the bucket labelled `01 Dec 2025`. 21:00 UTC = 00:00 Riyadh. So Odoo 16 **does** apply the context tz to date-granularity grouping. No Python correction of bucket boundaries is needed.
- We will still not trust the display label (`"01 Dec 2025"`, locale-dependent). We derive the canonical local label from `__range.from` converted to Riyadh. Deterministic and testable.
- m2o grouping returns `[id, display_name]` pairs; `branch_id` grouping works despite the jsonb name.

Indexes (this is where production pain will be):
- `pos_wags`: well covered — `(branch_id, order_datetime, state)`, `(order_datetime DESC, id DESC)`, `(session_order_link)`, and a partial index for unposted orders. `sales`, `returns`, `discounts`, `unposted_orders` should all be fine at 12M.
- `pos_wags_tree`: only `pkey`, `pos_id`, `product_id`. **No** `branch_id`, `category_id`, `pos_category_id`, `app_line_id`.
- `pos_wags_payment_method`: **only `pkey`**. No `pos_id`, no `order_datetime`, no `payment_method_id`. At 12M rows the `payments` metric is a full sequential scan on every call.
- Consequence: local timings prove nothing about production. I will report local timings as required, and separately report `EXPLAIN (ANALYZE, BUFFERS)` plans plus a concrete index recommendation, because the plan shape is what transfers to a 26M-row table. I will not create indexes.

Local data reality:
- 318 orders, 879 lines, 583 payments. Not 12M. All 318 are `state='validate'`.
- `order_datetime` spans 2024-09-25 → 2025-12-11. **All 318 rows have time exactly 00:00:00** — so `hour` grouping and any real tz-shift effect cannot be observed locally. The `__range` probe above is the substitute proof, and it is sufficient.
- `order_type` distribution: Dine In 165, Drive Through 142, Hungerstation 8, Delivery 2. **`Return` (is_return=true) has 0 orders.** Returns arithmetic therefore has no live data locally; it gets covered by stage-one unit tests with synthetic buckets instead.
- 67 of 318 orders have `session_order_link` NULL → `unposted_orders` has real local data.
- `order_type_wags` has 20 rows but 11 are junk duplicates with NULL flags. `pos_payment_method_wags` has 11 rows, 2 with NULL flags. Lookup cache must treat NULL `is_return` as false, not as unknown.

---

## Blockers — both now resolved

### B1 — `order.type.wags` missing DB column. **FIXED, re-verified.**

`read_group` grouped by `order_type` used to die with `psycopg2.errors.UndefinedColumn: column order_type_wags.hide_from_pos does not exist` (field declared in `wags-base/pos_wags/models/orders.py:1364`, never added to the table; Odoo `name_get` selects all stored fields).

You updated the DB. Re-verified: `hide_from_pos` present, and `order_type` grouping returns live buckets — Dine In 165 / Drive Through 142 / Hungerstation 8 / Delivery 2. `sales` is unblocked. No module update needed from me.

One thing that surfaced in the same probe: **1 order has `order_type = False`** (NULL, 8.26 untaxed). It has no `is_return` flag to read, so the returns split must bucket NULL order types as **non-return** and count them into gross. Silently dropping them would make gross ≠ sum of buckets. Covered by a stage-one test.

### B2 — date floor. **Removed per your instruction.**

Local data is old (2024-09-25 → 2025-12-11) and you want all of it queryable here, not just 2026.

`DATA_START` becomes **optional**:
- **unset or empty → no floor at all.** No clipping, no refusal, all history queried. This is the local/testing setting and what `.env` will hold.
- **set to a date → clip as originally specified.** Ranges wholly before it are refused without querying; partial overlaps are clipped and the answer says so.

So the guardrail stays fully implemented and shipped — it is just switched off locally by leaving the env var blank. `.env.example` documents both modes. Stage-one tests cover *both* paths explicitly: `data_start=date(2026,1,1)` proves clipping and refusal still work, `data_start=None` proves no floor is applied and nothing is silently dropped. That way turning it off here cannot rot the production behaviour.

---

## Decisions you made (encoded as constants, not scattered logic)

| Question | Decision | Where it lives |
|---|---|---|
| `app_line_id` NULL = 300/879 (34%) | NULL counts as a **product** line | `catalog.py` — `products` domain is `['|', ('app_line_id','<',100000), ('app_line_id','=',False)]`; `modifiers` is `('app_line_id','>=',100000)` |
| "category" unqualified | **accounting** `category_id` | `GROUP_BY['category']`; POS one stays as `pos_category`  |
| "last week" | **Monday–Sunday** (ISO) | `dates.py` helper, single source |
| Keyword search | matches **en_US and ar_001** | lookup cache resolvers |
| Date floor locally | **off** — `DATA_START` blank, all history queried | `dates.py`, `data_start=None` path |

The 34% NULL rate is high enough that I will still surface it: every `products` result carries a note stating how many lines had no `app_line_id`, so the choice stays visible in the answer rather than buried in code.

---

## Architecture

```
main.py       gradio ui + fastapi app, wiring only
rpc.py        xmlrpc connection, authenticate once, execute_kw wrapper, timeout
catalog.py    METRICS, GROUP_BY, FILTERS, lookup cache, system-prompt generator
dates.py      tz + range resolution, DATA_START clipping   (new — see note)
known_limits.py  known gaps, injected into the system prompt  (new — written, see below)
builder.py    pure: args in, QueryPlan out, no network
query.py      builder + rpc + returns maths + bucket cap
llm.py        two groq calls, tool schema, token capture
store.py      supabase writes
tests/test_builder.py
```

One addition to your layout: **`dates.py`**. Range resolution (`"last week"` → UTC bounds, clip at `DATA_START`) is the single most test-worthy piece of logic and it is needed by `builder.py`, which must stay import-free of everything networked. Putting it in its own pure module keeps `builder.py` readable and lets date logic be tested in isolation. It imports only `zoneinfo`/`datetime`. If you would rather it live inside `builder.py`, say so and I will inline it.

`builder.py` imports nothing from `rpc`, `llm`, `store`, `fastapi`. Enforced by a test that asserts its import set — so the MCP-tool wrapping stays possible later.

### `known_limits.py` — the open-questions file (already written)

Rule 9 says never produce a number that did not come from the database. There is a second failure mode it does not cover: a number that *did* come from the database but answers a different question than the user asked. Delivery-app sales is the clearest case — `amount_untaxed` is what the customer paid, not what WAGS receives after commission, and there is no commission field to deduct.

So unresolved questions live in code, not in a doc nobody reads:

- Each `Limit` carries the open question, why guessing hurts, `mvp` (exactly what the code does today), `warn` (plain user-facing caveat), and `triggers` (question shapes that should fire it).
- `catalog.build_system_prompt()` appends `known_limits.prompt_block()`. The model gives the number **and** states the caveat, in the user's language. It never invents a correction.
- Entries with an empty `warn` are engineer-only and stay out of the prompt, so it does not bloat.
- Golden-question tests assert the caveat actually fires — a limit that stops warning is a test failure, not a silent regression.

Answer a question → set `status="answered"`, fill `answer`, fix the code, drop the entry. 12 entries logged now: `aggregator_commission`, `charges_not_in_sales`, `finance_date_basis`, `app_line_id_null`, `null_order_type`, `duplicate_lookup_rows`, `payment_method_aggregator_flag`, `walkin_customer`, `missing_indexes_production`, `local_data_not_representative`, `return_reason_unused`, `token_cost_rate`.

### The whitelist contract

`catalog.py` is the single source of truth. Every metric declares its own model, date field, aggregates, forced domain, and which group-by/filter keys it permits:

```python
METRICS = {
  "sales": Metric(
      model="pos.wags",
      date_field="order_datetime",
      aggregates=["amount_untaxed", "amount_total", "amount_tax", "discount_amount"],
      forced_domain=[("state", "=", "validate")],
      always_group_by=["order_type"],        # returns split, never optional
      allows=("day","week","month","hour","branch","order_type","customer","employee","cashbox"),
  ),
  ...
}
```

`GROUP_BY` maps a key to the field **per model**, because `branch` is `branch_id` on all three but `product` only exists on `pos.wags.tree`:

```python
GROUP_BY = {
  "branch":       {"pos.wags":"branch_id", "pos.wags.tree":"branch_id", "pos.wags.payment.method":None},
  "product":      {"pos.wags.tree":"product_id"},
  "payment_method": {"pos.wags.payment.method":"payment_method_id"},
  ...
}
```

A `None` or missing entry is a rejection, not a fallback. `builder.build()` raises `WhitelistError("group_by 'product' is not available for metric 'sales'")` — a clear message the model can act on, and never a passthrough.

For `pos.wags.tree`, date and state filters go through the parent: `('pos_id.order_datetime','>=',...)`, `('pos_id.state','=','validate')`. Verified working, 17ms locally on 879 rows.

### Returns maths

`read_group` on `sales` returns one bucket per order type. `is_return` comes from the lookup cache (NULL treated as false), never from name matching. Per user-requested group, we fold the order-type dimension away:

```
gross_sales   = Σ untaxed where not is_return
return_amount = Σ untaxed where is_return
net_sales     = gross_sales - return_amount
order_count   = Σ count where not is_return
return_count  = Σ count where is_return
average_ticket = net_sales / order_count   (None when order_count == 0)
```

Same shape nested under branch/month when the user groups further. Never emit net alone.

### Guardrails, all enforced in code

- `state = validate` injected by `catalog`, not by the model, on every metric.
- Branch scope: `BRANCH_IDS` from env intersected with any model-supplied `branch_ids`; empty env means all branches.
- Date range always resolved to explicit UTC bounds. When `DATA_START` is set: wholly-before → refuse without querying; partial overlap → clip and set `plan.clipped=True`, which forces the answer to say so. When `DATA_START` is blank (local): no floor, no clip, no refusal.
- Bucket cap: if `len(buckets) > MAX_BUCKETS` we discard them and return a narrow-your-range error. Raw rows never reach the model; only aggregates do.
- RPC failure returns an explicit error object. `llm.py` call two is never invoked with a partial or fabricated result.

---

## Build order

### Stage one — no network, no model calls
1. `.env.example` (all keys, empty values, `DATA_START` documented as "blank = no floor, all history"), `.gitignore` with `.env`. Note: `.env` currently in the repo untracked and holds live Groq + Supabase keys — gitignoring it before any commit is the point.
2. `catalog.py` — metrics, group-by matrix, filters, `Lookups` dataclass with an injectable loader so it is testable without Odoo.
3. `dates.py` — Riyadh↔UTC, named ranges (today, yesterday, last week Mon–Sun, this/last month), optional `data_start` clipping (`None` = no floor).
4. `builder.py` — `build(args, *, today, tz_name, data_start, lookups) -> QueryPlan`. `data_start` accepts `None`.
5. `tests/test_builder.py` covering: UTC conversion both directions; `last week` = Mon–Sun; **with `data_start=date(2026,1,1)`** clip on partial overlap and refuse on wholly-pre-2026; **with `data_start=None`** no clip and no refusal for a 2024 range; unknown metric / unknown group_by / unknown filter key each rejected with a message; `state=validate` present on all seven metrics; branch scope forced; `products` includes NULL `app_line_id` and `modifiers` excludes it; tree metrics route date+state through `pos_id.`; returns maths on synthetic buckets including the zero-order division case and a **NULL order_type bucket counted as non-return**; `builder` import-set assertion.

**Gate: these pass before stage two starts.**

### Stage two — real database
6. `rpc.py` — authenticate once, cached uid, `RPC_TIMEOUT` on the transport, `execute_kw` wrapper.
7. `query.py` — plan → `read_group` → returns maths → bucket cap → result object with notes.
8. Run all seven metrics against the **full** local history (no date floor). Report per metric: bucket count, wall-clock ms, anything over 5s. Cross-check `sales` totals against a direct `psql` aggregate so the returns maths is proven against SQL, not just against itself.
9. Report `EXPLAIN (ANALYZE, BUFFERS)` for the `pos.wags.tree` parent-traversal and for `payments`, plus the missing-index list from the findings above. Recommendation only.

**Gate: timings reported to you before stage three.**

### Stage three — model and UI
10. `llm.py` — system prompt generated from `catalog.py` (today, tz, data start, metric/group-by/filter lists, branch + payment-method + order-type names, the answer rules). One tool `query_pos`. Call one → clarify or tool args; call two → prose in the question's language, Roman Urdu included. Capture prompt/completion tokens, model, latency from both.
11. `store.py` — Supabase inserts. Tables created by you from SQL I supply, never from code.
12. `main.py` — Gradio + FastAPI wiring only.
13. Golden questions: assert the tool args the model chose against expected args.

---

## Supabase SQL (for you to run, delivered in stage three)

```sql
create table if not exists chat_messages (
  id                bigserial primary key,
  session_id        text not null,
  role              text not null,
  content           text,
  tool_args         jsonb,
  tool_result       jsonb,
  prompt_tokens     int,
  completion_tokens int,
  model             text,
  latency_ms        int,
  cost_usd          numeric(12,6),
  created_at        timestamptz not null default now()
);
create index if not exists idx_chat_messages_session on chat_messages (session_id, created_at);

create table if not exists settings (
  key        text primary key,
  value      text not null,
  updated_at timestamptz not null default now()
);
insert into settings (key, value) values
  ('daily_query_limit', '10'),
  ('data_start',        '2026-01-01'),
  ('max_buckets',       '200')
on conflict (key) do nothing;
```

---

## Verification

- Stage one: `pytest tests/ -v`. No network, no Odoo, no Groq. Every guardrail has a test that fails if the guardrail is removed.
- Stage two: script prints a metric × (buckets, ms) table against `step2_farhan` via live XML-RPC. Plus EXPLAIN output.
- Stage three: golden-question table — question, expected tool args, actual tool args, pass/fail. Then manual Gradio pass on the running app.

---

## Open items I am not guessing on

Longer-term unknowns now live in `known_limits.py` and are surfaced to users at answer time. Only the two build decisions below actually block me.

1. **Metric I think the whitelist is missing**: `aggregator_sales`. `order.type.wags.is_aggregator` flags Hungerstation/Jahez/Chefz/Keeta (locally Hungerstation has 8 orders, 770.43 untaxed). Delivery revenue is normally the first question after total sales, and today it is only reachable as a manual `order_type_ids` filter. Implementation is one `catalog.py` entry plus a `scope_flag` that resolves to `("order_type","in",[ids])` from the existing lookup cache — ~15 lines, 2 tests, no new code path, returns maths reused as-is. The payment-method `is_aggregator` flag is empty on all 11 rows so it is not used (logged as `payment_method_aggregator_flag`). Add now, or task 02?
2. `dates.py` as a separate module — confirm or tell me to inline it into `builder.py`.
3. Confirm `DATA_START` should stay **implemented but blank locally** (my reading of your instruction), rather than deleted from the code. Blank means the production guardrail still ships tested; deleting it means re-adding it later untested.

---

## Change log

### Stage one — done. 67 tests pass.

Command: `.venv/bin/python -m pytest tests/ -q` → `67 passed in 0.04s`

Files created:

| File | What it does |
|---|---|
| `.gitignore` | `.env` excluded. It holds live Groq + Supabase keys. |
| `.env.example` | All keys, empty values, `DATA_START` documented as blank = no floor. |
| `.env` | Appended `ODOO_*`, `TZ_NAME`, `DATA_START=""` (blank), limits. Existing keys untouched. |
| `config.py` | Env loading + `Settings`. Parses the existing `export KEY="value"` style. |
| `dates.py` | Riyadh↔UTC, named periods, optional floor. Half-open bounds. |
| `catalog.py` | The whitelist: 7 metrics, group-by matrix, filters, lookup cache, prompt generator. |
| `builder.py` | Pure `build()` → `QueryPlan`, plus `fold_returns` / `fold_returns_by_group`. |
| `known_limits.py` | 12 open gaps, injected into the system prompt. |
| `tests/test_builder.py` | 67 tests. |

Deviations from the stated file layout, each deliberate:

- **`dates.py`** — date logic is the most bug-prone part and `builder.py` must stay import-clean. Own module, own tests.
- **`config.py`** — `llm.py` and `store.py` need settings and must not import the Odoo client to get them. Putting env reads in `catalog.py` would pollute the whitelist.
- **`known_limits.py`** — see the section above.
- **Returns maths lives in `builder.py`, not `query.py`.** It has to be unit-testable without Odoo, which matters more than usual here because the local database has zero return orders. `query.py` calls it.

Constraint discovered while building, now encoded: **Odoo `read_group` cannot group by a related field.** Verified live — `pos_id.order_datetime:day` and `pos_id.branch_id` both raise. So `products`/`modifiers` cannot be grouped by any date grain, and `payments` cannot be grouped by branch. Both are explicit rejections with an explanation, never a silent substitution. Date *filtering* through the parent works and is used.

### Stage two — done. All 12 metric calls pass, nothing over 5s.

Command: `.venv/bin/python stage2_report.py`

Two bugs found and fixed against the live database. Both would have produced wrong numbers rather than errors, which is the dangerous kind.

**1. Archived lookup records were invisible — this one silently corrupted sales.**

The only order type flagged `is_return` (id 2, "Return") is **archived**. So is the only `is_cash` payment method (id 9, "Cash RM-TWN"). Odoo hides archived records by default, so the lookup cache loaded 0 return types and 0 cash methods. Effect: `returns` refused to run, `is_cash` refused to run, and — worst — `is_return_type()` answered False for every return order, which would have folded returns into **gross sales** with no error and no warning. Exactly the failure rule 8 exists to prevent.

Fix: `load_lookups()` reads with `context={'active_test': False}`. Historical orders keep pointing at archived types, so the cache must contain them to interpret them. Order types went 5 → 15 (1 return), payment methods 3 → 11 (1 cash).

**2. XML-RPC cannot carry a NULL aggregate. Transport switched to JSON-RPC.**

`discounts` failed with `TypeError: cannot marshal None unless allow_none is enabled`. Cause: `coupon_amount` and `voucher_amount` are NULL on all 318 orders, so `read_group`'s sum returns None, and Odoo 16 hardcodes `allow_none=False` when marshalling XML-RPC replies (`odoo/addons/base/controllers/rpc.py`, `_xmlrpc`). Server-side, so no client setting fixes it.

`rpc.py` now uses `/jsonrpc` — Odoo's own equally-supported endpoint, same `execute_kw`, same auth, carries null natively. Verified on the exact failing call. This is a deliberate deviation from the spec's "XML-RPC"; the alternative was a metric that can never run.

**Timings** (`step2_farhan`, full history, no date floor — 318 orders / 879 lines / 528 payments):

| Call | Buckets | ms |
|---|---|---|
| sales | 5 | 4 |
| sales by month | 10 | 14 |
| sales by branch | 7 | 12 |
| sales by hour | 47 | 14 |
| returns | 1 | 3 |
| discounts | 1 | 3 |
| unposted_orders | 1 | 2 |
| payments by method | 7 | 4 |
| payments cash only | 1 | 4 |
| products by product | 97 | 23 |
| products by category | 1 | 5 |
| modifiers | 6 | 5 |

Connect 126ms, lookups 28ms. Nothing over 5s — but see the index note below before reading anything into that.

**Cross-check against direct SQL — every figure matches exactly:**

| Figure | App | `psql` |
|---|---|---|
| net / gross untaxed | 961654.64 | 961654.64 |
| total with VAT | 1105675.38 | 1105675.38 |
| tax | 144218.53 | 144218.53 |
| discount | 197.79 | 197.79 |
| order count | 318 | 318 |
| returns | 0.0 / 0 orders | NULL / 0 orders |
| payments | 1118879.59 / 528 lines | 1118879.59 / 528 lines |

**Timezone proven end-to-end.** Local `order_datetime` values are all 00:00:00 UTC; hour buckets come back labelled `03:00` Riyadh. Month bucket `2025-12` = 411122.0, identical to a `custom` 2025-12-01→2025-12-11 range. Bucket labels are derived from `__range.from`, not from Odoo's locale display string.

**Guardrails re-verified against the live database, not just in unit tests:** unknown metric rejected, injected `domain` argument rejected, `products` by day rejected, `payments` by branch rejected, unmatched branch keyword rejected with the real branch list.

**EXPLAIN findings — the part that matters for production.**

Local plans are all Seq Scan + Hash Join, which is correct for 900-row tables and tells us nothing on its own. The *shape* is what transfers:

- `sales` / `returns` / `discounts` / `unposted_orders` — index scan on `idx_pos_wags_order_datetime`. Fine at 12M.
- `products` / `modifiers` — seq scan on `pos_wags_tree` + hash join to the parent. `pos_wags_tree_pos_id_index` exists, so a narrow date range should drive the join from the parent side. A wide range will not.
- `payments` — this is the real problem. `pos_wags_payment_method` has **only a primary key**. The date filter lands on `pay.order_datetime` with no index on it, and the state filter joins to the parent on `pay.pos_id` with no index on that either. At 12M rows every payments question is a full scan plus a full-table hash join.

Recommended indexes, in priority order. **Not created — recommendation only:**

```sql
create index idx_pwpm_order_datetime on pos_wags_payment_method (order_datetime);
create index idx_pwpm_pos_id         on pos_wags_payment_method (pos_id);
create index idx_pwt_pos_app         on pos_wags_tree (pos_id, app_line_id);
create index idx_pwt_branch          on pos_wags_tree (branch_id);
create index idx_pwt_category        on pos_wags_tree (category_id);
create index idx_pwt_pos_category    on pos_wags_tree (pos_category_id);
```

**Gate: stage three has not started.** Timings above are the report the plan says you get first.

### Scenario run — 22 real questions, 10 attack cases

`.venv/bin/python scenarios.py` (pins "today" to 2025-12-11 so relative periods land inside the local data window)

**22/22 answered. 10/10 refusals held.** Refused: unknown metric, injected `domain`, injected `model`, products-by-day, payments-by-branch, unknown branch, `"1 OR 1=1"` as a branch id, two time groupings, wrong-metric filter, four groupings.

Sample answers that look right: total net 961,654.64 / 318 orders / avg ticket 3024.07 · Dec 2025 411,122.00 / 45 orders · Hungerstation 770.43 / 8 orders · cash taken 9,118.11 / 52 lines · unposted 67 orders / 1,489.57 · top products Cappuccino 115, Espresso 92, Americano 75.

**Four defects the scenario run exposed that the metric run had hidden.** All logged in `known_limits.py` (now 16 entries, 11 user-facing). Two need a decision.

1. **`accounting_category_empty` — needs your decision.** `category_id` is NULL on **879 of 879** line rows. 100%. So `group_by: category` — the meaning we agreed for the bare word "category" — returns one bucket, `Not set`, always. `pos_category_id` is populated on 497 of 879 and does work. The agreed default is currently pointing at an empty column.

2. **`branch_names_contradict_across_languages` — needs your decision.** Branch 2 is `Madinah Branch` in English and `الفرع الرياض` (*Riyadh* Branch) in Arabic. Branch 6 is `Madinah Branch New` / `الفرع الرئيسي الرياض` (*Riyadh Main* Branch). Separate branch 5 is named `Riyadh` and has zero orders. Net effect: "Riyadh sales" in English → 0.00; the same question in Arabic → 130.43. Both faithful to the data. This is a branch-setup problem, but it will be read as our bug.

3. **`hour_grouping_is_not_hour_of_day`.** Odoo groups hour per calendar date, so "busiest time of day" answers `2025-11-22 03:00` — the single busiest hour that ever occurred, not the busiest hour across days. Folding to 0–23 has to happen in Python, which needs every hour bucket first and would breach the bucket cap on a long period. Currently warned about, not solved.

4. **`lines_without_a_product`.** 27 line rows have no product, grouped as `Not set`. Kept rather than dropped so line totals still add up.

### Your three decisions — all implemented, 94 tests pass

**1. Bare "category" now refuses instead of guessing.** The key `category` was removed. `accounting_category` and `pos_category` are separate explicit keys, and `AMBIGUOUS_GROUP_BY` in `catalog.py` makes the bare word raise:

> `'category' is ambiguous — it could mean accounting_category or pos_category. Ask which one is wanted, then use that key.`

Verified live: `accounting_category` → 1 bucket, `Not set`, 1462 units (the column is NULL on all 879 rows). `pos_category` → 9 buckets, `Not set` 976 / Black beverages 402 / All 36.

**2. Ambiguous branch keywords split per branch instead of summing.** When a keyword resolves to more than one branch, `builder` adds branch grouping automatically and names every match. If three groupings were already requested a fourth cannot be added, so it says so rather than silently merging.

```
'madinah' -> grouped_by=('branch',)  Madinah Branch  net=130.43  2 orders
             note: 'madinah' matched 2 branches: Madinah Branch,
                   Madinah Branch New. Each one is reported separately below.
'twn'     -> grouped_by=()           net=960,165.08  251 orders
             note: 'twn' was matched to the branch RUH-TWN-A.
```

Only one Madinah row appears because Madinah Branch New has no orders — `read_group` returns no bucket for an empty group. The note names both, so nothing is hidden.

**3. `hour_of_day` built.** Odoo cannot group hour-of-day, so `catalog.DERIVED_DATE_GRAINS` maps it to a server-side `hour` grouping and the fold to 0–23 happens in Python, keyed off `__range.from` converted to Riyadh. Verified: 47 raw hour buckets fold to one `03:00` bucket, matching the fact that every local order is stored at 00:00 UTC. `bucket_count` now reports the folded count, not the raw fetch. Long periods are guarded by `MAX_RAW_HOUR_BUCKETS = 2000` with a plain-words error.

Also fixed while testing branch keywords: matching was plain substring, so **`"twn branch"` and `"RUH TWN A"` both failed**, and `"a"` matched all four branches. Now token-based — separators normalised, filler words (`branch`, `store`, `فرع`) dropped, single-character tokens must match a whole word. 14 tests cover the variants.

### Stage three — built. MVP complete.

| File | What it does |
|---|---|
| `llm.py` | Two Groq calls, tool schema generated from `catalog.py`, exact token capture, rate-limit backoff |
| `store.py` | Supabase REST writes, best-effort with visible error list |
| `main.py` | `App.ask()` orchestration + FastAPI `/health` and `/ask` + Gradio chat |
| `golden_run.py` | 47 questions through the full pipeline, writes a reviewable report |
| `run_until_done.sh` | Re-runs the suite until complete, resuming across quota resets |
| `supabase_schema.sql` | The schema you ran |

**Verified end to end**, real Groq + real Odoo + real Supabase:

```
Q: What were our total sales?
   tool_args: {'metric': 'sales', 'period': 'all_time'}
   answer:  net 961,654.64, gross 961,654.64, returns 0.0, VAT 144,218.53,
            318 orders, average ticket 3,024.07
   logged:  Supabase rows 3, 4, 5

Q: What is our busiest time of day?
   tool_args: {'metric': 'sales', 'period': 'this_month',
               'group_by': ['hour_of_day']}
   answer:  busiest is 03:00 with net 411,122.00, 45 orders
```

Supabase writes confirmed against the live project: insert 201, jsonb round-trips, `role` CHECK rejects a bad value with 400, `cost_usd` stays NULL.

### The blocker you will care about most: Groq's free tier is 100,000 tokens per DAY

Not per minute. The first golden run died at question 21 with:

```
Rate limit reached ... on tokens per day (TPD): Limit 100000, Used 99213
```

Headers confirm the two separate limits — per-minute `x-ratelimit-limit-tokens: 12000` (never a problem) and the daily 100k (the real ceiling).

The cause was mine: the system prompt was ~4,200 tokens and the spec has call two receive **the same** prompt, so every question cost ~8,500 tokens. That capped the whole app at **~11 questions a day**.

Fixed by giving call two its own lean prompt (`catalog.build_answer_prompt`). Call two only turns a finished result into prose — it cannot query, so the metric list, group-by keys, filter keys and period names were dead weight. Also dropped trigger phrases from the known-limits block and de-duplicated the lookup name lists.

| | Before | After |
|---|---|---|
| Call one prompt | ~4,200 | ~1,378 |
| Call two prompt | ~4,200 | ~593 |
| Per question | ~8,500 | **~1,970** |
| Questions per day on free tier | ~11 | **~44** |

**This is a deliberate deviation from the spec's "call two gets the same system prompt".** Every rule that shapes the answer is still in the lean prompt — validated orders only, net before VAT, charges excluded, returns stated together, known limits. Say the word and I will revert it, but the app answers 4× more questions this way.

Even so, 47 questions × ~2,000 = ~94,000 tokens, which is almost exactly one full day's allowance. `DAILY_QUERY_LIMIT=10` in your settings table is about right for the free tier. Worth considering the Dev tier if this goes to real users.

Also fixed: **"Sales per branch per month" crashed** with `keys must be str, int, float, bool or None, not tuple`. Multi-grouping produced tuple dict keys, which cannot be JSON-serialised, so the result could reach neither Supabase nor the model. Now joined into one readable string (`2025-12 / RUH-TWN-A`) by `builder.group_key`.

### Golden run — in progress, resumable

First attempt: **19 PASS, 1 crash (the tuple bug), 27 killed by the daily quota.** Both causes fixed.

A background runner (`run_until_done.sh`) is now retrying every 30 minutes. `golden_run.py` resumes from `golden_pairs.json`, so each attempt only pays for questions not yet answered and tokens are never spent twice. Results are written after **every** question, so a quota stop cannot lose paid work.

Two outputs for you:

- **`golden_results.md`** — one section per question: the tool arguments the model chose, the figures the database returned, the caveats attached, the sentence written, token counts, and `PASS` / `BLOCKED` / `CLARIFY` / `ERROR` against what I expected. Anything that differs from expectation is flagged ⚠️ and listed in a "needs your review first" table at the top.
- **`golden_pairs.json`** — the same data machine-readably, so you can correct `tool_args` in place and turn it into the golden test set.

`BLOCKED` is frequently the correct outcome — 6 questions are *meant* to be refused (profit margin, raw order rows, user emails, products-per-day, payments-by-branch, Jeddah branch). The report marks expected-vs-actual so a correct refusal is not mistaken for a failure.

Progress: `tail -f golden_run.log`

### Not built, by decision

`aggregator_sales` — you said skip. Moved to task 02. Design is recorded in the "Open items" section above so it can be picked up as-is.
