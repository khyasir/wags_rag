"""The two Groq calls, and the tool schema they are constrained by.

Call one reads the question and either asks one short clarifying question or
returns arguments for ``query_pos``. Call two turns the tool result into a
sentence in the language the question was asked in.

The tool schema is **generated from catalog.py**, so the model can only ever be
offered keys the query layer actually accepts. Adding a metric to the catalog
offers it to the model automatically; nothing is hand-listed here.

Uses the official ``groq`` SDK. Plain HTTP to the API is blocked by Cloudflare
(403, error 1010), which cost time to discover, so do not "simplify" this to
urllib.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from groq import Groq

import catalog
import dates
from catalog import Lookups
from config import Settings

TOOL_NAME = "query_pos"

#: Groq's free tier allows 100,000 tokens per DAY. Once that is gone, waiting
#: does not help, so a per-day limit must be distinguished from a per-minute one.
_RETRY_WAITS = (2, 6, 15)


class RateLimited(RuntimeError):
    """Groq refused on quota. ``per_day`` means waiting will not help today."""

    def __init__(self, message: str, per_day: bool, retry_after: float = 0.0):
        super().__init__(message)
        self.per_day = per_day
        self.retry_after = retry_after


def _parse_rate_limit(text: str) -> tuple[bool, float]:
    """(is_per_day, seconds_to_wait) from Groq's 429 body."""
    per_day = "per day" in text.lower() or "(tpd)" in text.lower()
    wait = 0.0
    marker = "try again in "
    if marker in text:
        tail = text.split(marker, 1)[1]
        num = ""
        for ch in tail:
            if ch.isdigit() or ch == ".":
                num += ch
            elif ch == "m" and num:
                wait += float(num) * 60
                num = ""
            elif ch == "s" and num:
                wait += float(num)
                break
            elif num:
                break
        else:
            if num:
                wait += float(num)
    return per_day, wait


@dataclass
class Usage:
    """Exact counts from Groq. Never estimated — the provider is authoritative."""

    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0

    @classmethod
    def of(cls, response, latency_ms: float) -> "Usage":
        u = getattr(response, "usage", None)
        return cls(
            model=getattr(response, "model", "") or "",
            prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(u, "completion_tokens", 0) or 0,
            latency_ms=int(latency_ms),
        )


@dataclass
class CallOne:
    """Either a clarifying question, or tool arguments. Never both."""

    tool_args: Optional[dict] = None
    clarification: str = ""
    usage: Usage = field(default_factory=Usage)
    raw_text: str = ""

    @property
    def wants_query(self) -> bool:
        return self.tool_args is not None


@dataclass
class CallTwo:
    answer: str = ""
    usage: Usage = field(default_factory=Usage)


def build_tool_schema() -> dict:
    """Generated from the catalog. The model cannot name a field, only a key."""
    metrics = list(catalog.METRICS)
    group_keys = sorted({k for m in metrics
                         for k in catalog.allowed_group_by(m)})
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Query POS sales figures. You choose a metric and parameters; "
                "the server builds and runs the query. Only validated orders "
                "are counted."),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "enum": metrics,
                        "description": "Which figure to fetch.",
                    },
                    "period": {
                        "type": "string",
                        "enum": list(dates.NAMED_PERIODS),
                        "description": "Time range. Use custom with dates for "
                                       "anything else.",
                    },
                    "start_date": {"type": "string",
                                   "description": "YYYY-MM-DD, custom only."},
                    "end_date": {"type": "string",
                                 "description": "YYYY-MM-DD, custom only, "
                                                "inclusive."},
                    "group_by": {
                        "type": "array",
                        "items": {"type": "string", "enum": group_keys},
                        "description": "Up to 3 groupings, at most one of them "
                                       "a time grouping.",
                    },
                    "filters": {
                        "type": "object",
                        "properties": {
                            "branch_ids": {"type": "array",
                                           "items": {"type": "integer"}},
                            "branch_keyword": {"type": "string"},
                            "product_keyword": {"type": "string"},
                            "payment_method_keyword": {"type": "string"},
                            "order_type_ids": {"type": "array",
                                               "items": {"type": "integer"}},
                            "exclude_returns": {"type": "boolean"},
                            "is_cash": {"type": "boolean"},
                        },
                        "additionalProperties": False,
                    },
                },
                "required": ["metric"],
                "additionalProperties": False,
            },
        },
    }


class Llm:
    def __init__(self, cfg: Settings, lookups: Lookups, today_fn=None):
        self.cfg = cfg
        self.lookups = lookups
        self.client = Groq(api_key=cfg.groq_api_key)
        self._today_fn = today_fn or (lambda: date.today())
        self.tool = build_tool_schema()

    # ------------------------------------------------------------------ prompt

    # ------------------------------------------------------------------- calls

    def _create(self, **kwargs):
        """One Groq call, retrying transient rate limits with backoff.

        A per-minute limit is worth waiting out. A per-day limit is not — it
        raises immediately so the caller reports the truth instead of stalling.
        """
        last = None
        for attempt, wait in enumerate((*_RETRY_WAITS, None)):
            try:
                return self.client.chat.completions.create(
                    model=self.cfg.groq_model, **kwargs)
            except Exception as exc:                    # noqa: BLE001
                text = str(exc)
                if "429" not in text and "rate_limit" not in text:
                    raise
                per_day, suggested = _parse_rate_limit(text)
                if per_day:
                    raise RateLimited(
                        "Groq's daily token allowance for this account is used "
                        "up. It resets on a rolling 24-hour window; the app "
                        "will work again after that, or on a paid tier.",
                        per_day=True, retry_after=suggested) from exc
                last = exc
                if wait is None:
                    break
                time.sleep(min(suggested, 30) or wait)
        raise RateLimited(f"Groq rate limit did not clear: {last}",
                          per_day=False)

    def system_prompt(self) -> str:
        return catalog.build_system_prompt(
            today=self._today_fn(),
            tz_name=self.cfg.tz_name,
            data_start=self.cfg.data_start,
            lookups=self.lookups,
            max_buckets=self.cfg.max_buckets,
        )

    # ---------------------------------------------------------------- call one

    def choose_query(self, question: str, history: Optional[list] = None) -> CallOne:
        messages = [{"role": "system", "content": self.system_prompt()}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": question})

        started = time.time()
        # Groq counts requested max_tokens toward the daily allowance, not just
        # what is generated. Call one emits a small tool call, so a large
        # reservation here buys nothing and blocks the request sooner.
        resp = self._create(messages=messages, tools=[self.tool],
                            tool_choice="auto", temperature=0, max_tokens=200)
        usage = Usage.of(resp, (time.time() - started) * 1000)
        msg = resp.choices[0].message
        calls = getattr(msg, "tool_calls", None) or []

        if calls:
            raw = calls[0].function.arguments or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                return CallOne(
                    clarification="I could not read that request. Could you "
                                  "rephrase it?",
                    usage=usage, raw_text=raw)
            # Strip keys the model left empty; the builder rejects unknowns and
            # an empty string is not a valid date or keyword.
            args = {k: v for k, v in args.items()
                    if v not in (None, "", [], {})}
            return CallOne(tool_args=args, usage=usage, raw_text=raw)

        return CallOne(clarification=(msg.content or "").strip(), usage=usage,
                       raw_text=msg.content or "")

    # ---------------------------------------------------------------- call two

    def answer_prompt(self) -> str:
        return catalog.build_answer_prompt(tz_name=self.cfg.tz_name,
                                           lookups=self.lookups)

    def write_answer(self, question: str, tool_result: dict,
                     tool_args: Optional[dict] = None) -> CallTwo:
        payload = json.dumps(tool_result, ensure_ascii=False, default=str)
        messages = [
            {"role": "system", "content": self.answer_prompt()},
            {"role": "user",
             "content": (f"Question: {question}\n\n"
                         f"Result:\n{payload}\n\nWrite the answer.")},
        ]

        started = time.time()
        # Answers are a few sentences. See the note on max_tokens above.
        resp = self._create(messages=messages, temperature=0.2, max_tokens=400)
        return CallTwo(answer=(resp.choices[0].message.content or "").strip(),
                       usage=Usage.of(resp, (time.time() - started) * 1000))
