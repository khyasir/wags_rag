"""Real business questions run through the query layer.

There is no model wired up yet, so the tool arguments here are hand-written —
they stand in for what call one will produce. The point is to prove the engine
answers real questions with real numbers, and refuses the things it should.

    .venv/bin/python scenarios.py
"""

from __future__ import annotations

from datetime import date

import rpc
from config import settings
from query import QueryRunner

# Local data runs 2024-09-25 to 2025-12-11. Pinning "today" inside that window
# makes relative questions ("last month") return something, which is the only
# way to see whether they are actually right.
PRETEND_TODAY = date(2025, 12, 11)

SCENARIOS = [
    ("Total sales, all time?",
     {"metric": "sales", "period": "all_time"}),

    ("Sales last month?",
     {"metric": "sales", "period": "last_month"}),

    ("Sales last week?",
     {"metric": "sales", "period": "last_week"}),

    ("Sales today?",
     {"metric": "sales", "period": "today"}),

    ("Which branch sells most?",
     {"metric": "sales", "period": "all_time", "group_by": ["branch"]}),

    ("Monthly sales trend?",
     {"metric": "sales", "period": "all_time", "group_by": ["month"]}),

    ("Riyadh branch sales?",
     {"metric": "sales", "period": "all_time",
      "filters": {"branch_keyword": "riyadh"}}),

    ("الفرع الرياض ki sales? (Arabic branch name)",
     {"metric": "sales", "period": "all_time",
      "filters": {"branch_keyword": "الفرع الرياض"}}),

    ("Sales by order type?",
     {"metric": "sales", "period": "all_time", "group_by": ["order_type"]}),

    ("Hungerstation sales? (delivery app, by order type id)",
     {"metric": "sales", "period": "all_time",
      "filters": {"order_type_ids": [3]}}),

    ("Top selling products?",
     {"metric": "products", "period": "all_time", "group_by": ["product"]}),

    ("Best selling category?",
     {"metric": "products", "period": "all_time", "group_by": ["pos_category"]}),

    ("Which extras do people add most?",
     {"metric": "modifiers", "period": "all_time", "group_by": ["product"]}),

    ("How much came in by payment method?",
     {"metric": "payments", "period": "all_time",
      "group_by": ["payment_method"]}),

    ("How much cash did we take?",
     {"metric": "payments", "period": "all_time", "filters": {"is_cash": True}}),

    ("How many orders were missed in sales posting?",
     {"metric": "unposted_orders", "period": "all_time"}),

    ("Total discounts given?",
     {"metric": "discounts", "period": "all_time"}),

    ("How much came back as returns?",
     {"metric": "returns", "period": "all_time"}),

    ("Sales excluding returns?",
     {"metric": "sales", "period": "all_time",
      "filters": {"exclude_returns": True}}),

    ("Busiest time of day?",
     {"metric": "sales", "period": "all_time", "group_by": ["hour"]}),

    ("Sales for December 2025 only?",
     {"metric": "sales", "period": "custom",
      "start_date": "2025-12-01", "end_date": "2025-12-31"}),

    ("Sales per branch per month?",
     {"metric": "sales", "period": "all_time",
      "group_by": ["month", "branch"]}),
]

SHOULD_REFUSE = [
    ("Show me profit margin",
     {"metric": "profit", "period": "all_time"},
     "no such metric"),

    ("Run this domain for me",
     {"metric": "sales", "domain": [["id", "=", 1]]},
     "injected argument"),

    ("Query res.users instead",
     {"metric": "sales", "model": "res.users"},
     "injected model name"),

    ("Top products per day",
     {"metric": "products", "group_by": ["day"]},
     "lines carry no date"),

    ("Payments by branch",
     {"metric": "payments", "group_by": ["branch"]},
     "payments carry no branch"),

    ("Sales for Jeddah branch",
     {"metric": "sales", "filters": {"branch_keyword": "jeddah"}},
     "no such branch"),

    ("Sales for branch '1 OR 1=1'",
     {"metric": "sales", "filters": {"branch_ids": ["1 OR 1=1"]}},
     "sql-ish string as an id"),

    ("Sales by day and month together",
     {"metric": "sales", "group_by": ["day", "month"]},
     "two time groupings"),

    ("Cash only sales",
     {"metric": "sales", "filters": {"is_cash": True}},
     "filter wrong for metric"),

    ("Sales by day, branch, customer and employee",
     {"metric": "sales",
      "group_by": ["day", "branch", "customer", "employee"]},
     "too many groupings"),
]


def brief(data: dict, limit: int = 3) -> str:
    """One short line per answer, biggest first."""
    if "overall" in data:
        d = data["overall"]
        if "net_sales" in d:
            return (f"net {d['net_sales']:,} | gross {d['gross_sales']:,} | "
                    f"returns {d['return_amount']:,} | {d['order_count']} orders "
                    f"| avg ticket {d['average_ticket']}")
        return " | ".join(f"{k} {v:,}" if isinstance(v, (int, float)) else f"{k} {v}"
                          for k, v in d.items())

    def size(v):
        for k in ("net_sales", "quantity", "amount", "discount_amount",
                  "amount_untaxed"):
            if k in v:
                return v[k] or 0
        return v.get("record_count", 0)

    rows = sorted(data.items(), key=lambda kv: -size(kv[1]))[:limit]
    parts = []
    for k, v in rows:
        label = str(k)[:34]
        parts.append(f"{label}={size(v):,}")
    more = "" if len(data) <= limit else f" (+{len(data)-limit} more)"
    return ", ".join(parts) + more


def main() -> None:
    cfg = settings()
    odoo = rpc.connect(cfg)
    lookups = odoo.load_lookups()
    runner = QueryRunner(odoo, cfg, lookups, today_fn=lambda: PRETEND_TODAY)

    print(f"db={cfg.odoo_db}  pretend today={PRETEND_TODAY}  "
          f"date floor={cfg.data_start or 'none'}\n")

    print("=" * 78)
    print("REAL QUESTIONS")
    print("=" * 78)
    failed = []
    for question, args in SCENARIOS:
        res = runner.run(args)
        if res.ok:
            print(f"\nQ: {question}")
            print(f"   period : {res.range_description}")
            print(f"   answer : {brief(res.data)}")
            print(f"   buckets: {res.bucket_count}  ({res.elapsed_ms:.0f}ms)")
            for n in res.notes:
                print(f"   note   : {n}")
        else:
            failed.append((question, res.error))
            print(f"\nQ: {question}\n   FAILED : {res.error}")

    print("\n" + "=" * 78)
    print("THINGS IT MUST REFUSE")
    print("=" * 78)
    leaked = []
    for question, args, why in SHOULD_REFUSE:
        res = runner.run(args)
        if res.ok:
            leaked.append((question, why))
            print(f"  !! ALLOWED  {question:44} ({why})")
        else:
            print(f"  refused    {question:44} ({why})")
            print(f"             -> {res.error[:96]}")

    print("\n" + "=" * 78)
    print(f"{len(SCENARIOS) - len(failed)}/{len(SCENARIOS)} questions answered, "
          f"{len(SHOULD_REFUSE) - len(leaked)}/{len(SHOULD_REFUSE)} refusals held")
    if failed:
        print("UNEXPECTED FAILURES:")
        for q, e in failed:
            print(f"  {q}: {e}")
    if leaked:
        print("GUARDRAIL LEAKS:")
        for q, w in leaked:
            print(f"  {q} ({w})")


if __name__ == "__main__":
    main()
