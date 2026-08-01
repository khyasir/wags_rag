"""Stage one tests. No network, no Odoo, no Groq.

Every guardrail in the spec has a test that fails if the guardrail is removed.
That is the point of this file — not coverage for its own sake.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import builder                                    # noqa: E402
import catalog                                    # noqa: E402
import dates                                      # noqa: E402
from builder import WhitelistError, build         # noqa: E402
from catalog import LookupRecord, Lookups         # noqa: E402

TZ = "Asia/Riyadh"
TODAY = date(2026, 7, 31)          # a Friday
FLOOR = date(2026, 1, 1)


@pytest.fixture
def lookups():
    """Mirrors the real local database, including its awkward rows."""
    return Lookups(
        branches=(
            LookupRecord(1, "RUH-TWN-A", ("ruh-twn-a",)),
            LookupRecord(2, "Madinah Branch", ("madinah branch", "الفرع الرياض")),
            LookupRecord(5, "Riyadh", ("riyadh",)),
        ),
        order_types=(
            LookupRecord(1, "Dine In", ("dine in",), {"is_return": False}),
            LookupRecord(2, "Return", ("return",), {"is_return": True}),
            LookupRecord(3, "Hungerstation", ("hungerstation",),
                         {"is_return": False, "is_aggregator": True}),
            # Real rows exist with every flag NULL. They must read as False.
            LookupRecord(10, "Drive Through", ("drive through",), {}),
        ),
        payment_methods=(
            LookupRecord(2, "Visa/Master", ("visa/master",), {"is_cash": False}),
            LookupRecord(9, "Cash RM-TWN", ("cash rm-twn",), {"is_cash": True}),
            LookupRecord(10, "GCCNET", ("gccnet",), {}),
        ),
        categories=(LookupRecord(4, "Beverages", ("beverages",)),),
    )


def plan(args, *, floor=FLOOR, lookups=None, **kw):
    return build(args, today=TODAY, tz_name=TZ, data_start=floor,
                 lookups=lookups, **kw)


# ------------------------------------------------------------ purity contract

def test_builder_imports_nothing_networked():
    """builder must stay wrappable as an MCP tool, so keep it pure."""
    forbidden = {"rpc", "llm", "store", "fastapi", "gradio", "requests",
                 "xmlrpc", "psycopg2", "supabase", "groq"}
    src = (Path(__file__).resolve().parent.parent / "builder.py").read_text()
    for name in forbidden:
        assert f"import {name}" not in src, f"builder.py must not import {name}"


# -------------------------------------------------------------- UTC handling

def test_local_midnight_becomes_utc_21():
    """Riyadh is UTC+3, so local midnight is 21:00 the day before."""
    assert dates.local_midnight_to_utc(date(2026, 3, 15), TZ) == "2026-03-14 21:00:00"


def test_utc_string_maps_back_to_local_date():
    """21:00 UTC is already the next day in Riyadh."""
    assert dates.utc_str_to_local_date("2026-03-14 21:00:00", TZ) == date(2026, 3, 15)


def test_domain_dates_are_utc_not_local(lookups):
    """'Today' in Riyadh is 21:00 yesterday to 21:00 today in UTC."""
    p = plan({"metric": "sales", "period": "today"}, lookups=lookups)
    bounds = {t[1]: t[2] for t in p.domain
              if isinstance(t, tuple) and t[0] == "order_datetime"}
    assert bounds[">="] == "2026-07-30 21:00:00"
    assert bounds["<"] == "2026-07-31 21:00:00"


def test_context_carries_timezone(lookups):
    """read_group buckets dates using the context tz. Verified live via __range."""
    p = plan({"metric": "sales", "period": "today", "group_by": ["day"]},
             lookups=lookups)
    assert p.context == {"tz": TZ}


# --------------------------------------------------------------- week starts

def test_last_week_is_monday_to_sunday():
    r = dates.resolve_range("last_week", today=TODAY, tz_name=TZ, data_start=None)
    assert r.local_start == date(2026, 7, 20)      # Monday
    assert r.local_end == date(2026, 7, 27)        # exclusive, so Sun 26th is last
    assert r.local_start.weekday() == 0


def test_this_week_starts_monday():
    r = dates.resolve_range("this_week", today=TODAY, tz_name=TZ, data_start=None)
    assert r.local_start == date(2026, 7, 27)
    assert r.local_start.weekday() == 0


# ---------------------------------------------------- data floor: switched ON

def test_range_wholly_before_floor_is_refused():
    with pytest.raises(dates.DateRangeError, match="not available"):
        dates.resolve_range("custom", today=TODAY, tz_name=TZ, data_start=FLOOR,
                            start_date=date(2025, 1, 1), end_date=date(2025, 6, 30))


def test_straddling_range_is_clipped_and_flagged():
    r = dates.resolve_range("custom", today=TODAY, tz_name=TZ, data_start=FLOOR,
                            start_date=date(2025, 11, 1), end_date=date(2026, 2, 1))
    assert r.local_start == FLOOR
    assert r.clipped is True
    assert r.requested_start == date(2025, 11, 1)


def test_clipping_produces_a_note_the_answer_must_carry(lookups):
    p = plan({"metric": "sales", "period": "custom",
              "start_date": "2025-11-01", "end_date": "2026-02-01"},
             lookups=lookups)
    assert p.date_range.clipped
    assert any("shortened" in n for n in p.notes)


def test_all_time_is_clipped_to_the_floor():
    r = dates.resolve_range("all_time", today=TODAY, tz_name=TZ, data_start=FLOOR)
    assert r.local_start == FLOOR
    assert r.clipped is True


# --------------------------------------------------- data floor: switched OFF

def test_no_floor_means_a_2024_range_passes_through_unclipped():
    """Local testing setting. Must not silently drop old data."""
    r = dates.resolve_range("custom", today=TODAY, tz_name=TZ, data_start=None,
                            start_date=date(2024, 9, 25), end_date=date(2025, 12, 11))
    assert r.local_start == date(2024, 9, 25)
    assert r.clipped is False


def test_no_floor_never_refuses_an_old_range(lookups):
    p = plan({"metric": "sales", "period": "custom", "start_date": "2024-01-01",
              "end_date": "2024-12-31"}, floor=None, lookups=lookups)
    assert p.date_range.utc_start == "2023-12-31 21:00:00"
    assert not any("shortened" in n for n in p.notes)


def test_no_floor_all_time_has_no_lower_bound(lookups):
    p = plan({"metric": "sales", "period": "all_time"}, floor=None, lookups=lookups)
    assert p.date_range.utc_start is None
    assert not [t for t in p.domain
                if isinstance(t, tuple) and t[0] == "order_datetime" and t[1] == ">="]


# ------------------------------------------------------- forced state filter

@pytest.mark.parametrize("metric_key", list(catalog.METRICS))
def test_every_metric_forces_validated_state(metric_key, lookups):
    p = plan({"metric": metric_key}, lookups=lookups)
    state_terms = [t for t in p.domain
                   if isinstance(t, tuple) and t[0].endswith("state")]
    assert state_terms, f"{metric_key} has no state filter"
    assert all(t[1] == "=" and t[2] == "validate" for t in state_terms)


@pytest.mark.parametrize("metric_key", ["products", "modifiers", "payments"])
def test_child_models_filter_state_through_the_parent(metric_key, lookups):
    p = plan({"metric": metric_key}, lookups=lookups)
    assert ("pos_id.state", "=", "validate") in p.domain


def test_line_metrics_filter_date_through_the_parent(lookups):
    p = plan({"metric": "products", "period": "today"}, lookups=lookups)
    fields = {t[0] for t in p.domain if isinstance(t, tuple)}
    assert "pos_id.order_datetime" in fields
    assert "order_datetime" not in fields


def test_only_order_datetime_is_ever_used(lookups):
    """date, create_date and order_datetime_dup all exist and are all banned."""
    banned = {"date", "create_date", "order_datetime_dup", "effective_date"}
    for metric_key in catalog.METRICS:
        p = plan({"metric": metric_key}, lookups=lookups)
        used = {t[0].split(".")[-1] for t in p.domain if isinstance(t, tuple)}
        assert not (used & banned), f"{metric_key} touched a banned date field"


# ------------------------------------------------------------ rejected input

def test_unknown_metric_is_rejected(lookups):
    with pytest.raises(WhitelistError, match="Unknown metric"):
        plan({"metric": "profit"}, lookups=lookups)


def test_unknown_group_by_is_rejected(lookups):
    with pytest.raises(WhitelistError, match="Unknown group_by"):
        plan({"metric": "sales", "group_by": ["supplier"]}, lookups=lookups)


def test_unknown_filter_is_rejected(lookups):
    with pytest.raises(WhitelistError, match="Unknown filter"):
        plan({"metric": "sales", "filters": {"waiter_name": "ali"}}, lookups=lookups)


def test_unknown_top_level_argument_is_rejected(lookups):
    with pytest.raises(WhitelistError, match="Unknown argument"):
        plan({"metric": "sales", "domain": [["id", "=", 1]]}, lookups=lookups)


def test_raw_model_name_cannot_be_injected(lookups):
    with pytest.raises(WhitelistError, match="Unknown argument"):
        plan({"metric": "sales", "model": "res.users"}, lookups=lookups)


def test_group_by_not_possible_on_this_metric_is_rejected(lookups):
    """products has no date of its own; grouping by day must fail loudly."""
    with pytest.raises(WhitelistError, match="carry no date of their own"):
        plan({"metric": "products", "group_by": ["day"]}, lookups=lookups)


def test_branch_grouping_rejected_for_payments(lookups):
    """pos_id.branch_id grouping is unsupported by read_group. Verified live."""
    with pytest.raises(WhitelistError, match="cannot be grouped by 'branch'"):
        plan({"metric": "payments", "group_by": ["branch"]}, lookups=lookups)


def test_product_grouping_rejected_for_sales(lookups):
    with pytest.raises(WhitelistError, match="cannot be grouped by 'product'"):
        plan({"metric": "sales", "group_by": ["product"]}, lookups=lookups)


def test_two_time_groupings_rejected(lookups):
    with pytest.raises(WhitelistError, match="Only one time grouping"):
        plan({"metric": "sales", "group_by": ["day", "month"]}, lookups=lookups)


def test_too_many_groupings_rejected(lookups):
    with pytest.raises(WhitelistError, match="At most 3 groupings"):
        plan({"metric": "sales",
              "group_by": ["day", "branch", "customer", "employee"]},
             lookups=lookups)


def test_filter_on_wrong_metric_is_rejected(lookups):
    with pytest.raises(WhitelistError, match="does not apply"):
        plan({"metric": "sales", "filters": {"is_cash": True}}, lookups=lookups)


def test_non_integer_ids_rejected(lookups):
    with pytest.raises(WhitelistError, match="whole-number ids"):
        plan({"metric": "sales", "filters": {"branch_ids": ["1 OR 1=1"]}},
             lookups=lookups)


def test_bad_date_string_rejected(lookups):
    with pytest.raises(WhitelistError, match="2026-01-31"):
        plan({"metric": "sales", "period": "custom", "start_date": "last tuesday"},
             lookups=lookups)


# -------------------------------------------------------- forced branch scope

def test_branch_scope_is_applied_even_when_not_asked_for(lookups):
    p = plan({"metric": "sales"}, lookups=lookups, branch_scope=(1, 5))
    assert ("branch_id", "in", [1, 5]) in p.domain


def test_branch_scope_narrows_a_requested_branch(lookups):
    p = plan({"metric": "sales", "filters": {"branch_ids": [1, 2, 5]}},
             lookups=lookups, branch_scope=(1, 5))
    assert ("branch_id", "in", [1, 5]) in p.domain


def test_branch_outside_scope_is_refused(lookups):
    with pytest.raises(WhitelistError, match="outside the branches"):
        plan({"metric": "sales", "filters": {"branch_ids": [2]}},
             lookups=lookups, branch_scope=(1, 5))


def test_scope_refuses_rather_than_leaking_all_branches(lookups):
    """Payments carry no branch, so a scoped install must refuse, not widen."""
    with pytest.raises(WhitelistError, match="do not record a branch"):
        plan({"metric": "payments"}, lookups=lookups, branch_scope=(1,))


# ------------------------------------------------- keyword resolves to ids only

def test_branch_keyword_becomes_ids(lookups):
    p = plan({"metric": "sales", "filters": {"branch_keyword": "riyadh"}},
             lookups=lookups)
    assert ("branch_id", "in", [5]) in p.domain


@pytest.mark.parametrize("keyword", [
    "RUH-TWN-A",        # exact, as stored
    "ruh-twn-a",        # lowercase
    "twn",              # partial token
    "TWN",              # partial, wrong case
    "twn branch",       # filler word people add
    "TWN Branch",
    "RUH TWN A",        # spaces where Odoo has dashes
    "ruh twn",
    "ruh-twn",
    "twn-a",
])
def test_branch_code_matches_however_it_is_typed(keyword, lookups):
    """Branch codes are written with dashes and spoken with spaces."""
    p = plan({"metric": "sales", "filters": {"branch_keyword": keyword}},
             lookups=lookups)
    assert ("branch_id", "in", [1]) in p.domain, keyword


def test_single_letter_does_not_match_every_branch(lookups):
    """'a' used to match all four branches. It must match whole words only."""
    assert lookups.resolve("branches", "a") == [1]


def test_filler_word_alone_is_not_a_match(lookups):
    with pytest.raises(WhitelistError, match="No branch matches"):
        plan({"metric": "sales", "filters": {"branch_keyword": "branch"}},
             lookups=lookups)


def test_matched_branch_is_always_named_in_the_answer(lookups):
    """English and Arabic branch names disagree, so never match silently."""
    p = plan({"metric": "sales", "filters": {"branch_keyword": "twn"}},
             lookups=lookups)
    assert any("RUH-TWN-A" in n for n in p.notes)


def test_ambiguous_branch_keyword_says_how_many_matched(lookups):
    p = plan({"metric": "sales", "filters": {"branch_keyword": "madinah"}},
             lookups=lookups)
    assert ("branch_id", "in", [2]) in p.domain
    assert any("matched" in n for n in p.notes)


# ------------------------------------------- ambiguous keyword splits by branch

@pytest.fixture
def two_madinah(lookups):
    """Both Madinah branches, so 'madinah' is genuinely ambiguous."""
    from dataclasses import replace
    return replace(lookups, branches=lookups.branches + (
        LookupRecord(6, "Madinah Branch New",
                     ("madinah branch new", "الفرع الرئيسي الرياض")),))


def test_ambiguous_branch_is_split_not_summed(two_madinah):
    """One combined figure would hide which branch it came from."""
    p = plan({"metric": "sales", "filters": {"branch_keyword": "madinah"}},
             lookups=two_madinah)
    assert ("branch_id", "in", [2, 6]) in p.domain
    assert "branch" in p.user_group_by
    assert "branch_id" in p.groupby
    assert any("separately" in n for n in p.notes)


def test_unambiguous_branch_is_not_split(lookups):
    p = plan({"metric": "sales", "filters": {"branch_keyword": "twn"}},
             lookups=lookups)
    assert "branch" not in p.user_group_by


def test_branch_not_double_grouped_when_user_asked_for_it(two_madinah):
    p = plan({"metric": "sales", "group_by": ["branch"],
              "filters": {"branch_keyword": "madinah"}}, lookups=two_madinah)
    assert p.groupby.count("branch_id") == 1


def test_split_gives_way_when_grouping_cap_is_full(two_madinah):
    """Cannot add a 4th grouping, so say the branches were merged."""
    p = plan({"metric": "sales", "group_by": ["month", "customer", "employee"],
              "filters": {"branch_keyword": "madinah"}}, lookups=two_madinah)
    assert "branch" not in p.user_group_by
    assert any("could not be listed separately" in n for n in p.notes)


# ------------------------------------------------------ ambiguous "category"

def test_bare_category_is_refused_not_guessed(lookups):
    """category_id is NULL on every line, so a silent choice answers nothing."""
    with pytest.raises(WhitelistError, match="ambiguous"):
        plan({"metric": "products", "group_by": ["category"]}, lookups=lookups)


def test_category_refusal_names_both_options(lookups):
    with pytest.raises(WhitelistError,
                       match="accounting_category or pos_category"):
        plan({"metric": "products", "group_by": ["category"]}, lookups=lookups)


@pytest.mark.parametrize("key,expected", [
    ("accounting_category", "category_id"),
    ("pos_category", "pos_category_id"),
])
def test_explicit_category_keys_work(key, expected, lookups):
    p = plan({"metric": "products", "group_by": [key]}, lookups=lookups)
    assert p.groupby == [expected]


# -------------------------------------------------------------- hour_of_day

def test_hour_of_day_asks_odoo_for_hour_buckets(lookups):
    """Odoo cannot group 0-23, so we request hour and fold it ourselves."""
    p = plan({"metric": "sales", "group_by": ["hour_of_day"]}, lookups=lookups)
    assert "order_datetime:hour" in p.groupby
    assert p.folds_hour_of_day is True
    assert p.user_group_by == ("hour_of_day",)


def test_plain_hour_does_not_fold(lookups):
    p = plan({"metric": "sales", "group_by": ["hour"]}, lookups=lookups)
    assert p.folds_hour_of_day is False


def test_hour_of_day_counts_as_a_time_grouping(lookups):
    with pytest.raises(WhitelistError, match="Only one time grouping"):
        plan({"metric": "sales", "group_by": ["hour_of_day", "month"]},
             lookups=lookups)


def test_hour_of_day_rejected_for_line_metrics(lookups):
    with pytest.raises(WhitelistError, match="carry no date of their own"):
        plan({"metric": "products", "group_by": ["hour_of_day"]},
             lookups=lookups)


def test_hour_of_day_available_for_payments(lookups):
    p = plan({"metric": "payments", "group_by": ["hour_of_day"]},
             lookups=lookups)
    assert "order_datetime:hour" in p.groupby


def test_branch_keyword_matches_arabic(lookups):
    p = plan({"metric": "sales", "filters": {"branch_keyword": "الفرع الرياض"}},
             lookups=lookups)
    assert ("branch_id", "in", [2]) in p.domain


def test_no_keyword_ever_reaches_the_big_table(lookups):
    p = plan({"metric": "sales", "filters": {"branch_keyword": "riyadh"}},
             lookups=lookups)
    for term in p.domain:
        if isinstance(term, tuple) and term[1] in ("in", "not in"):
            assert all(isinstance(v, int) for v in term[2])
        if isinstance(term, tuple):
            assert "ilike" != term[1]


def test_unmatched_branch_keyword_lists_the_real_branches(lookups):
    with pytest.raises(WhitelistError, match="RUH-TWN-A"):
        plan({"metric": "sales", "filters": {"branch_keyword": "jeddah"}},
             lookups=lookups)


def test_product_keyword_requires_prior_resolution(lookups):
    with pytest.raises(WhitelistError, match="resolved to ids"):
        plan({"metric": "products", "filters": {"product_keyword": "latte"}},
             lookups=lookups)


def test_product_keyword_uses_resolved_ids(lookups):
    p = plan({"metric": "products", "filters": {"product_keyword": "latte"}},
             lookups=lookups, resolved_product_ids=[4740])
    assert ("product_id", "in", [4740]) in p.domain


def test_is_cash_resolves_from_the_flag_not_the_name(lookups):
    p = plan({"metric": "payments", "filters": {"is_cash": True}}, lookups=lookups)
    assert ("payment_method_id", "in", [9]) in p.domain


# ------------------------------------------------------ the app_line_id rule

def test_products_include_lines_with_no_marker(lookups):
    """34% of local lines have no app_line_id. They count as products."""
    p = plan({"metric": "products"}, lookups=lookups)
    assert "|" in p.domain
    assert ("app_line_id", "<", 100000) in p.domain
    assert ("app_line_id", "=", False) in p.domain


def test_modifiers_exclude_lines_with_no_marker(lookups):
    p = plan({"metric": "modifiers"}, lookups=lookups)
    assert ("app_line_id", ">=", 100000) in p.domain
    assert ("app_line_id", "=", False) not in p.domain


def test_products_answer_carries_the_caveat(lookups):
    p = plan({"metric": "products"}, lookups=lookups)
    assert any("no product/extra marker" in n for n in p.notes)


# ------------------------------------------------------- returns are flag-led

def test_returns_metric_uses_the_flag_not_the_name(lookups):
    p = plan({"metric": "returns"}, lookups=lookups)
    assert ("order_type", "in", [2]) in p.domain


def test_sales_always_groups_by_order_type_internally(lookups):
    p = plan({"metric": "sales", "period": "this_month"}, lookups=lookups)
    assert "order_type" in p.groupby
    assert p.internal_group_by == ("order_type",)
    assert p.user_group_by == ()


def test_order_type_not_duplicated_when_user_asked_for_it(lookups):
    p = plan({"metric": "sales", "group_by": ["order_type"]}, lookups=lookups)
    assert p.groupby.count("order_type") == 1
    assert p.internal_group_by == ()


def test_exclude_returns_drops_them_and_says_so(lookups):
    p = plan({"metric": "sales", "filters": {"exclude_returns": True}},
             lookups=lookups)
    assert ("order_type", "not in", [2]) in p.domain
    assert any("excluded" in n for n in p.notes)


# -------------------------------------------------------- returns arithmetic

def buckets(*rows):
    """rows are (order_type, untaxed, total, tax, count)."""
    return [{"order_type": ot, "amount_untaxed": u, "amount_total": t,
             "amount_tax": x, "__count": c} for ot, u, t, x, c in rows]


def test_net_is_gross_minus_returns(lookups):
    r = builder.fold_returns(
        buckets(([1, "Dine In"], 1000.0, 1150.0, 150.0, 10),
                ([2, "Return"], 200.0, 230.0, 30.0, 3)),
        lookups=lookups)
    assert r["gross_sales"] == 1000.0
    assert r["return_amount"] == 200.0
    assert r["net_sales"] == 800.0
    assert r["order_count"] == 10
    assert r["return_count"] == 3


def test_average_ticket_uses_net_over_order_count(lookups):
    r = builder.fold_returns(
        buckets(([1, "Dine In"], 1000.0, 1150.0, 150.0, 8),
                ([2, "Return"], 200.0, 230.0, 30.0, 2)),
        lookups=lookups)
    assert r["average_ticket"] == 100.0


def test_average_ticket_is_none_not_a_crash_when_no_orders(lookups):
    r = builder.fold_returns(
        buckets(([2, "Return"], 50.0, 57.5, 7.5, 1)), lookups=lookups)
    assert r["order_count"] == 0
    assert r["average_ticket"] is None
    assert r["net_sales"] == -50.0


def test_unset_order_type_counts_as_non_return(lookups):
    """One local order has order_type NULL. Gross must still equal the sum."""
    r = builder.fold_returns(
        buckets((False, 8.26, 9.5, 1.24, 1),
                ([1, "Dine In"], 100.0, 115.0, 15.0, 4)),
        lookups=lookups)
    assert r["gross_sales"] == 108.26
    assert r["order_count"] == 5
    assert r["return_amount"] == 0.0


def test_order_type_with_null_flags_counts_as_non_return(lookups):
    """11 real order types have every flag NULL. NULL is not 'unknown'."""
    r = builder.fold_returns(
        buckets(([10, "Drive Through"], 500.0, 575.0, 75.0, 5)), lookups=lookups)
    assert r["gross_sales"] == 500.0
    assert r["return_amount"] == 0.0


def test_returns_maths_per_group(lookups):
    p = plan({"metric": "sales", "group_by": ["branch"]}, lookups=lookups)
    rows = [
        {"branch_id": [1, "RUH-TWN-A"], "order_type": [1, "Dine In"],
         "amount_untaxed": 1000.0, "__count": 10},
        {"branch_id": [1, "RUH-TWN-A"], "order_type": [2, "Return"],
         "amount_untaxed": 100.0, "__count": 1},
        {"branch_id": [5, "Riyadh"], "order_type": [1, "Dine In"],
         "amount_untaxed": 500.0, "__count": 5},
    ]
    out = builder.fold_returns_by_group(rows, plan=p, lookups=lookups)
    assert out["RUH-TWN-A"]["net_sales"] == 900.0
    assert out["RUH-TWN-A"]["return_amount"] == 100.0
    assert out["Riyadh"]["net_sales"] == 500.0
    assert out["Riyadh"]["return_count"] == 0


# ---------------------------------------------------------- catalog contract

def test_only_the_three_big_models_are_reachable():
    assert {m.model for m in catalog.METRICS.values()} <= set(catalog.BIG_MODELS)


def test_no_metric_exposes_a_field_outside_its_whitelist():
    """Aggregates must stay inside the fields the spec exposes."""
    exposed = {
        catalog.ORDERS: {"amount_untaxed", "amount_total", "amount_tax",
                         "discount_amount", "coupon_amount", "voucher_amount"},
        catalog.LINES: {"quantity", "price_subtotal", "amount_untaxed",
                        "price_total", "discount_amount", "price_unit", "taxes"},
        catalog.PAYMENTS: {"amount"},
    }
    for key, m in catalog.METRICS.items():
        assert set(m.aggregates) <= exposed[m.model], key


def test_system_prompt_is_generated_and_carries_the_limits(lookups):
    prompt = catalog.build_system_prompt(
        today=TODAY, tz_name=TZ, data_start=FLOOR, lookups=lookups,
        max_buckets=200)
    assert "2026-07-31" in prompt
    assert "Asia/Riyadh" in prompt
    assert "Monday to Sunday" in prompt
    for metric_key in catalog.METRICS:
        assert metric_key in prompt
    assert "KNOWN LIMITS" in prompt
    assert "commission" in prompt          # aggregator caveat reaches the model


def test_prompt_tells_the_model_not_to_over_group(lookups):
    """The model added group_by ["day"] to "what is the average ticket" and
    answered with 47 numbers instead of one. The prompt must forbid that."""
    prompt = catalog.build_system_prompt(
        today=TODAY, tz_name=TZ, data_start=None, lookups=lookups,
        max_buckets=200)
    assert "ASK FOR THE LEAST" in prompt
    assert "NO group_by unless" in prompt
    assert "average ticket" in prompt          # worked example
    assert "exclude_returns on your own" in prompt


def test_no_floor_is_stated_in_the_prompt(lookups):
    prompt = catalog.build_system_prompt(
        today=TODAY, tz_name=TZ, data_start=None, lookups=lookups,
        max_buckets=200)
    assert "No date floor" in prompt
