"""Builder + RPC + returns arithmetic. The only thing the model's tool call hits.

Responsibilities that belong here rather than in ``builder.py`` because they need
the network or the result set:

* resolving ``product_keyword`` to ids before building
* running the ``read_group``
* enforcing the bucket cap, so raw rows never reach the model
* relabelling date buckets from ``__range`` instead of Odoo's display string
* folding the internal order-type dimension into net / gross / returns
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import builder
import catalog
import dates
from builder import QueryPlan, WhitelistError
from config import Settings
from rpc import Odoo, RpcError


@dataclass
class QueryResult:
    ok: bool
    metric: str = ""
    #: Folded aggregates. Either {"overall": {...}} or one entry per group.
    data: dict = field(default_factory=dict)
    #: Raw grouped buckets, aggregates only. Never raw order rows.
    buckets: list = field(default_factory=list)
    bucket_count: int = 0
    #: Rows Odoo returned before any Python folding. Differs from bucket_count
    #: only for hour_of_day.
    raw_bucket_count: int = 0
    range_description: str = ""
    grouped_by: tuple = ()
    notes: tuple = ()
    error: str = ""
    elapsed_ms: float = 0.0

    def for_model(self) -> dict:
        """Exactly what call two is allowed to see."""
        if not self.ok:
            return {"ok": False, "error": self.error}
        return {
            "ok": True,
            "metric": self.metric,
            "period": self.range_description,
            "grouped_by": list(self.grouped_by),
            "bucket_count": self.bucket_count,
            "results": self.data,
            "notes": list(self.notes),
        }


class QueryRunner:
    def __init__(self, odoo: Odoo, cfg: Settings, lookups: catalog.Lookups,
                 today_fn=None):
        self.odoo = odoo
        self.cfg = cfg
        self.lookups = lookups
        # Injectable so tests are not tied to the real clock.
        self._today_fn = today_fn or (lambda: date.today())

    # ------------------------------------------------------------------- run

    def run(self, args: dict) -> QueryResult:
        started = time.time()
        try:
            plan = self._plan(args)
        except WhitelistError as exc:
            return QueryResult(ok=False, error=str(exc))

        try:
            rows, elapsed = self.odoo.read_group(
                plan.model, plan.domain, plan.fields, plan.groupby,
                context=plan.context)
        except RpcError as exc:
            # Rule 9: say it failed. Never fabricate a figure.
            return QueryResult(ok=False, error=str(exc),
                               elapsed_ms=(time.time() - started) * 1000)

        # hour_of_day collapses to at most 24 rows, so the model-facing cap is
        # checked after folding. The raw fetch gets its own, larger guard.
        if plan.folds_hour_of_day:
            if len(rows) > catalog.MAX_RAW_HOUR_BUCKETS:
                return QueryResult(
                    ok=False,
                    error=("That period has too many hours to work out a "
                           "busiest time of day. Ask for a shorter period, "
                           "around a month or less."),
                    elapsed_ms=elapsed)
        elif len(rows) > self.cfg.max_buckets:
            return QueryResult(
                ok=False,
                error=(f"That would return {len(rows)} groups, more than the "
                       f"{self.cfg.max_buckets} allowed. Ask for a shorter "
                       f"period or fewer groupings."),
                elapsed_ms=elapsed)

        rows = [self._relabel_dates(r, plan) for r in rows]
        data = self._fold(rows, plan)

        if len(data) > self.cfg.max_buckets:
            return QueryResult(
                ok=False,
                error=(f"That would return {len(data)} groups, more than the "
                       f"{self.cfg.max_buckets} allowed. Ask for a shorter "
                       f"period or fewer groupings."),
                elapsed_ms=elapsed)

        return QueryResult(
            ok=True,
            metric=plan.metric,
            data=data,
            buckets=rows,
            # What the model actually sees, not the raw fetch. They differ when
            # hour_of_day folds many hour buckets into at most 24.
            bucket_count=len(data),
            raw_bucket_count=len(rows),
            range_description=plan.date_range.description,
            grouped_by=plan.user_group_by,
            notes=plan.notes,
            elapsed_ms=elapsed,
        )

    # ------------------------------------------------------------------ parts

    def _plan(self, args: dict) -> QueryPlan:
        """Resolve anything needing the network, then build."""
        product_ids = None
        filters = (args or {}).get("filters") or {}
        if isinstance(filters, dict) and filters.get("product_keyword"):
            try:
                product_ids = self.odoo.resolve_product_ids(
                    str(filters["product_keyword"]))
            except RpcError as exc:
                raise WhitelistError(f"Could not look up that product: {exc}")

        return builder.build(
            args,
            today=self._today_fn(),
            tz_name=self.cfg.tz_name,
            data_start=self.cfg.data_start,
            lookups=self.lookups,
            branch_scope=self.cfg.branch_ids,
            resolved_product_ids=product_ids,
        )

    def _relabel_dates(self, row: dict, plan: QueryPlan) -> dict:
        """Replace Odoo's locale display label with an unambiguous local one.

        ``__range.from`` is the UTC start of the bucket, which read_group has
        already computed using the context timezone. Converting that back to a
        local date is exact; parsing "01 Dec 2025" is not.
        """
        rng = row.get("__range") or {}
        out = {k: v for k, v in row.items() if k not in ("__domain", "__range")}
        for spec, bounds in rng.items():
            grain = spec.split(":")[-1]
            local = dates.utc_str_to_local_date(bounds["from"], self.cfg.tz_name)
            if grain == "month":
                out[spec] = local.strftime("%Y-%m")
            elif grain == "hour":
                hour = dates.datetime.strptime(bounds["from"], dates.ODOO_DT_FMT)
                hour = hour.replace(tzinfo=dates.UTC).astimezone(
                    dates.ZoneInfo(self.cfg.tz_name))
                # For hour_of_day, drop the date so every 15:00 across the whole
                # period collapses into one bucket. That is the fold.
                out[spec] = (hour.strftime("%H:00") if plan.folds_hour_of_day
                             else hour.strftime("%Y-%m-%d %H:00"))
            else:
                out[spec] = local.isoformat()
        return out

    def _fold(self, rows: list, plan: QueryPlan) -> dict:
        if plan.returns_split:
            return builder.fold_returns_by_group(
                rows, plan=plan, lookups=self.lookups)
        return {"overall": self._plain_totals(rows, plan)} if not plan.user_group_by \
            else self._plain_by_group(rows, plan)

    def _plain_totals(self, rows: list, plan: QueryPlan) -> dict:
        agg = catalog.METRICS[plan.metric].aggregates
        out = {f: round(sum(r.get(f) or 0.0 for r in rows), 2) for f in agg}
        out["record_count"] = sum(r.get("__count") or 0 for r in rows)
        return out

    def _plain_by_group(self, rows: list, plan: QueryPlan) -> dict:
        agg = catalog.METRICS[plan.metric].aggregates
        fields = [plan.group_fields[k] for k in plan.user_group_by]
        grouped: dict = {}
        for r in rows:
            key = builder.group_key(builder._label(r.get(f)) for f in fields)
            grouped.setdefault(key, []).append(r)
        out = {k: {**{f: round(sum(x.get(f) or 0.0 for x in v), 2) for f in agg},
                   "record_count": sum(x.get("__count") or 0 for x in v)}
               for k, v in grouped.items()}
        return dict(sorted(out.items(), key=lambda kv: str(kv[0]))) \
            if plan.folds_hour_of_day else out
