"""WAGS Insight — Gradio UI on a FastAPI app. Wiring only.

    .venv/bin/python main.py            # http://localhost:7860

One turn:
    call one  -> clarifying question, or tool arguments
    query     -> builder + Odoo + returns maths (no model involved)
    call two  -> the sentence, in the language asked
    store     -> both calls logged to Supabase with exact token counts

The logic lives in builder/query/llm/store. This file only connects them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import gradio as gr
from fastapi import FastAPI

import rpc
from config import Settings, settings
from llm import Llm
from query import QueryResult, QueryRunner
from store import Store

# Local data stops in Dec 2025 while the real clock says 2026, so relative
# periods would all be empty. Point "today" at the last day with data when there
# is no date floor, i.e. local testing. With a floor set, use the real date.
LOCAL_TEST_TODAY = date(2025, 12, 11)


@dataclass
class Turn:
    """Everything one question produced. Also what the golden run records."""

    question: str
    session_id: str
    tool_args: Optional[dict] = None
    clarification: str = ""
    answer: str = ""
    result: Optional[QueryResult] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    store_ids: list = field(default_factory=list)
    error: str = ""

    @property
    def status(self) -> str:
        if self.error:
            return "ERROR"
        if self.clarification:
            return "CLARIFY"
        if self.result is not None and not self.result.ok:
            return "BLOCKED"
        return "PASS"


class App:
    def __init__(self, cfg: Optional[Settings] = None):
        self.cfg = cfg or settings()
        self.odoo = rpc.connect(self.cfg)
        self.lookups = self.odoo.load_lookups()
        self.today_fn = (lambda: date.today()) if self.cfg.has_date_floor \
            else (lambda: LOCAL_TEST_TODAY)
        self.runner = QueryRunner(self.odoo, self.cfg, self.lookups,
                                  today_fn=self.today_fn)
        self.llm = Llm(self.cfg, self.lookups, today_fn=self.today_fn)
        self.store = Store(self.cfg)

    def refresh_lookups(self) -> None:
        """Re-read the reference models without a restart."""
        self.lookups = self.odoo.load_lookups()
        self.runner.lookups = self.lookups
        self.llm.lookups = self.lookups

    # -------------------------------------------------------------------- turn

    def ask(self, question: str, history: Optional[list] = None,
            session_id: str = "") -> Turn:
        session_id = session_id or uuid.uuid4().hex[:12]
        turn = Turn(question=question, session_id=session_id)

        turn.store_ids.append(
            self.store.log_message(session_id=session_id, role="user",
                                   content=question))

        # --- call one
        try:
            one = self.llm.choose_query(question, history=history)
        except Exception as exc:                        # noqa: BLE001
            turn.error = f"The model call failed: {exc}"
            # Mirror it into answer so the chat window and the API both show
            # something. Never a figure — rule 9.
            turn.answer = turn.error
            return turn

        turn.prompt_tokens += one.usage.prompt_tokens
        turn.completion_tokens += one.usage.completion_tokens
        turn.latency_ms += one.usage.latency_ms

        if not one.wants_query:
            turn.clarification = one.clarification or (
                "Could you say a bit more about what you need?")
            turn.answer = turn.clarification
            turn.store_ids.append(self.store.log_message(
                session_id=session_id, role="assistant",
                content=turn.clarification,
                prompt_tokens=one.usage.prompt_tokens,
                completion_tokens=one.usage.completion_tokens,
                model=one.usage.model, latency_ms=one.usage.latency_ms))
            return turn

        turn.tool_args = one.tool_args

        # --- the query. No model involvement.
        result = self.runner.run(one.tool_args)
        turn.result = result
        turn.store_ids.append(self.store.log_message(
            session_id=session_id, role="tool",
            tool_args=one.tool_args, tool_result=result.for_model(),
            latency_ms=int(result.elapsed_ms)))

        # --- call two. Runs even on failure, so the user is told it failed
        # --- rather than being handed silence.
        try:
            two = self.llm.write_answer(question, result.for_model(),
                                        tool_args=one.tool_args)
        except Exception as exc:                        # noqa: BLE001
            turn.error = f"The model could not write the answer: {exc}"
            turn.answer = (f"The query ran but the answer could not be written. "
                           f"{result.error or ''}").strip()
            return turn

        turn.answer = two.answer
        turn.prompt_tokens += two.usage.prompt_tokens
        turn.completion_tokens += two.usage.completion_tokens
        turn.latency_ms += two.usage.latency_ms
        turn.store_ids.append(self.store.log_message(
            session_id=session_id, role="assistant", content=two.answer,
            prompt_tokens=two.usage.prompt_tokens,
            completion_tokens=two.usage.completion_tokens,
            model=two.usage.model, latency_ms=two.usage.latency_ms))
        return turn


# --------------------------------------------------------------------- web app

app = FastAPI(title="WAGS Insight")
_app: Optional[App] = None


def get_app() -> App:
    global _app
    if _app is None:
        _app = App()
    return _app


@app.get("/health")
def health():
    a = get_app()
    return {
        "ok": True,
        "odoo_db": a.cfg.odoo_db,
        "odoo_uid": a.odoo.uid,
        "branches": len(a.lookups.branches),
        "order_types": len(a.lookups.order_types),
        "return_types": a.lookups.return_type_ids(),
        "date_floor": str(a.cfg.data_start) if a.cfg.has_date_floor else None,
        "today_used": str(a.today_fn()),
        "supabase": a.store.enabled,
        "store_errors": a.store.errors[-3:],
    }


@app.post("/ask")
def ask(payload: dict):
    a = get_app()
    turn = a.ask(payload.get("question", ""),
                 session_id=payload.get("session_id", ""))
    return {
        "status": turn.status,
        "answer": turn.answer,
        "tool_args": turn.tool_args,
        "results": turn.result.data if turn.result and turn.result.ok else None,
        "error": turn.error or (turn.result.error if turn.result else ""),
        "prompt_tokens": turn.prompt_tokens,
        "completion_tokens": turn.completion_tokens,
        "latency_ms": turn.latency_ms,
    }


def build_ui() -> gr.Blocks:
    a = get_app()
    session = uuid.uuid4().hex[:12]

    def respond(message, history):
        prior = []
        for h in (history or [])[-6:]:
            if isinstance(h, dict):
                prior.append({"role": h["role"], "content": h["content"]})
        turn = a.ask(message, history=prior, session_id=session)
        body = turn.answer or turn.error or "No answer."
        if turn.tool_args:
            body += f"\n\n<sub>`{turn.tool_args}` · {turn.prompt_tokens}+" \
                    f"{turn.completion_tokens} tokens · {turn.latency_ms}ms</sub>"
        return body

    floor = str(a.cfg.data_start) if a.cfg.has_date_floor else "none (all history)"
    with gr.Blocks(title="WAGS Insight") as ui:
        gr.Markdown(
            f"### WAGS Insight\n"
            f"Database `{a.cfg.odoo_db}` · timezone {a.cfg.tz_name} · "
            f"date floor {floor} · today treated as {a.today_fn()}")
        # Gradio 6 dropped the `type` argument; message dicts are the default.
        gr.ChatInterface(
            respond,
            examples=["Total sales?",
                      "Sales by month",
                      "Which branch sells most?",
                      "Top selling products",
                      "How much cash did we take?",
                      "TWN branch ki sales kitni hai?"],
        )
    return ui


def serve(host: str = "0.0.0.0", port: int = 7860) -> None:
    """Mount the UI and serve.

    The UI is built here rather than at import time so that importing ``App``
    (golden_run.py does) neither connects to Odoo nor constructs Gradio.
    """
    import uvicorn

    mounted = gr.mount_gradio_app(app, build_ui(), path="/")
    uvicorn.run(mounted, host=host, port=port)


if __name__ == "__main__":
    serve()
