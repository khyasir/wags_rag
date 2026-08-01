"""Environment loading. One place that reads the outside world.

Kept out of ``catalog.py`` (that file is the whitelist) and out of ``rpc.py``
(``llm.py`` and ``store.py`` need settings too and must not import the Odoo
client to get them).

``.env`` is parsed here rather than via python-dotenv because the existing file
uses shell ``export KEY="value"`` lines, which dotenv handles inconsistently.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

ENV_PATH = Path(__file__).resolve().parent / ".env"
_LINE = re.compile(r'^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*?)\s*$')


def load_env(path: Path = ENV_PATH) -> dict:
    """Read .env into os.environ without clobbering real environment vars."""
    values = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if val[:1] in ("'", '"') and val[-1:] == val[:1]:
            val = val[1:-1]
        values[key] = val
        os.environ.setdefault(key, val)
    return values


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _ids(name: str) -> tuple:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return ()
    return tuple(int(p) for p in raw.replace(" ", "").split(",") if p)


def _date(name: str) -> Optional[date]:
    """Blank means no floor. That is a deliberate mode, not a missing value."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%d").date()


@dataclass(frozen=True)
class Settings:
    odoo_url: str
    odoo_db: str
    odoo_user: str
    odoo_key: str
    groq_api_key: str
    groq_model: str
    supabase_url: str
    supabase_key: str
    tz_name: str
    data_start: Optional[date]
    branch_ids: tuple
    daily_query_limit: int
    max_buckets: int
    rpc_timeout: int

    @property
    def has_date_floor(self) -> bool:
        return self.data_start is not None


def settings() -> Settings:
    load_env()
    return Settings(
        odoo_url=os.environ.get("ODOO_URL", "http://localhost:8069"),
        odoo_db=os.environ.get("ODOO_DB", ""),
        odoo_user=os.environ.get("ODOO_USER", ""),
        # API key in production, password locally.
        odoo_key=os.environ.get("ODOO_KEY") or os.environ.get("ODOO_PASSWORD", ""),
        groq_api_key=os.environ.get("GROQ_API_KEY", ""),
        groq_model=os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
        supabase_url=os.environ.get("SUPABASE_URL", ""),
        supabase_key=os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        tz_name=os.environ.get("TZ_NAME", "Asia/Riyadh"),
        data_start=_date("DATA_START"),
        branch_ids=_ids("BRANCH_IDS"),
        daily_query_limit=_int("DAILY_QUERY_LIMIT", 10),
        max_buckets=_int("MAX_BUCKETS", 200),
        rpc_timeout=_int("RPC_TIMEOUT", 30),
    )
