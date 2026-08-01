"""RETRIEVE — everything that talks to Odoo, plus what is allowed to be asked.

Three parts, in order:

1. ``METRICS`` / ``GROUP_BY`` — the whitelist. The LLM sends keys; this is the
   only place a key turns into a field name. A key that is not here is an
   error, never a guess.
2. ``build_query`` — pure. Tool arguments in, the exact Odoo call out. No
   network, so it can be read and tested on its own.
3. ``Odoo`` — the XML-RPC client that runs it.

Transport is XML-RPC, with one exception. Odoo 16 hardcodes ``allow_none=False``
when marshalling XML-RPC replies (``odoo/addons/base/controllers/rpc.py``), so a
``read_group`` SUM over a column that is NULL in every matched row makes the
server fail to encode its own answer:

    TypeError: cannot marshal None unless allow_none is enabled

That is not hypothetical here — ``coupon_amount`` and ``voucher_amount`` are
NULL on all 318 local orders, so the ``discounts`` metric hits it every time.
When that specific fault appears the same call is retried over ``/jsonrpc``,
which carries null natively. Everything else goes over XML-RPC as intended.
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

#: Local data stops 2025-12-11 while the clock says 2026, so relative periods
#: would all come back empty. Override with TODAY=YYYY-MM-DD.
TODAY = (datetime.strptime(os.environ["TODAY"], "%Y-%m-%d").date()
         if os.environ.get("TODAY") else date(2025, 12, 11))

UTC = ZoneInfo("UTC")
DT_FMT = "%Y-%m-%d %H:%M:%S"

ORDERS = "pos.wags"
LINES = "pos.wags.tree"
PAYMENTS = "pos.wags.payment.method"


# ------------------------------------------------------------ THE WHITELIST

@dataclass(frozen=True)
class Metric:
    model: str
    date_field: str          # what a date range filters on
    date_group: Optional[str]  # what a date grouping uses; None = not possible
    aggregates: tuple
    forced: tuple            # always applied, the LLM cannot remove it
    groups: tuple
    about: str
    split_returns: bool = False
    only_returns: bool = False


METRICS: dict[str, Metric] = {
    "sales": Metric(
        ORDERS, "order_datetime", "order_datetime",
        ("amount_untaxed", "amount_total", "amount_tax", "discount_amount"),
        (("state", "=", "validate"),),
        ("day", "week", "month", "branch", "order_type", "employee", "cashbox"),
        "Net, gross and returns. Net is before VAT.",
        split_returns=True),
    "returns": Metric(
        ORDERS, "order_datetime", "order_datetime",
        ("amount_untaxed", "amount_total", "amount_tax"),
        (("state", "=", "validate"),),
        ("day", "week", "month", "branch", "order_type"),
        "Return orders only.",
        only_returns=True),
    "discounts": Metric(
        ORDERS, "order_datetime", "order_datetime",
        ("discount_amount", "coupon_amount", "voucher_amount"),
        (("state", "=", "validate"),),
        ("day", "week", "month", "branch", "order_type"),
        "Discounts, coupons and gift vouchers."),
    "unposted_orders": Metric(
        ORDERS, "order_datetime", "order_datetime",
        ("amount_untaxed", "amount_total"),
        (("state", "=", "validate"), ("session_order_link", "=", False)),
        ("day", "week", "month", "branch"),
        "Validated orders never posted to a session."),
    "payments": Metric(
        PAYMENTS, "order_datetime", "order_datetime",
        ("amount",),
        (("pos_id.state", "=", "validate"),),
        ("day", "week", "month", "payment_method", "cashbox"),
        "Money collected, by payment method. Includes VAT."),
    "products": Metric(
        LINES, "pos_id.order_datetime", None,
        ("quantity", "price_subtotal", "price_total"),
        (("pos_id.state", "=", "validate"),
         "|", ("app_line_id", "<", 100000), ("app_line_id", "=", False)),
        ("product", "pos_category", "branch", "order_type"),
        "Product lines. Quantity is the main figure."),
    "modifiers": Metric(
        LINES, "pos_id.order_datetime", None,
        ("quantity", "price_subtotal", "price_total"),
        (("pos_id.state", "=", "validate"), ("app_line_id", ">=", 100000)),
        ("product", "pos_category", "branch", "order_type"),
        "Modifier lines, such as an extra shot."),
}

DATE_GRAINS = ("day", "week", "month")

#: key -> {model: field}. Missing means that grouping is impossible there.
#: Odoo's read_group cannot group by a related field, so line metrics have no
#: date grouping and payments have no branch. Verified against the database.
GROUP_BY: dict[str, dict] = {
    "branch": {ORDERS: "branch_id", LINES: "branch_id"},
    "order_type": {ORDERS: "order_type", LINES: "order_type_id"},
    "employee": {ORDERS: "employee_id"},
    "cashbox": {ORDERS: "cashbox_id", PAYMENTS: "cashbox_id"},
    "product": {LINES: "product_id"},
    "pos_category": {LINES: "pos_category_id"},
    "payment_method": {PAYMENTS: "payment_method_id"},
}

PERIODS = ("today", "yesterday", "this_week", "last_week", "this_month",
           "last_month", "last_7_days", "last_30_days", "this_year",
           "last_year", "all_time", "custom")


class QueryError(ValueError):
    """Rejected request. The message is safe to show the user."""


# ----------------------------------------------------------------- the dates

def _to_utc(d: date) -> str:
    """Local midnight as the naive UTC string Odoo stores."""
    return (datetime.combine(d, time.min, tzinfo=ZoneInfo(TZ_NAME))
            .astimezone(UTC).replace(tzinfo=None).strftime(DT_FMT))


def date_range(period: str, start: str = "", end: str = "",
               today: Optional[date] = None) -> tuple:
    """(utc_from, utc_to, human_label). Bounds are half-open."""
    today = today or TODAY
    tom = today + timedelta(days=1)
    mon = today - timedelta(days=today.weekday())      # week starts Monday

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
    elif period == "all_time":       return None, None, "all available data"
    elif period == "custom":
        if not start and not end:
            raise QueryError("A custom range needs a start or an end date.")
        try:
            a = datetime.strptime(start, "%Y-%m-%d").date() if start else None
            b = (datetime.strptime(end, "%Y-%m-%d").date() + timedelta(days=1)
                 ) if end else None
        except ValueError:
            raise QueryError("Dates must look like 2025-12-01.")
        if a and b and b <= a:
            raise QueryError("The end date is before the start date.")
    else:
        raise QueryError(f"Unknown period '{period}'. "
                         f"Allowed: {', '.join(PERIODS)}.")

    label = (f"{a} to {b - timedelta(days=1)}" if a and b and b - a > timedelta(days=1)
             else str(a) if a else "all available data")
    return (_to_utc(a) if a else None), (_to_utc(b) if b else None), label


# --------------------------------------------------------------- build_query

@dataclass
class Query:
    """The exact call that will be sent. This is what the UI displays."""
    metric: str
    model: str
    domain: list
    fields: list
    groupby: list
    context: dict
    period_label: str
    user_groups: list = field(default_factory=list)
    split_returns: bool = False

    def as_sql(self) -> str:
        """Readable equivalent of what Odoo will run. For display only."""
        cols = ", ".join(f"SUM({f.split(':')[0]})" for f in self.fields)
        grp = ", ".join(self.groupby)
        sql = (f"SELECT {grp + ', ' if grp else ''}{cols}, COUNT(*)\n"
               f"FROM   {self.model.replace('.', '_')}\n"
               f"WHERE  {domain_to_sql(self.domain)}")
        return sql + (f"\nGROUP BY {grp}" if grp else "")


def domain_to_sql(domain: list) -> str:
    """Odoo's prefix-notation domain as an infix WHERE clause.

    Odoo writes OR as a leading ``'|'`` applying to the next two terms, so
    ``['|', a, b, c]`` means ``(a OR b) AND c``. Rendering the list left to
    right with AND between everything reads as ``a AND b AND c`` — the opposite
    of what runs. The products metric relies on an OR, so this display would
    have quietly misrepresented the actual query.
    """
    def term(t) -> str:
        field, op, val = t
        op = {"=": "=", "!=": "<>"}.get(op, op)
        if val is False and op == "=":
            return f"{field} IS NULL"
        if isinstance(val, (list, tuple)):
            return f"{field} {op.upper()} ({', '.join(map(repr, val))})"
        return f"{field} {op} {val!r}"

    def parse(items, i):
        """Returns (sql_fragment, next_index)."""
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


def build_query(args: dict, lookups: "Lookups",
                today: Optional[date] = None) -> Query:
    """Tool arguments -> the exact Odoo call. Pure: no network."""
    if not isinstance(args, dict):
        raise QueryError("Arguments must be an object.")

    allowed_args = {"metric", "period", "start_date", "end_date", "group_by"}
    unknown = set(args) - allowed_args
    if unknown:
        # This is what stops `domain`, `model` or raw SQL being smuggled in.
        raise QueryError(f"Unknown argument(s): {', '.join(sorted(unknown))}. "
                         f"Allowed: {', '.join(sorted(allowed_args))}.")

    name = args.get("metric")
    if name not in METRICS:
        raise QueryError(f"Unknown metric '{name}'. "
                         f"Allowed: {', '.join(METRICS)}.")
    m = METRICS[name]

    utc_from, utc_to, label = date_range(
        args.get("period") or "all_time", args.get("start_date") or "",
        args.get("end_date") or "", today)

    domain = list(m.forced)                      # state filter, always first
    if m.only_returns:
        ids = lookups.return_type_ids()
        if not ids:
            raise QueryError("No order type is flagged as a return.")
        domain.append(("order_type", "in", ids))
    if utc_from:
        domain.append((m.date_field, ">=", utc_from))
    if utc_to:
        domain.append((m.date_field, "<", utc_to))

    keys = args.get("group_by") or []
    if isinstance(keys, str):
        keys = [keys]
    keys = list(dict.fromkeys(keys))             # dedupe, keep order
    if len(keys) > 2:
        raise QueryError(f"At most 2 groupings. You asked for {len(keys)}.")
    if len([k for k in keys if k in DATE_GRAINS]) > 1:
        raise QueryError("Only one time grouping at a time.")

    groupby = []
    for k in keys:
        if k in DATE_GRAINS:
            if not m.date_group:
                raise QueryError(
                    f"'{name}' cannot be grouped by {k} — order lines carry no "
                    f"date of their own.")
            groupby.append(f"{m.date_group}:{k}")
        elif k in GROUP_BY and m.model in GROUP_BY[k] and k in m.groups:
            groupby.append(GROUP_BY[k][m.model])
        else:
            raise QueryError(f"'{name}' cannot be grouped by '{k}'. "
                             f"Allowed: {', '.join(m.groups)}.")

    # Sales always splits by order type internally, so returns can be separated
    # whether or not the user asked for that breakdown.
    if m.split_returns:
        f = GROUP_BY["order_type"][m.model]
        if f not in groupby:
            groupby.append(f)

    return Query(metric=name, model=m.model, domain=domain,
                 fields=[f"{a}:sum" for a in m.aggregates], groupby=groupby,
                 context={"tz": TZ_NAME}, period_label=label,
                 user_groups=keys, split_returns=m.split_returns)


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
            out.append(f"{r[0]}={n}")
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
            # Odoo cannot encode its own reply. Same call, different endpoint.
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
        Without it the cache reports zero return types and return orders get
        counted into gross sales in silence.
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


def _tidy(fault: xmlrpc.client.Fault) -> str:
    lines = [l for l in str(fault.faultString).strip().splitlines() if l.strip()]
    return f"Odoo error: {lines[-1].strip()}" if lines else "Odoo error."


# ----------------------------------------------------------------- summarise

def _label(v):
    if isinstance(v, (list, tuple)) and len(v) > 1:
        return v[1]
    return v if v not in (False, None) else "Not set"


def summarise(rows: list, q: Query, lookups: Lookups) -> dict:
    """Raw Odoo buckets -> the figures the LLM is allowed to see.

    For sales this folds the internal order-type dimension away into net, gross
    and returns. Returns are separate records holding positive amounts, so net
    is gross minus returns, and net is never reported on its own.
    """
    if not q.split_returns:
        return _plain(rows, q)

    type_field = GROUP_BY["order_type"][q.model]
    group_fields = [_group_field(q, k) for k in q.user_groups
                    if k != "order_type"]

    buckets: dict = {}
    for r in rows:
        key = " / ".join(str(_label(r.get(f))) for f in group_fields) or "overall"
        buckets.setdefault(key, []).append(r)

    return {k: _fold(v, type_field, lookups) for k, v in buckets.items()}


def _fold(rows, type_field, lookups) -> dict:
    gross = ret = tax = total = disc = 0.0
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
    return {
        "net_sales": round(gross - ret, 2),
        "gross_sales": round(gross, 2),
        "returns": round(ret, 2),
        "vat": round(tax, 2),
        "total_with_vat": round(total, 2),
        "discounts": round(disc, 2),
        "orders": n,
        "return_count": nret,
        "average_ticket": round((gross - ret) / n, 2) if n else None,
    }


def _plain(rows: list, q: Query) -> dict:
    aggs = [f.split(":")[0] for f in q.fields]
    if not q.user_groups:
        out = {a: round(sum(r.get(a) or 0.0 for r in rows), 2) for a in aggs}
        out["records"] = sum(r.get("__count") or 0 for r in rows)
        return {"overall": out}

    fields = [_group_field(q, k) for k in q.user_groups]
    buckets: dict = {}
    for r in rows:
        key = " / ".join(str(_label(r.get(f))) for f in fields)
        buckets.setdefault(key, []).append(r)
    return {k: {**{a: round(sum(r.get(a) or 0.0 for r in v), 2) for a in aggs},
                "records": sum(r.get("__count") or 0 for r in v)}
            for k, v in buckets.items()}


def _group_field(q: Query, key: str) -> str:
    m = METRICS[q.metric]
    if key in DATE_GRAINS:
        return f"{m.date_group}:{key}"
    return GROUP_BY[key][q.model]
