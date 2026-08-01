"""Supabase writes over the REST API.

Tables are created by hand from ``supabase_schema.sql``, never from here.

Writes are best-effort by design: if Supabase is unreachable, the user still
gets their answer and the failure is recorded on the result rather than raised.
Losing a log line is acceptable; losing an answer that was correctly computed is
not. Failures are counted so they cannot pass unnoticed.

``certifi`` is used explicitly — a clean venv on macOS has no CA bundle and
every HTTPS call fails with CERTIFICATE_VERIFY_FAILED.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import certifi

from config import Settings


@dataclass
class Store:
    cfg: Settings
    #: Write failures, so a broken log is visible instead of silent.
    errors: list = field(default_factory=list)
    _ctx: Optional[ssl.SSLContext] = None

    def __post_init__(self):
        self._ctx = ssl.create_default_context(cafile=certifi.where())

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.supabase_url and self.cfg.supabase_key)

    # ----------------------------------------------------------------- transport

    def _request(self, path: str, method: str = "GET",
                 body: Optional[object] = None,
                 prefer: str = "return=representation"):
        url = f"{self.cfg.supabase_url.rstrip('/')}{path}"
        req = urllib.request.Request(
            url, method=method,
            data=json.dumps(body, default=str).encode() if body is not None else None,
            headers={
                "apikey": self.cfg.supabase_key,
                "Authorization": f"Bearer {self.cfg.supabase_key}",
                "Content-Type": "application/json",
                "Prefer": prefer,
            })
        with urllib.request.urlopen(req, timeout=20, context=self._ctx) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    # -------------------------------------------------------------------- writes

    def log_message(self, *, session_id: str, role: str,
                    content: Optional[str] = None,
                    tool_args: Optional[dict] = None,
                    tool_result: Optional[dict] = None,
                    prompt_tokens: Optional[int] = None,
                    completion_tokens: Optional[int] = None,
                    model: Optional[str] = None,
                    latency_ms: Optional[int] = None) -> Optional[int]:
        """Insert one row. Returns its id, or None if the write failed.

        ``cost_usd`` is deliberately left NULL. Token counts are exact, so the
        moment a per-million rate is confirmed every row can be backfilled. A
        guessed rate would look authoritative and be wrong.
        """
        if not self.enabled:
            return None

        row = {
            "session_id": session_id,
            "role": role,
            "content": content,
            "tool_args": tool_args,
            "tool_result": tool_result,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "model": model,
            "latency_ms": latency_ms,
        }
        try:
            out = self._request("/chat_messages", "POST", [row])
            return out[0]["id"] if out else None
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                ValueError) as exc:
            detail = exc.read().decode()[:200] if hasattr(exc, "read") else str(exc)
            self.errors.append(f"{role}: {detail}")
            return None

    # -------------------------------------------------------------------- reads

    def get_settings(self) -> dict:
        """Runtime knobs from the settings table. Empty dict if unreachable."""
        if not self.enabled:
            return {}
        try:
            rows = self._request("/settings?select=key,value") or []
            return {r["key"]: r["value"] for r in rows}
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                ValueError) as exc:
            self.errors.append(f"settings read: {exc}")
            return {}

    def count_today(self, session_id: str) -> int:
        """Rows logged for this session today, for the daily cap."""
        if not self.enabled:
            return 0
        try:
            rows = self._request(
                f"/chat_messages?select=id&role=eq.tool"
                f"&session_id=eq.{urllib.parse.quote(session_id)}"
                f"&created_at=gte.{date.today().isoformat()}") or []
            return len(rows)
        except Exception as exc:                     # noqa: BLE001 - never fatal
            self.errors.append(f"count read: {exc}")
            return 0
