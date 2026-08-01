"""Known gaps in the query layer.

Single source of truth for "we cannot answer this properly yet".

Two jobs:

1. Human record. Each entry says what the open question is, why it matters, and
   what the code does in the meantime. Answer one, delete the entry, fix the code.
2. Machine input. ``catalog.build_system_prompt()`` injects every entry whose
   ``status`` is ``"open"`` into the system prompt. The model must then warn the
   user instead of presenting the number as final. It must never silently
   answer a question that lands on an open limit.

Rules for adding an entry:

* ``warn`` is user-facing text. Plain words, no jargon, no field names.
  The model repeats it more or less verbatim, so write it the way you want the
  user to hear it.
* ``mvp`` is what the code actually does today. Be exact. This is the line that
  stops a future engineer from assuming the gap was handled.
* ``triggers`` are the question shapes that should fire the warning. Used by the
  model to spot the case, and by tests to assert the warning fired.
* Never let an entry sit ``open`` with no ``mvp``. If we have no behaviour, the
  metric should refuse, not guess.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Limit:
    id: str
    question: str          # the open question, for us
    why: str               # what breaks if we guess wrong
    mvp: str               # what the code does right now
    warn: str              # user-facing caveat the model must say
    triggers: tuple        # question shapes that should fire the warning
    status: str = "open"   # open | answered | wont_fix
    answer: str = ""       # fill in when status becomes answered


LIMITS = [

    # ---------------------------------------------------------------- money in
    Limit(
        id="aggregator_commission",
        question=(
            "For delivery apps (Hungerstation, Jahez, The Chefz, Keeta), does "
            "'sales' mean what the customer paid, or what WAGS actually "
            "receives after the app takes its commission?"
        ),
        why=(
            "These two numbers are far apart. Commission is typically a large "
            "cut. If the owner reads the customer-paid figure as revenue, "
            "delivery looks much more profitable than it is."
        ),
        mvp=(
            "amount_untaxed is reported, which is what the customer paid. "
            "No commission is deducted. No commission field exists in the "
            "exposed field list, so there is nothing to deduct from yet."
        ),
        warn=(
            "This is the amount customers paid on the delivery app. The app's "
            "commission is not taken off, so the money actually received is "
            "lower than this."
        ),
        triggers=(
            "hungerstation sales", "jahez sales", "keeta sales", "chefz sales",
            "delivery app revenue", "aggregator sales", "how much did we make "
            "from delivery",
        ),
    ),

    Limit(
        id="charges_not_in_sales",
        question=(
            "Delivery charge, service charge, bag fee and tip are excluded "
            "from sales by rule. Where are they stored, and does anyone need "
            "to report on them?"
        ),
        why=(
            "amount_total minus amount_untaxed minus amount_tax will not come "
            "to zero when charges exist. If someone reconciles those three "
            "numbers they will find a gap and assume our figures are broken."
        ),
        mvp=(
            "Charges are never added into sales. amount_untaxed is net, "
            "amount_total is with VAT and charges. The gap between them is not "
            "explained or broken down."
        ),
        warn=(
            "Delivery, service, bag and tip charges are not counted as sales. "
            "The total with VAT may be higher than net plus VAT for that reason."
        ),
        triggers=(
            "delivery charge", "service charge", "bag fee", "tip", "tips",
            "why total not matching", "difference between net and total",
        ),
    ),

    Limit(
        id="finance_date_basis",
        question=(
            "Finance closes the month on effective_date, but every query here "
            "uses order_datetime. Which one does month-end actually use?"
        ),
        why=(
            "An order placed late on the last day of the month can carry a "
            "different effective_date. Our monthly total would then not match "
            "the finance monthly total, and nobody would know which is right."
        ),
        mvp=(
            "order_datetime only, as the task specifies. effective_date is "
            "exposed but never filtered or grouped on."
        ),
        warn=(
            "This is counted by the time the order was placed. Accounting may "
            "close the month on a slightly different date, so month-end totals "
            "can differ a little."
        ),
        triggers=(
            "month end", "closing", "does this match accounting",
            "finance report", "match the books",
        ),
    ),

    # ------------------------------------------------------------- data shape
    Limit(
        id="app_line_id_null",
        question=(
            "300 of 879 order lines (34%) have no app_line_id. Are these real "
            "product lines, or something else?"
        ),
        why=(
            "The rule is below 100000 is a product, 100000 and above is a "
            "modifier. Rows with nothing set match neither, so a third of all "
            "lines would vanish from every product report without a word."
        ),
        mvp=(
            "Lines with no app_line_id are counted as PRODUCT lines, per "
            "Yasir's decision. Nothing is dropped. Every products result also "
            "reports how many lines had no app_line_id."
        ),
        warn=(
            "Some order lines do not say whether they are a product or an "
            "extra. They are counted as products here."
        ),
        triggers=("top products", "best selling", "product sales", "modifiers",
                  "extras", "quantity sold"),
    ),

    Limit(
        id="null_order_type",
        question="Why does one validated order have no order type at all?",
        why=(
            "Return orders are identified by a flag on the order type. An order "
            "with no type has no flag to read, so it can be neither confirmed "
            "sale nor confirmed return."
        ),
        mvp=(
            "Orders with no order type are counted as NON-return, so they land "
            "in gross sales. 1 such order locally, 8.26 untaxed. Dropping them "
            "instead would make gross stop equalling the sum of the buckets."
        ),
        warn="",  # too small to bother the user with; kept for engineers
        triggers=(),
    ),

    Limit(
        id="duplicate_lookup_rows",
        question=(
            "order.type.wags has 11 rows that look like duplicates (six named "
            "'Drive Through', five named 'Hungerstation') with every flag "
            "empty. pos.payment.method.wags has similar GCCNET duplicates. Are "
            "these dead rows, or per-branch copies that will collect orders?"
        ),
        why=(
            "Right now they hold no orders so nothing is wrong. The moment they "
            "do, 'Drive Through' will split into six separate lines in any "
            "order-type breakdown and the answer will look nonsensical."
        ),
        mvp=(
            "No merging by name. Each id stays its own bucket. Empty is_return "
            "and is_aggregator are read as false, never as unknown."
        ),
        warn=(
            "The same name can appear more than once because it is set up "
            "separately per branch."
        ),
        triggers=("by order type", "order type breakdown", "payment method "
                  "breakdown", "why same name twice"),
    ),

    Limit(
        id="payment_method_aggregator_flag",
        question=(
            "is_aggregator on pos.payment.method.wags is empty on all 11 rows. "
            "Is it meant to be filled in?"
        ),
        why=(
            "If it were populated we could answer delivery questions from the "
            "payment side too, and cross-check it against the order-type side. "
            "Right now the two cannot be reconciled."
        ),
        mvp=(
            "aggregator_sales uses the order.type.wags is_aggregator flag only. "
            "The payment-method flag is ignored."
        ),
        warn="",
        triggers=(),
    ),

    Limit(
        id="accounting_category_empty",
        question=(
            "category_id is NULL on 879 of 879 line rows — 100%. The accounting "
            "category is never filled in. pos_category_id is set on 497 of 879 "
            "(43% empty). Should 'category' mean the POS category instead?"
        ),
        why=(
            "'category' was set to mean the accounting category. On this data "
            "that grouping returns exactly one bucket, 'Not set', every single "
            "time. The answer looks like a working report and carries no "
            "information at all."
        ),
        mvp=(
            "group_by 'category' still maps to category_id as decided, so it "
            "returns one empty bucket. group_by 'pos_category' works and is the "
            "only category grouping that returns anything."
        ),
        warn=(
            "The accounting category is not filled in on any order line, so "
            "grouping by it gives nothing. Ask me to group by the POS screen "
            "category instead."
        ),
        triggers=("by category", "which category", "category sales",
                  "best selling category", "category breakdown"),
    ),

    Limit(
        id="branch_names_contradict_across_languages",
        question=(
            "Branch 2 is 'Madinah Branch' in English and 'الفرع الرياض' "
            "(Riyadh Branch) in Arabic. Branch 6 is 'Madinah Branch New' in "
            "English and 'الفرع الرئيسي الرياض' (Riyadh Main Branch) in Arabic. "
            "A separate branch 5 is named 'Riyadh' and has no orders. Which "
            "names are correct?"
        ),
        why=(
            "Asking 'Riyadh sales' in English resolves to branch 5 and answers "
            "zero. Asking the same thing in Arabic resolves to branch 2 and "
            "answers 130.43. Both are faithful to the data and one of them is "
            "going to be read as a bug in our app rather than in the branch "
            "setup."
        ),
        mvp=(
            "Keyword search matches whatever name is stored, in both languages, "
            "and always reports which branch it matched by name."
        ),
        warn=(
            "Some branches have an English name and an Arabic name that do not "
            "agree with each other. I will tell you exactly which branch I "
            "matched — check it is the one you meant."
        ),
        triggers=("riyadh", "madinah", "الرياض", "المدينة", "branch sales",
                  "which branch"),
    ),

    Limit(
        id="hour_grouping_is_not_hour_of_day",
        question=(
            "Should 'hour' mean a specific hour on a specific date, or the hour "
            "of the day averaged across the whole period?"
        ),
        why=(
            "'What is our busiest time of day' expects 0 to 23 folded across "
            "every day. Odoo groups hour per calendar date instead, so the "
            "answer comes back as '2025-11-22 03:00' — the single busiest hour "
            "that ever happened, which is a different question."
        ),
        mvp=(
            "group_by 'hour' returns one bucket per date-and-hour, labelled in "
            "Riyadh time. There is no hour-of-day grouping. Odoo cannot do it "
            "server-side, and folding it in Python needs every hour bucket "
            "first, which a long period would push past the bucket cap."
        ),
        warn=(
            "Grouping by hour gives each hour on each day separately, not an "
            "average time of day. For a true busiest-hour-of-day I would need a "
            "short period, a week or so."
        ),
        triggers=("busiest time", "busiest hour", "peak hour", "time of day",
                  "what time", "rush hour", "by hour"),
    ),

    Limit(
        id="lines_without_a_product",
        question="Why do 27 order lines have no product on them?",
        why=(
            "They show up in product reports as a bucket called 'Not set', "
            "which looks like a bug in the report rather than a gap in the data."
        ),
        mvp=(
            "Lines with no product are kept and grouped under 'Not set'. "
            "Dropping them would make the line totals stop adding up."
        ),
        warn=(
            "A few order lines have no product recorded. They are grouped under "
            "'Not set'."
        ),
        triggers=("top products", "product breakdown", "what is not set"),
    ),

    Limit(
        id="walkin_customer",
        question=(
            "Unidentified customers are described as 'Walk-In'. Is that one "
            "shared customer record, or an empty customer field?"
        ),
        why=(
            "Grouping by customer is allowed. If Walk-In is one record it will "
            "dominate every customer report and make the real named customers "
            "invisible below it."
        ),
        mvp=(
            "Grouping by customer returns whatever Odoo returns, Walk-In "
            "included, unfiltered. Not yet checked against the data."
        ),
        warn=(
            "Customers who were not identified at the till are grouped "
            "together, so that group will be much larger than the rest."
        ),
        triggers=("by customer", "top customers", "repeat customers",
                  "customer breakdown"),
    ),

    # ------------------------------------------------------------ performance
    Limit(
        id="missing_indexes_production",
        question=(
            "pos.wags.payment.method has only a primary key. No index on "
            "pos_id, order_datetime or payment_method_id. pos.wags.tree has no "
            "index on branch_id, category_id, pos_category_id or app_line_id. "
            "Can these be added in production?"
        ),
        why=(
            "At 12 million payment rows and 26 million line rows, every "
            "payments question and every line-level question by branch or "
            "category becomes a full table scan. Local timings hide this "
            "completely because the local tables have under 900 rows."
        ),
        mvp=(
            "Queries run as-is. No index is created by this project. Stage two "
            "reports EXPLAIN plans and a recommended index list for review."
        ),
        warn="",
        triggers=(),
    ),

    Limit(
        id="local_data_not_representative",
        question=(
            "The local database has 318 orders, all timestamped exactly "
            "00:00:00, spanning 2024-09 to 2025-12, with zero return orders. "
            "Is there a database with realistic data to test against?"
        ),
        why=(
            "Hour-of-day grouping cannot be observed when every order is at "
            "midnight. Returns arithmetic cannot be observed with zero returns. "
            "And 318 rows tell us nothing about speed at 12 million."
        ),
        mvp=(
            "Timezone bucketing is proven instead from the __range values "
            "read_group returns, which is exact. Returns arithmetic is proven "
            "by unit tests on synthetic buckets. Speed is reported from EXPLAIN "
            "plans rather than from wall-clock milliseconds."
        ),
        warn="",
        triggers=(),
    ),

    # -------------------------------------------------------------- reporting
    Limit(
        id="return_reason_unused",
        question=(
            "return_reason_id is exposed on the order but no metric groups by "
            "it. Should 'why are customers returning things' be answerable?"
        ),
        why=(
            "It is the obvious follow-up once someone sees the return total, "
            "and today the app cannot answer it."
        ),
        mvp="No return_reason group-by key exists. The question cannot be asked.",
        warn=(
            "I can give you return totals, but not yet a breakdown of the "
            "reasons for the returns."
        ),
        triggers=("why returns", "return reason", "reason for return",
                  "returns breakdown by reason"),
    ),

    Limit(
        id="token_cost_rate",
        question="What USD rate should cost_usd be calculated at for Groq?",
        why=(
            "cost_usd is a column in chat_messages. Filled with a guessed rate "
            "it becomes a number that looks authoritative and is wrong."
        ),
        mvp=(
            "Token counts are stored exactly as Groq reports them. cost_usd is "
            "left NULL until a rate is confirmed."
        ),
        warn="",
        triggers=(),
    ),
]


# --------------------------------------------------------------------- helpers

def open_limits():
    """Entries still unresolved."""
    return [l for l in LIMITS if l.status == "open"]


def user_facing():
    """Open entries that have something to say to the user.

    Entries with an empty ``warn`` are engineer-only notes and are kept out of
    the system prompt so it stays short.
    """
    return [l for l in open_limits() if l.warn]


def prompt_block(include_triggers: bool = False):
    """Render the caveats for injection into the system prompt.

    ``include_triggers`` is off by default. Listing the trigger phrases roughly
    doubles the size of this block, and Groq's free tier allows only 100,000
    tokens per DAY — the system prompt is sent on every call, so wasted tokens
    here directly reduce how many questions can be answered. The warnings alone
    are enough for the model to spot the case.
    """
    lines = [
        "KNOWN LIMITS. If the question touches one of these, give the number "
        "and then state the caveat in the user's own language. Never present "
        "such a number as final and never invent a correction for it.",
        "",
    ]
    for l in user_facing():
        lines.append(f"- {l.warn}")
        if include_triggers and l.triggers:
            lines.append(f"  (applies when asked about: {', '.join(l.triggers)})")
    return "\n".join(lines)
