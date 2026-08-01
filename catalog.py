"""The whitelist. Single source of truth for what may ever be queried.

Nothing outside this file decides which model, field, or filter is legal. The
model picks *keys*; this file is the only place a key becomes a field name.

Three hard constraints are encoded here rather than discovered at runtime:

1. ``state = validate`` is attached to every metric by ``forced_domain``. It is
   never supplied by the caller, so it cannot be forgotten or overridden.

2. ``pos.wags.tree`` has no exposed date or state field, so both go through the
   parent (``pos_id.order_datetime``, ``pos_id.state``). Verified working.

3. Odoo's ``read_group`` cannot group by a related field. Verified against the
   live database: ``pos_id.order_datetime:day`` and ``pos_id.branch_id`` both
   raise. So line metrics cannot be grouped by date at all, and payments cannot
   be grouped by branch. Those combinations are absent from ``GROUP_BY`` and
   therefore rejected with a clear message — never silently substituted.

Adding a metric means adding an entry here. It does not mean touching
``builder.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import known_limits

# ------------------------------------------------------------------- models

ORDERS = "pos.wags"
LINES = "pos.wags.tree"
PAYMENTS = "pos.wags.payment.method"

BIG_MODELS = (ORDERS, LINES, PAYMENTS)

#: Line and modifier split. Below this is a product, at or above is a modifier.
MODIFIER_THRESHOLD = 100000


# ------------------------------------------------------------------ metrics

@dataclass(frozen=True)
class Metric:
    model: str
    #: Field the date range filters on. ``pos_id.`` prefixed for line metrics.
    date_filter_field: str
    #: Field a date *group_by* uses. None when the model has no own date field,
    #: which makes date grouping impossible for that metric.
    date_group_field: Optional[str]
    aggregates: tuple
    forced_domain: tuple
    #: Group-by keys the model is allowed to request.
    allows: tuple
    #: Filter keys the model is allowed to request.
    allows_filters: tuple
    #: Grouped by order type internally so returns can be split out, whether or
    #: not the user asked for it.
    returns_split: bool = False
    #: Restrict to order types flagged is_return. Resolved from the lookup cache.
    only_return_types: bool = False
    description: str = ""


_ORDER_GROUPS = ("day", "week", "month", "hour", "hour_of_day", "branch",
                 "order_type", "customer", "employee", "cashbox")
_ORDER_FILTERS = ("branch_ids", "branch_keyword", "order_type_ids",
                  "exclude_returns")

METRICS: dict[str, Metric] = {
    "sales": Metric(
        model=ORDERS,
        date_filter_field="order_datetime",
        date_group_field="order_datetime",
        aggregates=("amount_untaxed", "amount_total", "amount_tax",
                    "discount_amount"),
        forced_domain=(("state", "=", "validate"),),
        allows=_ORDER_GROUPS,
        allows_filters=_ORDER_FILTERS,
        returns_split=True,
        description="Net, gross and returns. Net is amount_untaxed, before VAT.",
    ),
    "returns": Metric(
        model=ORDERS,
        date_filter_field="order_datetime",
        date_group_field="order_datetime",
        aggregates=("amount_untaxed", "amount_total", "amount_tax"),
        forced_domain=(("state", "=", "validate"),),
        allows=_ORDER_GROUPS,
        allows_filters=("branch_ids", "branch_keyword"),
        only_return_types=True,
        description="Return orders only. Separate records, positive amounts.",
    ),
    "discounts": Metric(
        model=ORDERS,
        date_filter_field="order_datetime",
        date_group_field="order_datetime",
        aggregates=("discount_amount", "coupon_amount", "voucher_amount"),
        forced_domain=(("state", "=", "validate"),),
        allows=_ORDER_GROUPS,
        allows_filters=_ORDER_FILTERS,
        description="Discounts, coupons and gift vouchers given.",
    ),
    "unposted_orders": Metric(
        model=ORDERS,
        date_filter_field="order_datetime",
        date_group_field="order_datetime",
        aggregates=("amount_untaxed", "amount_total"),
        forced_domain=(("state", "=", "validate"),
                       ("session_order_link", "=", False)),
        allows=_ORDER_GROUPS,
        allows_filters=_ORDER_FILTERS,
        description="Validated orders never posted to a session. Missed in "
                    "sales posting.",
    ),
    "payments": Metric(
        model=PAYMENTS,
        date_filter_field="order_datetime",
        date_group_field="order_datetime",
        aggregates=("amount",),
        # Payment lines carry no state of their own.
        forced_domain=(("pos_id.state", "=", "validate"),),
        allows=("day", "week", "month", "hour", "hour_of_day", "payment_method",
                "cashbox"),
        allows_filters=("payment_method_keyword", "is_cash"),
        description="Money collected by payment method. Includes VAT.",
    ),
    "products": Metric(
        model=LINES,
        date_filter_field="pos_id.order_datetime",
        date_group_field=None,          # no own date field -> no date grouping
        aggregates=("quantity", "price_subtotal", "amount_untaxed",
                    "price_total", "discount_amount"),
        # Lines with no app_line_id count as products, per Yasir. See
        # known_limits.app_line_id_null.
        forced_domain=(("pos_id.state", "=", "validate"),
                       "|",
                       ("app_line_id", "<", MODIFIER_THRESHOLD),
                       ("app_line_id", "=", False)),
        allows=("product", "accounting_category", "pos_category", "branch",
                "order_type"),
        allows_filters=("branch_ids", "branch_keyword", "product_keyword",
                        "order_type_ids"),
        description="Product lines. Quantity is the main figure.",
    ),
    "modifiers": Metric(
        model=LINES,
        date_filter_field="pos_id.order_datetime",
        date_group_field=None,
        aggregates=("quantity", "price_subtotal", "amount_untaxed",
                    "price_total"),
        forced_domain=(("pos_id.state", "=", "validate"),
                       ("app_line_id", ">=", MODIFIER_THRESHOLD)),
        allows=("product", "accounting_category", "pos_category", "branch",
                "order_type"),
        allows_filters=("branch_ids", "branch_keyword", "product_keyword",
                        "order_type_ids"),
        description="Modifier lines such as an extra shot.",
    ),
}


# ----------------------------------------------------------------- group_by

#: Grains Odoo groups server-side.
SERVER_DATE_GRAINS = ("day", "week", "month", "hour")

#: Grains Odoo cannot do, folded in Python from a server grain.
#: ``hour_of_day`` answers "what is our busiest time of day" — 0 to 23 across
#: every day in the period. Odoo's ``hour`` gives one bucket per calendar date
#: instead, which is a different question.
DERIVED_DATE_GRAINS = {"hour_of_day": "hour"}

DATE_GRAINS = SERVER_DATE_GRAINS + tuple(DERIVED_DATE_GRAINS)

#: Folding hour_of_day needs every hour bucket back before it can collapse them,
#: so a long period would fetch thousands of rows. This caps the raw fetch. The
#: folded result is at most 24 rows, so it never troubles MAX_BUCKETS.
MAX_RAW_HOUR_BUCKETS = 2000

#: key -> {model: stored field}. A key missing for a model is a rejection.
#: Deliberate omissions, both verified against the live database:
#:   * no date grain for LINES     — related-field grouping is unsupported
#:   * no "branch" for PAYMENTS    — pos_id.branch_id is unsupported
GROUP_BY: dict[str, dict[str, str]] = {
    "branch": {ORDERS: "branch_id", LINES: "branch_id"},
    "order_type": {ORDERS: "order_type", LINES: "order_type_id"},
    "customer": {ORDERS: "customer_id"},
    "employee": {ORDERS: "employee_id"},
    "cashbox": {ORDERS: "cashbox_id", PAYMENTS: "cashbox_id"},
    "product": {LINES: "product_id"},
    "accounting_category": {LINES: "category_id"},
    "pos_category": {LINES: "pos_category_id"},
    "payment_method": {PAYMENTS: "payment_method_id"},
}

#: Keys that are too vague to act on. Asking beats guessing: "category" means
#: the accounting category to finance and the screen category to a cashier, and
#: on this data the accounting one is empty on every single line, so a silent
#: choice would return a confident empty answer.
AMBIGUOUS_GROUP_BY = {
    "category": ("accounting_category", "pos_category"),
}


# ------------------------------------------------------------------ filters

@dataclass(frozen=True)
class FilterSpec:
    kind: str            # ids | keyword | bool
    #: Field per model. Missing model -> not available for that metric.
    fields: dict
    #: For keyword filters, which lookup cache resolves it.
    resolves_via: str = ""
    help: str = ""


FILTERS: dict[str, FilterSpec] = {
    "branch_ids": FilterSpec(
        kind="ids",
        fields={ORDERS: "branch_id", LINES: "branch_id"},
        help="Branch ids to restrict to.",
    ),
    "branch_keyword": FilterSpec(
        kind="keyword",
        fields={ORDERS: "branch_id", LINES: "branch_id"},
        resolves_via="branches",
        help="Branch name, matched in English and Arabic, resolved to ids.",
    ),
    "order_type_ids": FilterSpec(
        kind="ids",
        fields={ORDERS: "order_type", LINES: "order_type_id"},
        help="Order type ids to restrict to.",
    ),
    "exclude_returns": FilterSpec(
        kind="bool",
        fields={ORDERS: "order_type", LINES: "order_type_id"},
        help="Drop return orders entirely instead of splitting them out.",
    ),
    "product_keyword": FilterSpec(
        kind="keyword",
        fields={LINES: "product_id"},
        resolves_via="products",
        help="Product name. Resolved to ids by the caller before building.",
    ),
    "payment_method_keyword": FilterSpec(
        kind="keyword",
        fields={PAYMENTS: "payment_method_id"},
        resolves_via="payment_methods",
        help="Payment method name, resolved to ids.",
    ),
    "is_cash": FilterSpec(
        kind="bool",
        fields={PAYMENTS: "payment_method_id"},
        resolves_via="payment_methods",
        help="Cash payments only, from the is_cash flag.",
    ),
}


# ------------------------------------------------------------- lookup cache

#: Words people add that carry no identifying information. Dropped before
#: matching, so "twn branch" and "twn" behave the same.
KEYWORD_STOPWORDS = frozenset({
    "branch", "branches", "store", "shop", "outlet", "location",
    "فرع", "الفرع", "محل", "فروع",
})

_KEEP = re.compile(r"[^0-9a-z؀-ۿ]+")


def normalise(text: str) -> str:
    """Lowercase, and turn every separator into a space.

    ``RUH-TWN-A`` and ``RUH TWN A`` must resolve to the same branch. Codes are
    written with dashes in Odoo and with spaces by people.
    """
    return _KEEP.sub(" ", str(text).lower()).strip()


@dataclass(frozen=True)
class LookupRecord:
    id: int
    name: str
    #: All names seen for this record across languages, for search.
    search_names: tuple = ()
    flags: dict = field(default_factory=dict)

    def matches(self, keyword: str) -> bool:
        """Token matching, not plain substring.

        Plain substring failed the obvious cases: "twn branch" did not match
        ``RUH-TWN-A`` because the phrase is not literally inside the name, and a
        one-letter keyword like "a" matched every branch at once.

        Rules:
          * split both sides on any separator, so dashes and spaces are the same
          * drop filler words like "branch"
          * every remaining keyword token must appear in the name
          * tokens of 2+ characters match as substrings, so "twn" finds
            ``RUH-TWN-A`` and "madinah" finds ``Madinah Branch New``
          * a single-character token must match a whole word, so "a" finds
            ``RUH-TWN-A`` but not ``Madinah Branch``
        """
        tokens = [t for t in normalise(keyword).split()
                  if t not in KEYWORD_STOPWORDS]
        if not tokens:
            return False

        for raw in self.search_names:
            name = normalise(raw)
            words = set(name.split())
            if all((t in words) if len(t) == 1 else (t in name) for t in tokens):
                return True
        return False


@dataclass(frozen=True)
class Lookups:
    """Small reference models, read once at startup and cached.

    Plain data. Loading it is ``rpc.py``'s job, which keeps ``builder.py`` pure
    and lets tests build one by hand.
    """

    branches: tuple = ()
    order_types: tuple = ()
    payment_methods: tuple = ()
    categories: tuple = ()

    # --- flag-derived id sets. An unset flag reads as False, never as unknown,
    # --- because 11 order types and 2 payment methods have NULL flags locally.

    def return_type_ids(self) -> list:
        return [r.id for r in self.order_types if r.flags.get("is_return")]

    def non_return_type_ids(self) -> list:
        return [r.id for r in self.order_types if not r.flags.get("is_return")]

    def aggregator_type_ids(self) -> list:
        return [r.id for r in self.order_types if r.flags.get("is_aggregator")]

    def cash_method_ids(self) -> list:
        return [r.id for r in self.payment_methods if r.flags.get("is_cash")]

    def is_return_type(self, type_id) -> bool:
        """Order types with no id at all count as non-return.

        One validated order locally has order_type NULL. It carries no flag to
        read, so it belongs in gross rather than being dropped — otherwise gross
        stops equalling the sum of the buckets. See known_limits.null_order_type.
        """
        if not type_id:
            return False
        return type_id in set(self.return_type_ids())

    def resolve(self, cache_name: str, keyword: str) -> list:
        records = getattr(self, cache_name, ())
        return [r.id for r in records if r.matches(keyword)]

    def name_of(self, cache_name: str, rec_id: int) -> str:
        for r in getattr(self, cache_name, ()):
            if r.id == rec_id:
                return r.name
        return str(rec_id)


# ------------------------------------------------------------ system prompt

def metric_help() -> str:
    """Compact — every token here is charged on every single call."""
    lines = []
    for key, m in METRICS.items():
        lines.append(f"- {key}: {m.description}")
        lines.append(f"  groups: {', '.join(allowed_group_by(key)) or 'none'}"
                     f" | filters: {', '.join(m.allows_filters) or 'none'}")
    return "\n".join(lines)


def allowed_group_by(metric_key: str) -> list:
    """Keys that are both permitted by the metric and possible on its model."""
    m = METRICS[metric_key]
    out = []
    for key in m.allows:
        if key in DATE_GRAINS:
            if m.date_group_field:
                out.append(key)
        elif m.model in GROUP_BY.get(key, {}):
            out.append(key)
    return out


def group_by_field(key: str, model: str) -> Optional[str]:
    """read_group field for a key, or None when impossible on that model."""
    return GROUP_BY.get(key, {}).get(model)


def build_system_prompt(
    *,
    today: date,
    tz_name: str,
    data_start: Optional[date],
    lookups: Lookups,
    max_buckets: int,
) -> str:
    """Generated from this file, never hand typed."""
    floor = (f"Data starts {data_start}. Nothing before that exists."
             if data_start else
             "No date floor is set. All available history can be queried.")

    def names(records, limit=25):
        """Ids and names only. Duplicated names are collapsed to keep this short
        — several order types and payment methods repeat per branch."""
        seen, shown = set(), []
        for r in records:
            if r.name in seen:
                continue
            seen.add(r.name)
            shown.append(f"{r.id}={r.name}")
            if len(shown) >= limit:
                break
        more = len(records) - len(shown)
        return ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")

    return f"""You answer business questions about POS sales for WAGS.

You do not write queries. You choose a metric and parameters from the fixed
list below and call the query_pos tool. Python runs the real query.

Today is {today}. All dates you give are local time in {tz_name}.
{floor}

METRICS
{metric_help()}

PERIODS
today, yesterday, this_week, last_week, this_month, last_month, last_7_days,
last_30_days, this_year, last_year, all_time, custom (with start_date and/or
end_date as YYYY-MM-DD). A week runs Monday to Sunday.

TIME GROUPINGS
day, week, month give calendar buckets. hour gives one bucket per hour on each
separate date. hour_of_day folds every day together into 0 to 23 — use that one
for "what is our busiest time of day", and keep the period to about a month.

NOT EVERY MESSAGE IS A DATA QUESTION
Do NOT call the tool for greetings, thanks, small talk, or questions about what
you can do. Reply in words instead. Calling the tool for "hi" and answering with
sales figures is wrong and confusing.

  "hi" / "hello" / "salam" / "assalam o alaikum" / "kya haal hai"
      -> greet back, say briefly what you can report, ask what they need.
  "thanks" / "shukriya" / "ok"
      -> acknowledge. No tool call.
  "what can you do?" / "help"
      -> list what you can report, in words. No tool call.

Only call the tool when the person is actually asking for a number.

ASK FOR THE LEAST YOU NEED
This matters as much as picking the right metric. Extra groupings turn a
one-number answer into an unreadable list.

- Send NO group_by unless the question actually asks for a breakdown. Words like
  "by", "per", "each", "which branch", "trend", "compare" ask for one. "How
  much", "how many", "what is", "total" do NOT.
- Send NO filters unless the question names something to filter on. Do not add
  exclude_returns on your own; returns are already reported separately.
- One question, one figure. "What is the average ticket" wants a single number,
  not a number per day. "How much VAT" wants one total, not a total per branch.
- Only group when the user could not answer their own question without it.

Examples:
  "What is the average ticket?"     -> {{"metric":"sales","period":"all_time"}}
  "How much VAT did we collect?"    -> {{"metric":"sales","period":"all_time"}}
  "Average ticket per branch?"      -> same, plus group_by ["branch"]
  "Sales trend this year?"          -> same, plus group_by ["month"]

AMBIGUOUS WORDS — ask, do not guess
"category" on its own is not a valid grouping. It could mean
accounting_category or pos_category, and the two give different answers. Ask
which one is meant, then use that key. Note the accounting category is not
filled in on this data, so pos_category is usually what someone means.

BRANCHES
{names(lookups.branches)}

ORDER TYPES
{names(lookups.order_types)}

PAYMENT METHODS
{names(lookups.payment_methods)}

RULES FOR YOUR ANSWER
- Only validated orders are counted.
- Net sales means the figure before VAT. The total figure includes VAT.
- Delivery, service, bag and tip charges are never counted as sales.
- Returns are separate records with positive amounts. When you report sales you
  must state net, gross and returns together. Never net on its own.
- If a branch name matches more than one branch, each is reported separately.
  Name them, do not merge them into one figure.
- If a query fails, say it failed. Never state a number that did not come back
  from the database.
- At most {max_buckets} groups can be returned. If that is exceeded you will be
  told, and you should ask for a shorter period or fewer groupings.
- Ask one short clarifying question only when a required input is missing or the
  metric is genuinely ambiguous. Otherwise pick a sensible default, answer, and
  mention the alternative at the end.
- Answer in the language of the question, including Roman Urdu.

{known_limits.prompt_block()}
"""


def build_answer_prompt(*, tz_name: str, lookups: Lookups) -> str:
    """Lean prompt for call two.

    Call two only turns a finished result into prose. It cannot query anything,
    so the metric list, group-by keys, filter keys and period names are dead
    weight there — and Groq's free tier allows 100,000 tokens per DAY, with the
    system prompt charged on every call. Sending the full prompt twice per
    question cost about 8,500 tokens and capped the app at roughly 11 questions
    a day. This trims call two to the rules that affect the wording.

    This is a deliberate departure from the spec's "call two gets the same
    system prompt". The rules that shape the answer are all still here.
    """
    return f"""You write the final answer for WAGS Insight, a POS sales
assistant. The figures have already been fetched. Use only what you are given.

All times are {tz_name}.

RULES
- Only validated orders are counted.
- Net sales is before VAT. The total figure includes VAT.
- Delivery, service, bag and tip charges are never counted as sales.
- Returns are separate records with positive amounts. When reporting sales,
  state net, gross and returns together. Never net on its own.
- Report every figure you are given. Do not compute new ones.
- If ok is false, say plainly that it failed and why. Give no figures.
- Repeat any notes as caveats, in your own words.
- Answer in the same language as the question, including Roman Urdu.
- Be brief. No tables unless there are several groups.

{known_limits.prompt_block()}
"""
