"""FIELD REGISTRY — the labeled fields the app is allowed to touch.

This is the machine-readable copy of ``pos_rag_golden_pair/WAGS_Insight_fields_final_1.xlsx``.
The Excel stays the human reference; edit it there, then mirror the change here.

Every field carries a ROLE, taken from the colour in the Excel:

    forced      orange  the code always applies it; the AI can never change it.
                        (e.g. state = validate, and the 2026+ date window)
    special     orange  a forced filter used only for a specific question type
                        (e.g. app_line_id splits product vs modifier).
    dimension   green   the AI may filter by it and/or group by it.
    measure     blue    a number the AI may total (sum).

Fields with no colour in the Excel are intentionally left out — if a field is
not here, the app must not use it.

Each field also records whether it has a DB index (``indexed`` / ``index``),
taken from the Excel's "Indexed" and "In indexes" columns. Filtering on an
indexed field is fast; filtering on an unindexed one scans more rows. Nothing on
pos.wags.payment.method is indexed (its pos_id has no index yet), which is why
payment questions are slower.

Three models:
    pos.wags                  order header  — one row per order
    pos.wags.tree             order lines   — one row per product/modifier line
    pos.wags.payment.method   payment lines — one row per payment on an order
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ------------------------------------------------------------------- roles

FORCED = "forced"        # always applied by code
SPECIAL = "special"      # forced, but only for certain question types
DIMENSION = "dimension"  # AI may filter and/or group
MEASURE = "measure"      # AI may total (sum)

ORDERS = "pos.wags"
LINES = "pos.wags.tree"
PAYMENTS = "pos.wags.payment.method"

#: The date floor is set by the DATA_START env var (see odoo_db.py), blank in
#: dev and 2026-01-01 in production. Nothing before it is answered.


@dataclass(frozen=True)
class Field:
    name: str                       # the real Odoo field name
    label: str                      # plain-English label for the user
    role: str                       # one of the role constants above
    description: str = ""           # context, straight from the Excel
    relation: Optional[str] = None  # target model for Many2one fields
    indexed: bool = False           # has a DB index (fast to filter on)
    index: str = ""                 # the index name(s), from the Excel


@dataclass(frozen=True)
class Model:
    name: str
    label: str
    about: str
    fields: tuple

    def by_role(self, role: str) -> list:
        return [f for f in self.fields if f.role == role]

    def get(self, name: str) -> Optional[Field]:
        return next((f for f in self.fields if f.name == name), None)

    @property
    def dimensions(self) -> list:
        return self.by_role(DIMENSION)

    @property
    def measures(self) -> list:
        return self.by_role(MEASURE)


# ------------------------------------------------------------ pos.wags

POS_WAGS = Model(
    ORDERS, "Order header",
    "One row per order. Only state = validate counts as a real sale.",
    (
        Field("state", "Order state", FORCED,
              "Draft, KOT, validate, cancelled. Only validate counts as a real "
              "order.", "selection", indexed=True,
              index="idx_pw_branch_state[pos2], idx_pos_wags_branch_date_state[pos3]"),
        Field("order_datetime", "Order datetime", DIMENSION,
              "Exact date and time of the order in UTC. The only date field used "
              "for filtering and grouping.", "datetime", indexed=True,
              index="idx_pos_wags_branch_date_state[pos2], idx_pos_wags_order_datetime[pos1]"),
        Field("session_order_link", "Sales posting link", SPECIAL,
              "Filled when the order is posted into a sales session. Empty means "
              "not yet posted — used to find missed postings.", "pos.session.wags",
              indexed=True, index="idx_pos_wags_session_order_link[pos1]"),
        Field("branch_id", "Branch", DIMENSION,
              "The branch the sale belongs to.", "branch.wags", indexed=True,
              index="idx_pw_branch_state[pos1], idx_pos_wags_branch_date_state[pos1]"),
        Field("order_type", "Order type", DIMENSION,
              "Regular Order, Delivery App, Pickup, Return, etc.", "order.type.wags"),
        Field("customer_id", "Customer", DIMENSION,
              "Customer on the order. Walk-In when not identified.", "res.partner"),
        Field("employee_id", "Employee", DIMENSION,
              "Employee linked to the order.", "hr.employee.wags"),
        Field("company_id", "Company", DIMENSION,
              "Company of the order. Matters only with more than one company.",
              "res.company"),
        Field("cashbox_id", "Cash box", DIMENSION,
              "Cash box used at the till.", "cash.box"),
        Field("zatca_hash_reported_invoice", "Reported to ZATCA", DIMENSION,
              "Whether the invoice was reported to the tax authority.", "boolean",
              indexed=True, index="idx_pw_zatca_pending[partial]"),
        Field("transaction_id", "Transaction", DIMENSION,
              "Unique reference shared by the POS app and WAGS ERP; used to find "
              "a single order.", "char"),
        Field("reference", "Order reference", DIMENSION,
              "Human-readable order number, e.g. POS-5525156. Used to look up a "
              "single order.", "char", indexed=True,
              index="idx_pw_zatca_pending[partial]"),
        Field("amount_untaxed", "Net sales", MEASURE,
              "Sale value before VAT. The sales figure finance uses."),
        Field("amount_tax", "VAT amount", MEASURE, "VAT charged on the order."),
        Field("amount_total", "Total with VAT", MEASURE,
              "Net sales plus VAT plus charges. Use only when the total with VAT "
              "is asked for."),
        Field("discount_amount", "Discount amount", MEASURE,
              "Discount value given on the order."),
        Field("coupon_amount", "Coupon amount", MEASURE,
              "Value covered by a coupon."),
        Field("voucher_amount", "Voucher amount", MEASURE,
              "Value covered by a gift voucher."),
    ),
)


# ------------------------------------------------------- pos.wags.tree

POS_WAGS_TREE = Model(
    LINES, "Order lines",
    "One row per product or modifier line. app_line_id splits the two.",
    (
        Field("pos_id", "Order", DIMENSION,
              "Link back to the pos.wags order. state lives on pos_id.state.",
              "pos.wags", indexed=True, index="pos_wags_tree_pos_id_index[pos1]"),
        Field("app_line_id", "App line id", SPECIAL,
              "< 100000 means the line is a product; >= 100000 means it is a "
              "modifier (e.g. extra shot)."),
        Field("product_id", "Product", DIMENSION,
              "Product sold on this line.", "product.product.wags",
              indexed=True, index="idx_pwt_product[pos1]"),
        Field("pos_category_id", "POS category", DIMENSION,
              "Product category.", "pos.product.category.wags"),
        Field("order_type_id", "Order type", DIMENSION,
              "Regular Order, Delivery App, Pickup, Return, etc.", "order.type.wags"),
        Field("branch_id", "Branch", DIMENSION,
              "The branch the line belongs to.", "branch.wags"),
        Field("quantity", "Quantity", MEASURE, "Units sold on this line."),
        Field("price_unit", "Unit price", MEASURE, "Price of one unit."),
        Field("price_subtotal", "Line subtotal", MEASURE,
              "Unit price x quantity, before VAT."),
        Field("price_total", "Line total", MEASURE, "Line value including VAT."),
        Field("taxes", "Line VAT", MEASURE, "Tax amount on this line."),
        Field("discount_amount", "Discount amount", MEASURE,
              "Discount on this line."),
    ),
)


# --------------------------------------------- pos.wags.payment.method

POS_WAGS_PAYMENT = Model(
    PAYMENTS, "Payment lines",
    "One row per payment on an order. amount includes VAT. pos_id has no index "
    "yet, so payment questions are slow.",
    (
        Field("pos_id", "Order", DIMENSION,
              "Link back to the pos.wags order. state lives on pos_id.state.",
              "pos.wags"),
        Field("order_datetime", "Order datetime", DIMENSION,
              "Date and time of the order in UTC, copied to the payment.",
              "datetime"),
        Field("payment_method_id", "Payment method", DIMENSION,
              "Cash, card, voucher, and so on.", "pos.payment.method.wags"),
        Field("cashbox_id", "Cash box", DIMENSION,
              "Cash box that received the money.", "cash.box"),
        Field("amount", "Payment amount", MEASURE,
              "Money collected on this payment line, including VAT."),
    ),
)


MODELS: dict = {m.name: m for m in (POS_WAGS, POS_WAGS_TREE, POS_WAGS_PAYMENT)}


# ------------------------------------------------------- forced filters
#
# ALWAYS: applied to every query on that model, no matter what.
# SPECIAL: named filters the code adds only for a specific question type. The
#          value is the Odoo domain fragment to append.

def state_field(model: str) -> str:
    """Line and payment models reach the order state through pos_id."""
    return "state" if model == ORDERS else "pos_id.state"


def date_field(model: str) -> str:
    """Which datetime column a date range filters on, per model."""
    return "pos_id.order_datetime" if model == LINES else "order_datetime"


#: Applied to every query. state must be validate.
def always_forced(model: str) -> list:
    return [(state_field(model), "=", "validate")]


#: Only added when a specific question type needs it.
SPECIAL_FILTERS = {
    "products": ["|", ("app_line_id", "<", 100000), ("app_line_id", "=", False)],
    "modifiers": [("app_line_id", ">=", 100000)],
    "unposted_orders": [("session_order_link", "=", False)],
}
