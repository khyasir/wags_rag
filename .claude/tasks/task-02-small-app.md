# Task 02 — small app, 4 files, no Supabase

Status: **plan written, awaiting approval**

## What changes

Task 01 became 20 files. This strips it to 4, drops Supabase entirely, and makes
the query and its raw result visible on screen — which is the thing that was
missing when the app felt like a black box.

## Files

```
app_frontend.py   Gradio UI. Shows the answer AND the exact query + raw result.
main.py           LLM router: decides tool-call or plain reply. Two Groq calls.
odoo_db.py        Odoo connection, the allowed metrics, query building, execute.
golden.py         Input/output pairs. You edit the output structure here.
```

Deleted: `store.py`, `supabase_schema.sql`, `config.py`, `dates.py`,
`catalog.py`, `builder.py`, `query.py`, `rpc.py`, `llm.py`, `known_limits.py`,
`scenarios.py`, `stage2_report.py`, `golden_run.py`, `trace.py`,
`debug_chat.py`, `run_until_done.sh`, `tests/`.

That is ~3,900 lines down to roughly 700.

## The router

One decision, made by the LLM itself, exactly as you described:

```
question -> LLM call 1
              |
              +-- no tool needed  -> LLM replies in words. No Odoo. Done.
              |
              +-- tool needed     -> tool args
                                     -> odoo_db builds the query
                                     -> Odoo runs it
                                     -> LLM call 2 writes the answer
```

`tool_choice="auto"` already gives this. The prompt tells it not to call the
tool for greetings, thanks, or "what can you do".

## Seeing the query and the result

Every tool-backed answer shows three collapsible blocks under it:

1. **What the LLM chose** — the raw tool arguments
2. **The exact query sent to Odoo** — model, domain, fields, groupby, context,
   and the equivalent SQL
3. **The raw result** — exactly what Odoo returned, before any formatting

So nothing between your question and the number is hidden.

## What I am keeping from task 01, and why

The whitelist. `odoo_db.py` keeps a fixed set of metrics and group-by keys, and
still forces `state = validate` on every query. Not for tidiness — without it
the model can name any field or model, and a hallucinated field silently
produces a wrong number instead of an error. It is about 120 lines.

Also kept because the live data breaks without them:

- JSON-RPC, not XML-RPC (Odoo 16 crashes marshalling NULL aggregates)
- `active_test=False` on lookups (the only return order type is archived)
- returns split by the `is_return` flag, never by name

Dropped: the date floor, branch scope, bucket caps, `known_limits`, hour-of-day
folding, keyword resolution for products. All re-addable.

## golden.py

A plain list you can edit:

```python
GOLDEN = [
    {
        "question": "What were our total sales?",
        "expect_tool": True,
        "expect_args": {"metric": "sales", "period": "all_time"},
        "output": {                      # <- you change this structure
            "net_sales": 961654.64,
            "orders": 318,
        },
    },
]
```

`python golden.py` runs each question through the real pipeline and prints
expected vs actual for tool args and output. Change `output` to whatever shape
you want the answers to take, and it becomes the target.

## Verification

- `python golden.py` — every pair, expected vs actual
- `python app_frontend.py` — chat at localhost:7860, with query and result shown
- Greeting test: "hi" must not touch Odoo

## Open question

Whether the answer format should change at the same time — right now it is
`Net sales: 961654.64, Gross sales: 961654.64, ...` with no comparison and no
sense of good or bad. Structuring that is what `golden.py` is for, but I need
your target shape before I can aim at it.
