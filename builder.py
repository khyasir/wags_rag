"""Turn whitelisted tool arguments into an Odoo read_group call. Pure.

Imports only ``catalog`` and ``dates``. No rpc, no llm, no store, no fastapi —
asserted by a test, so this stays wrappable as an MCP tool later.

The contract: arguments in, a ``QueryPlan`` out. Anything not on the whitelist
raises ``WhitelistError`` with a message that is safe to show the user and
actionable by the model. Nothing is ever passed through unvalidated, and nothing
is silently substituted — a group-by that is impossible on a metric's model is
an error, not a quiet downgrade to something else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import catalog
from catalog import DATE_GRAINS, FILTERS, GROUP_BY, LINES, METRICS, PAYMENTS
from dates import DateRange, DateRangeError, resolve_range

ARG_KEYS = {"metric", "period", "start_date", "end_date", "group_by", "filters"}
MAX_GROUP_BY = 3


class WhitelistError(ValueError):
    """Rejected argument. Message is safe to show the user."""


@dataclass(frozen=True)
class QueryPlan:
    metric: str
    model: str
    domain: list
    #: read_group ``fields`` argument, e.g. ["amount_untaxed:sum"].
    fields: list
    #: read_group ``groupby`` argument, already including any internal grouping.
    groupby: list
    context: dict
    date_range: DateRange
    #: What the user asked to group by, in their own keys.
    user_group_by: tuple = ()
    #: Grouping we added ourselves, e.g. order_type for the returns split.
    internal_group_by: tuple = ()
    returns_split: bool = False
    #: Grouping key -> the read_group field, so results can be relabelled.
    group_fields: dict = field(default_factory=dict)
    #: Plain-words caveats the answer must carry.
    notes: tuple = ()
    #: Hour buckets must be folded to 0-23 in Python; Odoo cannot group that way.
    folds_hour_of_day: bool = False


# --------------------------------------------------------------- validation

def _require_int_ids(value, key: str) -> list:
    if isinstance(value, int):
        value = [value]
    if not isinstance(value, (list, tuple)) or not value:
        raise WhitelistError(f"Filter '{key}' needs a list of ids.")
    out = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, int):
            raise WhitelistError(f"Filter '{key}' accepts whole-number ids only.")
        out.append(v)
    return out


def _parse_date(value, key: str) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise WhitelistError(f"'{key}' must look like 2026-01-31.")
    raise WhitelistError(f"'{key}' must look like 2026-01-31.")


# ------------------------------------------------------------------ group_by

def _resolve_group_by(metric_key: str, keys) -> tuple[list, dict, list, bool]:
    """Map group-by keys to read_group fields.

    Returns (groupby_fields, key_to_field, ordered_keys, folds_hour_of_day).
    """
    metric = METRICS[metric_key]
    if keys in (None, ""):
        keys = []
    if isinstance(keys, str):
        keys = [keys]
    if not isinstance(keys, (list, tuple)):
        raise WhitelistError("'group_by' must be a list of keys.")

    ordered, seen = [], set()
    for k in keys:
        if k in seen:
            continue
        seen.add(k)
        ordered.append(k)

    if len(ordered) > MAX_GROUP_BY:
        raise WhitelistError(
            f"At most {MAX_GROUP_BY} groupings at once. You asked for "
            f"{len(ordered)}. Drop one.")

    grains = [k for k in ordered if k in DATE_GRAINS]
    if len(grains) > 1:
        raise WhitelistError(
            f"Only one time grouping at a time. You asked for {', '.join(grains)}.")

    allowed = catalog.allowed_group_by(metric_key)
    fields, key_to_field = [], {}
    folds_hour_of_day = False

    for k in ordered:
        # Vague keys are refused so the model asks instead of guessing.
        if k in catalog.AMBIGUOUS_GROUP_BY:
            options = catalog.AMBIGUOUS_GROUP_BY[k]
            raise WhitelistError(
                f"'{k}' is ambiguous — it could mean {' or '.join(options)}. "
                f"Ask which one is wanted, then use that key.")

        if k not in GROUP_BY and k not in DATE_GRAINS:
            raise WhitelistError(
                f"Unknown group_by '{k}'. Allowed for {metric_key}: "
                f"{', '.join(allowed) or 'none'}.")
        if k not in allowed:
            if k in DATE_GRAINS:
                raise WhitelistError(
                    f"The metric '{metric_key}' cannot be grouped by {k}, "
                    f"because order lines carry no date of their own. Group a "
                    f"date-based metric by {k} instead, or use "
                    f"{', '.join(allowed) or 'no grouping'}.")
            raise WhitelistError(
                f"The metric '{metric_key}' cannot be grouped by '{k}'. "
                f"Allowed: {', '.join(allowed) or 'none'}.")

        if k in catalog.DERIVED_DATE_GRAINS:
            # Ask Odoo for the server grain; the fold to 0-23 happens later.
            server_grain = catalog.DERIVED_DATE_GRAINS[k]
            spec = f"{metric.date_group_field}:{server_grain}"
            folds_hour_of_day = True
        elif k in DATE_GRAINS:
            spec = f"{metric.date_group_field}:{k}"
        else:
            spec = GROUP_BY[k][metric.model]
        fields.append(spec)
        key_to_field[k] = spec

    return fields, key_to_field, ordered, folds_hour_of_day


# ------------------------------------------------------------------- filters

def _apply_filters(metric_key: str, filters: dict, *, lookups,
                   resolved_product_ids, branch_scope) -> tuple[list, list, int]:
    """Build filter domain terms.

    Returns (terms, notes, branches_matched_by_keyword). The last value lets the
    caller split the answer per branch when a keyword was ambiguous.
    """
    metric = METRICS[metric_key]
    model = metric.model
    terms, notes = [], []
    keyword_matches = 0

    if filters in (None, ""):
        filters = {}
    if not isinstance(filters, dict):
        raise WhitelistError("'filters' must be an object of filter keys.")

    for key in filters:
        if key not in FILTERS:
            raise WhitelistError(
                f"Unknown filter '{key}'. Allowed for {metric_key}: "
                f"{', '.join(metric.allows_filters) or 'none'}.")
        if key not in metric.allows_filters:
            raise WhitelistError(
                f"The filter '{key}' does not apply to '{metric_key}'. "
                f"Allowed: {', '.join(metric.allows_filters) or 'none'}.")

    requested_branches = None

    # -- branch, by id or by name
    if "branch_ids" in filters:
        requested_branches = _require_int_ids(filters["branch_ids"], "branch_ids")

    if "branch_keyword" in filters:
        kw = str(filters["branch_keyword"]).strip()
        found = lookups.resolve("branches", kw)
        if not found:
            known = ", ".join(r.name for r in lookups.branches)
            raise WhitelistError(
                f"No branch matches '{kw}'. Branches are: {known}.")

        # Always say which branch was matched. Some branches have an English
        # and an Arabic name that disagree with each other, so a match that
        # looks obvious can easily be the wrong record.
        keyword_matches = len(found)
        matched = ", ".join(lookups.name_of("branches", i) for i in found)
        if len(found) > 1:
            notes.append(
                f"'{kw}' matched {len(found)} branches: {matched}. Each one is "
                f"reported separately below.")
        else:
            notes.append(f"'{kw}' was matched to the branch {matched}.")

        requested_branches = (found if requested_branches is None
                              else [i for i in found if i in requested_branches])
        if not requested_branches:
            raise WhitelistError(
                f"The branch '{kw}' is outside the branches you can see.")

    # Env-level branch scope is a hard boundary, applied whether or not the
    # caller asked for a branch.
    if branch_scope:
        requested_branches = (list(branch_scope) if requested_branches is None
                              else [i for i in requested_branches if i in branch_scope])
        if not requested_branches:
            raise WhitelistError(
                "That branch is outside the branches this app is allowed to read.")

    if requested_branches is not None:
        branch_field = FILTERS["branch_ids"].fields.get(model)
        if not branch_field:
            # Payment lines carry no branch. Returning every branch instead
            # would quietly break the scope, so refuse.
            raise WhitelistError(
                f"The metric '{metric_key}' cannot be filtered by branch, "
                f"because payment lines do not record a branch.")
        terms.append((branch_field, "in", requested_branches))

    # -- order type
    type_field = FILTERS["order_type_ids"].fields.get(model)
    if "order_type_ids" in filters:
        ids = _require_int_ids(filters["order_type_ids"], "order_type_ids")
        terms.append((type_field, "in", ids))

    if filters.get("exclude_returns"):
        return_ids = lookups.return_type_ids()
        if return_ids:
            terms.append((type_field, "not in", return_ids))
            notes.append("Return orders are excluded entirely from this figure.")

    # -- product, resolved by the caller because product.product is not cached
    if "product_keyword" in filters:
        kw = str(filters["product_keyword"]).strip()
        if resolved_product_ids is None:
            raise WhitelistError(
                "product_keyword must be resolved to ids before building the "
                "query. This is a wiring bug, not something you did wrong.")
        if not resolved_product_ids:
            raise WhitelistError(f"No product matches '{kw}'.")
        terms.append(("product_id", "in", list(resolved_product_ids)))

    # -- payment method
    if "payment_method_keyword" in filters:
        kw = str(filters["payment_method_keyword"]).strip()
        found = lookups.resolve("payment_methods", kw)
        if not found:
            known = ", ".join(r.name for r in lookups.payment_methods)
            raise WhitelistError(
                f"No payment method matches '{kw}'. Methods are: {known}.")
        terms.append(("payment_method_id", "in", found))

    if filters.get("is_cash"):
        cash_ids = lookups.cash_method_ids()
        if not cash_ids:
            raise WhitelistError(
                "No payment method is marked as cash, so cash cannot be "
                "separated out.")
        terms.append(("payment_method_id", "in", cash_ids))

    return terms, notes, keyword_matches


# -------------------------------------------------------------------- public

def build(
    args: dict,
    *,
    today: date,
    tz_name: str,
    data_start: Optional[date],
    lookups: catalog.Lookups,
    branch_scope: tuple = (),
    resolved_product_ids: Optional[list] = None,
) -> QueryPlan:
    """Validate arguments and produce the read_group call.

    ``data_start=None`` means no date floor, which is the local testing setting.
    """
    if not isinstance(args, dict):
        raise WhitelistError("Query arguments must be an object.")

    unknown = set(args) - ARG_KEYS
    if unknown:
        raise WhitelistError(
            f"Unknown argument(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(ARG_KEYS))}.")

    metric_key = args.get("metric")
    if metric_key not in METRICS:
        raise WhitelistError(
            f"Unknown metric '{metric_key}'. Allowed: {', '.join(METRICS)}.")
    metric = METRICS[metric_key]

    # -- date range
    period = args.get("period") or "this_month"
    try:
        date_range = resolve_range(
            period,
            today=today,
            tz_name=tz_name,
            data_start=data_start,
            start_date=_parse_date(args.get("start_date"), "start_date"),
            end_date=_parse_date(args.get("end_date"), "end_date"),
        )
    except DateRangeError as exc:
        raise WhitelistError(str(exc)) from exc

    notes = []
    if date_range.clipped:
        notes.append(
            f"Data starts {data_start}, so the range was shortened to begin "
            f"there ({date_range.description}).")

    # -- domain: forced terms first, so state can never be dropped
    domain = list(metric.forced_domain)

    if metric.only_return_types:
        return_ids = lookups.return_type_ids()
        if not return_ids:
            raise WhitelistError(
                "No order type is marked as a return, so returns cannot be "
                "reported.")
        domain.append(("order_type", "in", return_ids))

    if date_range.utc_start:
        domain.append((metric.date_filter_field, ">=", date_range.utc_start))
    if date_range.utc_end:
        domain.append((metric.date_filter_field, "<", date_range.utc_end))

    filter_terms, filter_notes, branch_matches = _apply_filters(
        metric_key,
        args.get("filters") or {},
        lookups=lookups,
        resolved_product_ids=resolved_product_ids,
        branch_scope=branch_scope,
    )
    domain.extend(filter_terms)
    notes.extend(filter_notes)

    # -- grouping
    groupby, key_to_field, user_keys, folds_hour = _resolve_group_by(
        metric_key, args.get("group_by"))

    # An ambiguous branch keyword gets reported per branch rather than summed.
    # "madinah" matches two differently-named branches, and one combined figure
    # hides which is which — worse here than usual, because the English and
    # Arabic names of these records disagree.
    branch_field = catalog.group_by_field("branch", metric.model)
    if branch_matches > 1 and branch_field and "branch" not in user_keys:
        if len(user_keys) < MAX_GROUP_BY:
            groupby.append(branch_field)
            key_to_field["branch"] = branch_field
            user_keys.append("branch")
        else:
            notes.append(
                "The branches could not be listed separately because too many "
                "other groupings were asked for, so the figure covers all of "
                "them together.")

    internal = []
    if metric.returns_split:
        split_field = GROUP_BY["order_type"][metric.model]
        if split_field not in groupby:
            groupby.append(split_field)
            internal.append("order_type")

    if metric_key == "products":
        notes.append(
            "Lines with no product/extra marker are counted as products.")

    return QueryPlan(
        metric=metric_key,
        model=metric.model,
        domain=domain,
        fields=[f"{f}:sum" for f in metric.aggregates],
        groupby=groupby,
        # tz drives date bucket boundaries; verified via read_group __range.
        context={"tz": tz_name},
        date_range=date_range,
        user_group_by=tuple(user_keys),
        internal_group_by=tuple(internal),
        returns_split=metric.returns_split,
        group_fields=key_to_field,
        notes=tuple(notes),
        folds_hour_of_day=folds_hour,
    )


# ------------------------------------------------------- returns arithmetic

def _m2o_id(value):
    """read_group returns m2o as [id, name], or False when unset."""
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return value or None


def fold_returns(buckets: list, *, lookups: catalog.Lookups,
                 order_type_field: str = "order_type") -> dict:
    """Collapse per-order-type buckets into net / gross / returns.

    Returns are separate records carrying positive amounts, so net is gross
    minus returns. Never report net on its own.

    Buckets whose order type is unset count as NON-return. One validated order
    locally has no order type; dropping it would make gross stop equalling the
    sum of the buckets. See known_limits.null_order_type.

    Pure, so this is covered by unit tests on synthetic buckets — which matters
    because the local database has zero return orders to exercise it with.
    """
    gross = returns = 0.0
    gross_total = returns_total = 0.0
    gross_tax = returns_tax = 0.0
    discount = 0.0
    order_count = return_count = 0

    for b in buckets:
        untaxed = b.get("amount_untaxed") or 0.0
        total = b.get("amount_total") or 0.0
        tax = b.get("amount_tax") or 0.0
        count = b.get("__count") or 0
        discount += b.get("discount_amount") or 0.0

        if lookups.is_return_type(_m2o_id(b.get(order_type_field))):
            returns += untaxed
            returns_total += total
            returns_tax += tax
            return_count += count
        else:
            gross += untaxed
            gross_total += total
            gross_tax += tax
            order_count += count

    net = gross - returns
    return {
        "net_sales": round(net, 2),
        "gross_sales": round(gross, 2),
        "return_amount": round(returns, 2),
        "net_total_with_vat": round(gross_total - returns_total, 2),
        "net_tax": round(gross_tax - returns_tax, 2),
        "discount_amount": round(discount, 2),
        "order_count": order_count,
        "return_count": return_count,
        "average_ticket": round(net / order_count, 2) if order_count else None,
    }


def fold_returns_by_group(buckets: list, *, plan: QueryPlan,
                          lookups: catalog.Lookups) -> dict:
    """Same arithmetic, done per user-requested group.

    The order-type dimension is internal, so it is folded away and the user sees
    one row per group they actually asked for.
    """
    order_type_field = catalog.GROUP_BY["order_type"][plan.model]
    group_fields = [plan.group_fields[k] for k in plan.user_group_by
                    if k != "order_type"]

    if not group_fields:
        return {"overall": fold_returns(
            buckets, lookups=lookups, order_type_field=order_type_field)}

    grouped: dict = {}
    for b in buckets:
        key = group_key(_label(b.get(f)) for f in group_fields)
        grouped.setdefault(key, []).append(b)

    out = {
        k: fold_returns(rows, lookups=lookups,
                        order_type_field=order_type_field)
        for k, rows in grouped.items()
    }
    # Hour-of-day reads as a clock, so keep it in clock order.
    return (dict(sorted(out.items(), key=lambda kv: str(kv[0])))
            if plan.folds_hour_of_day else out)


def _label(value):
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return value[1]
    return value if value not in (False, None) else "Not set"


def group_key(labels) -> str:
    """One string key per group, even for multiple groupings.

    A tuple key cannot be serialised to JSON, so multi-grouping results could
    not be written to Supabase or handed to the model — "sales per branch per
    month" failed outright with `keys must be str, int, float, bool or None,
    not tuple`. Joining is also more readable in the answer.
    """
    parts = [str(l) for l in labels]
    return parts[0] if len(parts) == 1 else " / ".join(parts)
