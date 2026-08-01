# Task 04 — Constrained-dynamic query engine

Repo changed: `wags_rag` only.

## Goal

Replace the fixed 7-metric router with a "constrained dynamic" engine. The LLM
fills a small form (a spec); the code validates it against `fields.py` and builds
the real Odoo call. This covers almost any counting/summing question without
hardcoding each one, while staying safe (the LLM can never name a field outside
`fields.py` or use one in the wrong role).

## Decisions (from the user)

- Transport: Odoo RPC (never raw SQL).
- Date floor: driven by `DATA_START` env (blank in dev = full history; production
  ships `2026-01-01`). Enforced in `date_range`.
- Clarify loop: ask up to 5 times, then apologise (front end).
- Single-order lookup: supported via a second tool.

## The spec the LLM fills (tool `query_data`)

    model       pos.wags | pos.wags.tree | pos.wags.payment.method   (required)
    measures    [measure field names]        # numbers to total; optional
    group_by    [dimension names + day/week/month]   # at most 2
    filters     [{field, value}]             # dimension equality, by name
    period      one of PERIODS               # default all_time
    start_date/end_date                       # custom
    line_type   product | modifier           # pos.wags.tree only
    unposted    bool                         # pos.wags only

Second tool `find_order(ref)` looks up ONE order by reference or transaction id.

## What was built

### fields.py
- Field registry with roles (forced / special / dimension / measure), used as the
  single source for both the tool enums and the build-time validation.
- `always_forced()`, `date_field()`, `state_field()`, `SPECIAL_FILTERS`.

### odoo_db.py
- `DATA_START` floor + `date_range` clamping (refuses ranges entirely before it,
  clamps others, floors all_time).
- `build_query(spec, lookups)` — validates spec keys, model, measures (must be
  measures of the model), group_by (dimensions or date grains), filters
  (dimensions, resolved to ids via lookups or `<field>.name ilike`), applies
  forced `state=validate`, the date window, and special filters (products vs
  modifiers, unposted). Orders always fetch the full sales set and split returns.
- `_truthy()` coerces string booleans ('true'/'false') the LLM sometimes sends.
- `summarise()` — returns-fold for pos.wags (net/gross/returns/vat/total/
  discounts/coupons/vouchers/orders/return_count/average_ticket); plain measure
  sums for the other models.
- `Odoo.find_order(ref)` — order header + product/modifier lines.
- `_show_build()` still prints the step-by-step build to the terminal (SHOW_QUERY).

### main.py
- `tool_schemas()` generates `query_data` + `find_order` enums from `fields.py`.
- `system_prompt()` describes each model's dimensions/measures, the periods, the
  routing rules, refusals, and the DATA_START floor.
- `Router.decide(question, history)` returns one of `{reply}`, `{query_spec}`,
  `{find_ref}`; accepts chat history for multi-turn clarify.
- `order_lines()` shared order formatter; CLI `ask()` handles all three outcomes.

### app_frontend.py
- Renders data answers as markdown tables with the spec/query/raw folded under.
- Renders a single order (header + line table).
- Clarify-then-refuse: counts trailing clarifying replies via an invisible
  `<!--answered-->` marker; after 5 it apologises.

### golden.py
- Pairs rewritten to the new spec shape; checks routing + spec (order-insensitive
  lists, bool-aware) + refusals + the single-order path.

## Verification (2026-08-01)

- `build_query` unit checks (no network): all models build correctly; bad
  measure / bad group / smuggled `domain` rejected; 2026 floor refuses Dec 2025
  and floors all_time.
- `python golden.py` end-to-end: 18/18 after the `unposted` string-bool fix.

## Notes / follow-ups
- Dev DB (`step2_farhan`) holds 2025 data; with `DATA_START` blank, `all_time`
  works but relative periods (today/last week) target 2026 and come back empty.
  Set `TODAY` in `.env` to test relatives against 2025 data.
- Payments `pos_id` has no index — payment questions are slower at scale.
- `MAX_BUCKETS` / `DAILY_QUERY_LIMIT` env values exist but are not enforced yet.
