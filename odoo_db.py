"""RETRIEVE — everything that talks to Odoo, and how a request becomes a query.

The app is "constrained dynamic": the LLM does not write a query. It fills in a
small form (a spec) and this file turns that spec into a real Odoo call — but
only using the fields listed in ``fields.py`` and only in their allowed role
(dimension = filter/group, measure = sum). Anything outside that is rejected.

Parts, in order:
1. config + dates (with the DATA_START floor).
2. ``build_query`` — pure. spec in, the exact Odoo call out. No network.
3. ``summarise`` — raw Odoo buckets folded into the figures shown to the user.
4. ``Odoo`` — the XML-RPC client that runs it (JSON-RPC only for the NULL fault).

Transport is XML-RPC, with one exception. Odoo 16 hardcodes ``allow_none=False``
when marshalling XML-RPC replies, so a ``read_group`` SUM over a column that is
NULL in every matched row makes the server fail to encode its own answer
(``cannot marshal None``). coupon_amount and voucher_amount hit this. When that
specific fault appears the same call is retried over ``/jsonrpc``.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import xmlrpc.client
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from fields import (DIMENSION, LINES, MEASURE, MODELS, ORDERS, PAYMENTS,
                    SPECIAL_FILTERS, always_forced, date_field)

# --------------------------------------------------------------------- config

def _env(path=".env") -> None:
    """Read .env. The file uses shell `export KEY="value"` lines."""
    if not os.path.exists(path):
        return
    for raw in open(path, encoding="utf-8"):
        m = re.match(r'^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*?)\s*$', raw)
        if not m or raw.lstrip().startswith("#"):
            continue
        val = m.group(2)
        if val[:1] in "'\"" and val[-1:] == val[:1]:
            val = val[1:-1]
        os.environ.setdefault(m.group(1), val)


_env()

ODOO_URL = os.environ.get("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.environ.get("ODOO_DB", "")
ODOO_USER = os.environ.get("ODOO_USER", "")
ODOO_KEY = os.environ.get("ODOO_KEY") or os.environ.get("ODOO_PASSWORD", "")
TZ_NAME = os.environ.get("TZ_NAME", "Asia/Riyadh")
RPC_TIMEOUT = int(os.environ.get("RPC_TIMEOUT") or 30)

#: Print every build_query step to the terminal. On by default so you can watch
#: how the spec turns into a real Odoo call. Set SHOW_QUERY=0 to silence.
SHOW_QUERY = os.environ.get("SHOW_QUERY", "1").lower() not in ("0", "", "false", "no")

#: The date floor. Blank (dev) = no floor, the whole history is queryable.
#: Production ships DATA_START=2026-01-01, so nothing before 2026 is answered.
def _floor() -> Optional[date]:
    s = os.environ.get("DATA_START", "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


DATA_START = _floor()

#: "Today" for relative periods. Override with TODAY=YYYY-MM-DD. Defaults to the
#: real clock; on the local 2025 sample DB set TODAY in .env to test relatives.
TODAY = (datetime.strptime(os.environ["TODAY"], "%Y-%m-%d").date()
         if os.environ.get("TODAY") else date.today())

UTC = ZoneInfo("UTC")
DT_FMT = "%Y-%m-%d %H:%M:%S"

DATE_GRAINS = ("day", "week", "month")

PERIODS = ("today", "yesterday", "this_week", "last_week", "this_month",
           "last_month", "last_7_days", "last_30_days", "this_year",
           "last_year", "all_time", "custom")

#: What each model totals when the spec names no measures.
DEFAULT_MEASURES = {
    ORDERS: ("amount_untaxed", "amount_tax", "amount_total",
             "discount_amount", "coupon_amount", "voucher_amount"),
    LINES: ("quantity", "price_subtotal", "price_total"),
    PAYMENTS: ("amount",),
}

#: The keys the spec may contain. Anything else is rejected — this is what
#: stops a raw ``domain`` or ``model`` string being smuggled past the whitelist.
ALLOWED_SPEC = {"model", "measures", "group_by", "filters", "period",
                "start_date", "end_date", "line_type", "unposted", "limit"}


class QueryError(ValueError):
    """Rejected request. The message is safe to show the user."""


def _truthy(v) -> bool:
    """The LLM sometimes sends booleans as the strings 'true'/'false'."""
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


# ----------------------------------------------------------------- the dates

def _to_utc(d: date) -> str:
    """Local midnight as the naive UTC string Odoo stores."""
    return (datetime.combine(d, time.min, tzinfo=ZoneInfo(TZ_NAME))
            .astimezone(UTC).replace(tzinfo=None).strftime(DT_FMT))


def date_range(period: str, start: str = "", end: str = "",
               today: Optional[date] = None) -> tuple:
    """(utc_from, utc_to, human_label). Bounds are half-open, floored by DATA_START."""
    today = today or TODAY
    tom = today + timedelta(days=1)
    mon = today - timedelta(days=today.weekday())      # week starts Monday
    a = b = None

    if period == "today":            a, b = today, tom
    elif period == "yesterday":      a, b = today - timedelta(days=1), today
    elif period == "this_week":      a, b = mon, tom
    elif period == "last_week":      a, b = mon - timedelta(days=7), mon
    elif period == "this_month":     a, b = today.replace(day=1), tom
    elif period == "last_month":
        first = today.replace(day=1)
        a, b = (first - timedelta(days=1)).replace(day=1), first
    elif period == "last_7_days":    a, b = today - timedelta(days=6), tom
    elif period == "last_30_days":   a, b = today - timedelta(days=29), tom
    elif period == "this_year":      a, b = date(today.year, 1, 1), tom
    elif period == "last_year":
        a, b = date(today.year - 1, 1, 1), date(today.year, 1, 1)
    elif period == "all_time":       a, b = None, None
    elif period == "custom":
        if not start and not end:
            raise QueryError("A custom range needs a start or an end date.")
        try:
            a = datetime.strptime(start, "%Y-%m-%d").date() if start else None
            b = (datetime.strptime(end, "%Y-%m-%d").date() + timedelta(days=1)
                 ) if end else None
        except ValueError:
            raise QueryError("Dates must look like 2026-01-01.")
        if a and b and b <= a:
            raise QueryError("The end date is before the start date.")
    else:
        raise QueryError(f"Unknown period '{period}'. "
                         f"Allowed: {', '.join(PERIODS)}.")

    # Apply the DATA_START floor.
    if DATA_START:
        if b is not None and b <= DATA_START:
            raise QueryError(
                f"I can only look at data from {DATA_START} onward.")
        if a is None or a < DATA_START:
            a = DATA_START

    if a is None:
        label = "all available data"
    elif b is None:
        label = f"{a} onward"
    elif b - a > timedelta(days=1):
        label = f"{a} to {b - timedelta(days=1)}"
    else:
        label = str(a)
    return (_to_utc(a) if a else None), (_to_utc(b) if b else None), label


# --------------------------------------------------------------- build_query

@dataclass
class Query:
    """The exact call that will be sent. This is what the UI displays."""
    model: str
    domain: list
    fields: list
    groupby: list
    context: dict
    period_label: str
    measures: list = field(default_factory=list)
    user_groups: list = field(default_factory=list)  # groupby the user asked for
    is_orders: bool = False
    limit: int = 0

    def as_sql(self) -> str:
        """Readable equivalent of what Odoo will run. For display only."""
        cols = ", ".join(f"SUM({f.split(':')[0]})" for f in self.fields)
        grp = ", ".join(self.groupby)
        sql = (f"SELECT {grp + ', ' if grp else ''}{cols}, COUNT(*)\n"
               f"FROM   {self.model.replace('.', '_')}\n"
               f"WHERE  {domain_to_sql(self.domain)}")
        return sql + (f"\nGROUP BY {grp}" if grp else "")


def domain_to_sql(domain: list) -> str:
    """Odoo's prefix-notation domain as an infix WHERE clause (display only)."""
    def term(t) -> str:
        f, op, val = t
        op = {"=": "=", "!=": "<>"}.get(op, op)
        if val is False and op == "=":
            return f"{f} IS NULL"
        if isinstance(val, (list, tuple)):
            return f"{f} {op.upper()} ({', '.join(map(repr, val))})"
        return f"{f} {op} {val!r}"

    def parse(items, i):
        tok = items[i]
        if tok in ("|", "&"):
            left, i = parse(items, i + 1)
            right, i = parse(items, i)
            joiner = "OR" if tok == "|" else "AND"
            return f"({left} {joiner} {right})", i
        if tok == "!":
            inner, i = parse(items, i + 1)
            return f"NOT {inner}", i
        return term(tok), i + 1

    parts, i = [], 0
    while i < len(domain):
        frag, i = parse(domain, i)
        parts.append(frag)
    return " AND ".join(parts)


def _resolve_filter(model: str, name: str, value, lookups: "Lookups") -> tuple:
    """A dimension filter -> an Odoo domain term. Names are matched to ids where
    a lookup table exists, otherwise matched loosely on the related name."""
    val = value if isinstance(value, str) else str(value)
    low = val.lower()

    if name == "branch_id":
        ids = [i for i, n in lookups.branches if low in n.lower()]
        return ("branch_id", "in", ids) if ids else ("branch_id.name", "ilike", val)
    if name in ("order_type", "order_type_id"):
        ids = [i for i, n, _ in lookups.order_types if low in n.lower()]
        return (name, "in", ids) if ids else (f"{name}.name", "ilike", val)
    if name == "payment_method_id":
        ids = [i for i, n, _ in lookups.payment_methods if low in n.lower()]
        return (name, "in", ids) if ids else (f"{name}.name", "ilike", val)

    f = MODELS[model].get(name)
    rel = f.relation if f else None
    if rel and rel not in ("char", "boolean", "selection", "datetime", "date"):
        return (f"{name}.name", "ilike", val)      # any other Many2one, by name
    if rel == "boolean":
        return (name, "=", low not in ("false", "0", "no", "none", ""))
    return (name, "ilike", val)                     # char / selection


def build_query(spec: dict, lookups: "Lookups",
                today: Optional[date] = None) -> Query:
    """A spec -> the exact Odoo call. Pure: no network."""
    if not isinstance(spec, dict):
        raise QueryError("The query spec must be an object.")

    unknown = set(spec) - ALLOWED_SPEC
    if unknown:
        raise QueryError(f"Unknown field(s): {', '.join(sorted(unknown))}. "
                         f"Allowed: {', '.join(sorted(ALLOWED_SPEC))}.")

    model = spec.get("model")
    if model not in MODELS:
        raise QueryError(f"Unknown model '{model}'. "
                         f"Allowed: {', '.join(MODELS)}.")
    M = MODELS[model]
    is_orders = model == ORDERS

    # -- measures (what to total) ----------------------------------------
    req = spec.get("measures") or []
    if isinstance(req, str):
        req = [req]
    for name in req:
        f = M.get(name)
        if not f or f.role != MEASURE:
            raise QueryError(
                f"'{name}' is not a number you can total on {model}. "
                f"Allowed: {', '.join(x.name for x in M.measures)}.")
    # Orders always fetches the full sales set so returns can be folded out.
    fetch = list(DEFAULT_MEASURES[ORDERS]) if is_orders else (
        req or list(DEFAULT_MEASURES[model]))

    # -- dates -----------------------------------------------------------
    utc_from, utc_to, label = date_range(
        spec.get("period") or "all_time", spec.get("start_date") or "",
        spec.get("end_date") or "", today)

    domain = list(always_forced(model))            # state = validate
    dfield = date_field(model)
    if utc_from:
        domain.append((dfield, ">=", utc_from))
    if utc_to:
        domain.append((dfield, "<", utc_to))

    # -- special forced filters -----------------------------------------
    special_used = []
    if model == LINES:
        lt = (spec.get("line_type") or "product").lower()
        key = "modifiers" if lt.startswith("mod") else "products"
        domain += SPECIAL_FILTERS[key]
        special_used.append(key)
    if is_orders and _truthy(spec.get("unposted")):
        domain += SPECIAL_FILTERS["unposted_orders"]
        special_used.append("unposted_orders")

    # -- user filters ----------------------------------------------------
    for filt in (spec.get("filters") or []):
        if not isinstance(filt, dict) or "field" not in filt:
            raise QueryError("Each filter needs a field and a value.")
        name = filt["field"]
        f = M.get(name)
        if not f or f.role != DIMENSION:
            raise QueryError(
                f"Cannot filter by '{name}' on {model}. "
                f"Allowed: {', '.join(x.name for x in M.dimensions)}.")
        domain.append(_resolve_filter(model, name, filt.get("value"), lookups))

    # -- group by --------------------------------------------------------
    keys = spec.get("group_by") or []
    if isinstance(keys, str):
        keys = [keys]
    keys = list(dict.fromkeys(keys))
    if len(keys) > 2:
        raise QueryError(f"At most 2 groupings. You asked for {len(keys)}.")
    if len([k for k in keys if k in DATE_GRAINS]) > 1:
        raise QueryError("Only one time grouping at a time.")

    groupby, user_groups = [], []
    for k in keys:
        if k in DATE_GRAINS:
            if model == LINES:
                raise QueryError(
                    f"Order lines carry no date of their own, so they cannot be "
                    f"grouped by {k}.")
            token = f"{dfield}:{k}"
            groupby.append(token)
            user_groups.append(token)
        else:
            f = M.get(k)
            if not f or f.role != DIMENSION:
                raise QueryError(
                    f"Cannot group by '{k}' on {model}. "
                    f"Allowed: {', '.join(x.name for x in M.dimensions)}.")
            groupby.append(k)
            user_groups.append(k)

    # Orders always splits by order type internally so returns are separable.
    if is_orders and "order_type" not in groupby:
        groupby.append("order_type")

    q = Query(model=model, domain=domain,
              fields=[f"{a}:sum" for a in fetch], groupby=groupby,
              context={"tz": TZ_NAME}, period_label=label, measures=fetch,
              user_groups=user_groups, is_orders=is_orders,
              limit=int(spec.get("limit") or 0))

    if SHOW_QUERY:
        _show_build(spec, q, utc_from, utc_to, special_used)
    return q


def _show_build(spec, q: Query, utc_from, utc_to, special_used) -> None:
    """Print, step by step, how the spec turned into a query."""
    C, B, D, R = "\033[36m", "\033[1m", "\033[2m", "\033[0m"
    line = "─" * 70
    print(f"\n{C}{line}\n build_query — how the query is created\n{line}{R}")

    print(f"{B}1. SPEC — what the LLM chose{R}")
    print(f"   {json.dumps(spec)}")

    print(f"{B}2. model{R}")
    print(f"   {q.model}")
    print(f"   {D}forced (always on): state = validate"
          + (f"; special: {', '.join(special_used)}" if special_used else "")
          + f"{R}")

    print(f"{B}3. period → dates{R}")
    print(f"   {spec.get('period') or 'all_time'}  →  {q.period_label}")
    print(f"   {D}from {utc_from or '—'}  to {utc_to or '—'}  (UTC, half-open){R}")

    print(f"{B}4. measures → totals{R}")
    print(f"   {', '.join(q.measures)}")

    print(f"{B}5. group by{R}")
    print(f"   {', '.join(q.groupby) if q.groupby else '(none)'}"
          + (f"   {D}(order_type added to split returns){R}"
             if q.is_orders and 'order_type' not in q.user_groups else ""))

    print(f"{B}6. FINAL query object{R}")
    print(f"   model    {q.model}")
    print(f"   domain   " + "\n            ".join(str(t) for t in q.domain))
    print(f"   fields   {q.fields}")
    print(f"   groupby  {q.groupby}")

    print(f"{B}7. equivalent SQL{R}")
    for l in q.as_sql().splitlines():
        print(f"   {l}")
    print(f"{C}{line}{R}\n")


# ------------------------------------------------------------------- lookups

@dataclass(frozen=True)
class Lookups:
    branches: tuple = ()
    order_types: tuple = ()      # (id, name, is_return)
    payment_methods: tuple = ()

    def return_type_ids(self) -> list:
        return [i for i, _, is_ret in self.order_types if is_ret]

    def is_return(self, type_id) -> bool:
        # An order with no type at all counts as non-return: it carries no flag
        # to read, and dropping it would make gross stop matching the buckets.
        return bool(type_id) and type_id in set(self.return_type_ids())

    def names(self, which: str, limit: int = 12) -> str:
        rows = getattr(self, which)
        seen, out = set(), []
        for r in rows:
            n = r[1]
            if n in seen:
                continue
            seen.add(n)
            out.append(str(n))
            if len(out) >= limit:
                break
        return ", ".join(out)


# -------------------------------------------------------------------- client

class OdooError(RuntimeError):
    """Odoo call failed. Never swallowed — a failure must not become a number."""


class _Transport(xmlrpc.client.Transport):
    def make_connection(self, host):
        c = super().make_connection(host)
        c.timeout = RPC_TIMEOUT
        return c


class Odoo:
    def __init__(self):
        self.uid = None
        self._obj = None
        self.last_transport = ""

    def connect(self) -> "Odoo":
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common",
                                           transport=_Transport())
        try:
            self.uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_KEY, {})
        except Exception as exc:                        # noqa: BLE001
            raise OdooError(f"Could not reach Odoo at {ODOO_URL}: {exc}") from exc
        if not self.uid:
            raise OdooError(f"Odoo rejected the login for '{ODOO_USER}' "
                            f"on database '{ODOO_DB}'.")
        self._obj = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object",
                                              transport=_Transport())
        return self

    def call(self, model: str, method: str, args: list, kw: Optional[dict] = None):
        """XML-RPC, falling back to JSON-RPC only for the NULL-marshal fault."""
        if self.uid is None:
            self.connect()
        try:
            self.last_transport = "XML-RPC"
            return self._obj.execute_kw(ODOO_DB, self.uid, ODOO_KEY,
                                        model, method, args, kw or {})
        except xmlrpc.client.Fault as fault:
            if "cannot marshal None" not in str(fault.faultString):
                raise OdooError(_tidy(fault)) from fault
            self.last_transport = "XML-RPC failed on a NULL total, retried over JSON-RPC"
            return self._json(model, method, args, kw or {})
        except Exception as exc:                        # noqa: BLE001
            raise OdooError(f"Odoo call {model}.{method} failed: {exc}") from exc

    def _json(self, model, method, args, kw):
        payload = json.dumps({
            "jsonrpc": "2.0", "method": "call",
            "params": {"service": "object", "method": "execute_kw",
                       "args": [ODOO_DB, self.uid, ODOO_KEY, model, method,
                                args, kw]},
            "id": 1}).encode()
        req = urllib.request.Request(f"{ODOO_URL}/jsonrpc", data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            body = json.loads(urllib.request.urlopen(req,
                                                     timeout=RPC_TIMEOUT).read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise OdooError(f"Odoo call {model}.{method} failed: {exc}") from exc
        if "error" in body:
            raise OdooError(f"Odoo error: {body['error'].get('message')}")
        return body.get("result")

    # ------------------------------------------------------------------ reads

    def load_lookups(self) -> Lookups:
        """Small reference tables, read once.

        ``active_test=False`` is essential: the only order type flagged
        ``is_return`` is archived, and Odoo hides archived records by default.
        """
        ctx = {"context": {"active_test": False}}
        br = self.call("branch.wags", "search_read", [[], ["name"]], ctx)
        ot = self.call("order.type.wags", "search_read",
                       [[], ["name", "is_return"]], ctx)
        pm = self.call("pos.payment.method.wags", "search_read",
                       [[], ["name", "is_cash"]], ctx)
        return Lookups(
            branches=tuple((r["id"], str(r["name"])) for r in br),
            order_types=tuple((r["id"], str(r["name"]), bool(r.get("is_return")))
                              for r in ot),
            payment_methods=tuple((r["id"], str(r["name"]),
                                   bool(r.get("is_cash"))) for r in pm),
        )

    def run(self, q: Query) -> tuple:
        """Execute the query. Returns (raw_rows, milliseconds)."""
        import time as _t
        started = _t.time()
        rows = self.call(q.model, "read_group",
                         [[list(t) if isinstance(t, tuple) else t
                           for t in q.domain], q.fields, q.groupby],
                         {"lazy": False, "context": q.context})
        return rows, (_t.time() - started) * 1000

    def find_order(self, ref: str) -> Optional[dict]:
        """One order by reference or transaction id, with its product lines."""
        ref = (ref or "").strip()
        if not ref:
            return None
        head_fields = ["reference", "transaction_id", "branch_id", "order_type",
                       "order_datetime", "state", "amount_untaxed", "amount_tax",
                       "amount_total", "discount_amount", "coupon_amount",
                       "voucher_amount"]
        recs = self.call(ORDERS, "search_read",
                         [["|", ("reference", "=", ref),
                           ("transaction_id", "=", ref)], head_fields],
                         {"limit": 1, "context": {"tz": TZ_NAME}})
        if not recs:
            return None
        order = recs[0]
        lines = self.call(LINES, "search_read",
                          [[("pos_id", "=", order["id"])],
                           ["product_id", "quantity", "price_unit",
                            "price_subtotal", "price_total", "app_line_id"]],
                          {"context": {"tz": TZ_NAME}})
        return {"order": order, "lines": lines}


def _tidy(fault: xmlrpc.client.Fault) -> str:
    lines = [l for l in str(fault.faultString).strip().splitlines() if l.strip()]
    return f"Odoo error: {lines[-1].strip()}" if lines else "Odoo error."


# ----------------------------------------------------------------- summarise

def _label(v):
    if isinstance(v, (list, tuple)) and len(v) > 1:
        return v[1]
    return v if v not in (False, None) else "Not set"


def summarise(rows: list, q: Query, lookups: Lookups) -> dict:
    """Raw Odoo buckets -> the figures shown to the user.

    Orders fold the internal order-type dimension into net, gross and returns.
    Returns are separate records holding positive amounts, so net = gross minus
    returns. Everything else is a plain sum of the requested measures.
    """
    if q.is_orders:
        buckets: dict = {}
        for r in rows:
            key = " / ".join(str(_label(r.get(f))) for f in q.user_groups) or "overall"
            buckets.setdefault(key, []).append(r)
        return {k: _fold(v, "order_type", lookups) for k, v in buckets.items()}

    aggs = q.measures
    if not q.user_groups:
        out = {a: round(sum(r.get(a) or 0.0 for r in rows), 2) for a in aggs}
        out["records"] = sum(r.get("__count") or 0 for r in rows)
        return {"overall": out}

    buckets = {}
    for r in rows:
        key = " / ".join(str(_label(r.get(f))) for f in q.user_groups)
        buckets.setdefault(key, []).append(r)
    return {k: {**{a: round(sum(r.get(a) or 0.0 for r in v), 2) for a in aggs},
                "records": sum(r.get("__count") or 0 for r in v)}
            for k, v in buckets.items()}


def _fold(rows, type_field, lookups) -> dict:
    gross = ret = tax = total = disc = coup = vouch = 0.0
    n = nret = 0
    for r in rows:
        u = r.get("amount_untaxed") or 0.0
        c = r.get("__count") or 0
        tid = r.get(type_field)
        tid = tid[0] if isinstance(tid, (list, tuple)) and tid else tid
        if lookups.is_return(tid):
            ret += u
            nret += c
        else:
            gross += u
            n += c
            tax += r.get("amount_tax") or 0.0
            total += r.get("amount_total") or 0.0
            disc += r.get("discount_amount") or 0.0
            coup += r.get("coupon_amount") or 0.0
            vouch += r.get("voucher_amount") or 0.0
    return {
        "net_sales": round(gross - ret, 2),
        "gross_sales": round(gross, 2),
        "returns": round(ret, 2),
        "vat": round(tax, 2),
        "total_with_vat": round(total, 2),
        "discounts": round(disc, 2),
        "coupons": round(coup, 2),
        "vouchers": round(vouch, 2),
        "orders": n,
        "return_count": nret,
        "average_ticket": round((gross - ret) / n, 2) if n else None,
    }
