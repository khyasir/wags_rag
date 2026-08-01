"""FRONT END — chat in the browser.

    python app_frontend.py            # http://localhost:7860

You type a question. For a data question the page shows the figures as a table,
with the exact spec and query folded underneath so you can always check what
ran. Greetings and refusals are answered in words, with no Odoo call.

All the thinking lives in main.py, odoo_db.py and fields.py. This file renders.
"""

from __future__ import annotations

import json

import gradio as gr

import odoo_db
from main import Router, order_lines, sort_col
from odoo_db import OdooError, QueryError, build_query, summarise

router: Router | None = None

#: Invisible marker on real answers, so clarifying replies can be counted.
ANSWERED = "<!--answered-->"
#: Ask at most this many clarifying questions before giving up.
CLARIFY_LIMIT = 5


def _num(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _md_table(data: dict, sort_key: str) -> str:
    """The output as a markdown table, biggest first."""
    if not data:
        return "_no rows_"
    if list(data) == ["overall"]:
        rows = "\n".join(f"| {k.replace('_', ' ')} | {_num(v)} |"
                         for k, v in data["overall"].items())
        return f"| figure | value |\n|---|--:|\n{rows}"

    cols = list(next(iter(data.values())))
    sc = sort_key if sort_key in cols else cols[0]
    ordered = sorted(data.items(), key=lambda kv: -(kv[1].get(sc) or 0))
    shown, hidden = ordered[:25], max(0, len(ordered) - 25)

    head = "| group | " + " | ".join(c.replace("_", " ") for c in cols) + " |"
    sep = "|---|" + "--:|" * len(cols)
    body = "\n".join("| " + str(k) + " | " +
                     " | ".join(_num(v.get(c)) for c in cols) + " |"
                     for k, v in shown)
    more = f"\n\n_… and {hidden} more, sorted by {sc}_" if hidden else ""
    return f"{head}\n{sep}\n{body}{more}"


def _order_md(res: dict) -> str:
    header, lines = order_lines(res)
    head = "| field | value |\n|---|--:|\n" + "\n".join(
        f"| {k.replace('_', ' ')} | {_num(v)} |" for k, v in header.items())
    if lines:
        lt = ("| product | kind | qty | unit price | total |\n"
              "|---|---|--:|--:|--:|\n" + "\n".join(
                  f"| {l['product']} | {l['kind']} | {_num(l['qty'])} | "
                  f"{_num(l['unit_price'])} | {_num(l['total'])} |" for l in lines))
    else:
        lt = "_no lines_"
    return f"**Order {header.get('reference')}**\n\n{head}\n\n{lt}"


def _to_msgs(history) -> list:
    """Normalise Gradio history (messages dicts or (user, bot) tuples)."""
    msgs = []
    for item in history or []:
        if isinstance(item, dict) and item.get("role"):
            msgs.append({"role": item["role"],
                         "content": str(item.get("content") or "")})
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            u, a = item
            if u:
                msgs.append({"role": "user", "content": str(u)})
            if a:
                msgs.append({"role": "assistant", "content": str(a)})
    return msgs


def _trailing_clarifications(history) -> int:
    """How many recent assistant replies in a row were NOT real answers."""
    n = 0
    for msg in reversed(_to_msgs(history)):
        if msg["role"] != "assistant":
            continue
        if ANSWERED in msg["content"]:
            break
        n += 1
    return n


def answer(question: str, history) -> str:
    """One turn. Returns markdown for the chat bubble."""
    if not question or not question.strip():
        return "Ask me something about POS sales."

    try:
        d = router.decide(question, history=_to_msgs(history))
    except Exception as exc:                            # noqa: BLE001
        return f"**The model call failed.**\n\n```\n{str(exc)[:400]}\n```"

    meta = (f"`{d['prompt_tokens']}+{d['completion_tokens']} tokens · "
            f"{d['ms']}ms · {router.label}`")

    # -- not a data question: reply in words (clarify, greet, refuse) ----
    if "reply" in d:
        if _trailing_clarifications(history) >= CLARIFY_LIMIT:
            return ("Sorry, I still couldn't understand that after several tries. "
                    "Try naming a figure and a period, for example "
                    "*net sales last week by branch*.")
        return d["reply"]

    # -- a single order by reference / transaction id --------------------
    if "find_ref" in d:
        try:
            res = router.odoo.find_order(d["find_ref"])
        except OdooError as exc:
            return f"**The database call failed.** {exc}"
        if not res:
            return f"I couldn't find an order matching **{d['find_ref']}**."
        return f"{_order_md(res)}\n\n{meta}\n{ANSWERED}"

    # -- a data query ----------------------------------------------------
    spec = d["query_spec"]
    try:
        query = build_query(spec, router.lookups)
    except QueryError as exc:
        return (f"**I can't run that.** {exc}\n\n"
                f"<details><summary>what the model chose</summary>\n\n"
                f"```json\n{json.dumps(spec, indent=2)}\n```\n\n{meta}\n\n"
                f"</details>\n{ANSWERED}")

    try:
        rows, ms = router.odoo.run(query)
    except OdooError as exc:
        return f"**The database call failed.** {exc}"

    out = summarise(rows, query, router.lookups)
    body = _md_table(out, sort_col(query))
    return f"""{body}

<details><summary>1 · what the LLM chose</summary>

```json
{json.dumps(spec, indent=2)}
```
{meta}
</details>

<details><summary>2 · the exact query sent to Odoo</summary>

```
model    {query.model}
domain   {chr(10).join('         ' + str(t) for t in query.domain).strip()}
fields   {query.fields}
groupby  {query.groupby}
period   {query.period_label}
```

```sql
{query.as_sql()}
```
</details>

<details><summary>3 · raw rows — {len(rows)} buckets, {ms:.0f}ms via {router.odoo.last_transport}</summary>

```json
{chr(10).join(json.dumps({k: v for k, v in r.items() if k not in ('__domain', '__range')}, default=str, ensure_ascii=False) for r in rows[:12])}
```
</details>
{ANSWERED}"""


def build() -> gr.Blocks:
    with gr.Blocks(title="WAGS Insight", fill_height=True) as ui:
        gr.Markdown(
            f"### WAGS Insight\n"
            f"`{odoo_db.ODOO_DB}` · {odoo_db.TZ_NAME} · today "
            f"**{odoo_db.TODAY}** · floor **{odoo_db.DATA_START or 'none'}** · "
            f"{router.label}\n\n"
            f"Ask about sales, products, payments or a single order. Every answer "
            f"carries the query that produced it.")
        gr.ChatInterface(
            answer,
            examples=["total sales",
                      "net sales last week by branch",
                      "top 5 products last week",
                      "how much did we take by payment method",
                      "how many orders were not posted to a session",
                      "show me order POS-5525156"],
        )
    return ui


if __name__ == "__main__":
    router = Router()
    print(f"db={odoo_db.ODOO_DB} uid={router.odoo.uid} "
          f"today={odoo_db.TODAY} model={router.label}")
    build().launch(server_name="0.0.0.0", server_port=7860, inbrowser=True,
                   share=True)
