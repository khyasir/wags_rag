"""Stage two report: run every metric against the live database, print timings.

Kept in the repo so the numbers can be reproduced after any change:

    .venv/bin/python stage2_report.py
"""

from __future__ import annotations

import time
from datetime import date

import catalog
import rpc
from config import settings
from query import QueryRunner

SLOW_MS = 5000


def main() -> None:
    cfg = settings()
    print(f"db={cfg.odoo_db} url={cfg.odoo_url} tz={cfg.tz_name}")
    print(f"data_start={cfg.data_start or 'NONE (no floor, full history)'} "
          f"branch_scope={cfg.branch_ids or 'all'} max_buckets={cfg.max_buckets}")

    t = time.time()
    odoo = rpc.connect(cfg)
    print(f"connected uid={odoo.uid} in {(time.time()-t)*1000:.0f}ms")

    t = time.time()
    lookups = odoo.load_lookups()
    print(f"lookups loaded in {(time.time()-t)*1000:.0f}ms — "
          f"{len(lookups.branches)} branches, {len(lookups.order_types)} order "
          f"types ({len(lookups.return_type_ids())} flagged return), "
          f"{len(lookups.payment_methods)} payment methods "
          f"({len(lookups.cash_method_ids())} cash), "
          f"{len(lookups.categories)} categories")

    runner = QueryRunner(odoo, cfg, lookups)

    # Row counts, so bucket counts can be read in context.
    print("\n-- table sizes (validated only) --")
    for model, dom in (
        ("pos.wags", [("state", "=", "validate")]),
        ("pos.wags.tree", [("pos_id.state", "=", "validate")]),
        ("pos.wags.payment.method", [("pos_id.state", "=", "validate")]),
    ):
        t = time.time()
        n = odoo.search_count(model, dom)
        print(f"  {model:26} {n:>10,}  {(time.time()-t)*1000:.0f}ms")

    cases = [
        ("sales", {"metric": "sales", "period": "all_time"}),
        ("sales by month", {"metric": "sales", "period": "all_time",
                            "group_by": ["month"]}),
        ("sales by branch", {"metric": "sales", "period": "all_time",
                             "group_by": ["branch"]}),
        ("sales by hour", {"metric": "sales", "period": "all_time",
                           "group_by": ["hour"]}),
        ("returns", {"metric": "returns", "period": "all_time"}),
        ("discounts", {"metric": "discounts", "period": "all_time"}),
        ("unposted_orders", {"metric": "unposted_orders", "period": "all_time"}),
        ("payments", {"metric": "payments", "period": "all_time",
                      "group_by": ["payment_method"]}),
        ("payments cash only", {"metric": "payments", "period": "all_time",
                                "filters": {"is_cash": True}}),
        ("products", {"metric": "products", "period": "all_time",
                      "group_by": ["product"]}),
        ("products by category", {"metric": "products", "period": "all_time",
                                  "group_by": ["category"]}),
        ("modifiers", {"metric": "modifiers", "period": "all_time",
                       "group_by": ["product"]}),
    ]

    print(f"\n-- metrics ({len(cases)} calls) --")
    slow = []
    for label, args in cases:
        res = runner.run(args)
        flag = ""
        if res.elapsed_ms > SLOW_MS:
            flag = "  <-- SLOW"
            slow.append((label, res.elapsed_ms))
        if res.ok:
            print(f"  {label:24} ok   buckets={res.bucket_count:<5} "
                  f"{res.elapsed_ms:7.0f}ms{flag}")
        else:
            print(f"  {label:24} FAIL {res.error[:80]}")

    print("\n-- sales figures, full history --")
    res = runner.run({"metric": "sales", "period": "all_time"})
    if res.ok:
        for k, v in res.data["overall"].items():
            print(f"  {k:22} {v}")
        for n in res.notes:
            print(f"  note: {n}")

    print("\n-- app_line_id split on the line table --")
    for label, dom in (
        ("total validated lines", [("pos_id.state", "=", "validate")]),
        ("product (<100000)", [("pos_id.state", "=", "validate"),
                               ("app_line_id", "<", 100000)]),
        ("modifier (>=100000)", [("pos_id.state", "=", "validate"),
                                 ("app_line_id", ">=", 100000)]),
        ("NOT SET", [("pos_id.state", "=", "validate"),
                     ("app_line_id", "=", False)]),
    ):
        print(f"  {label:24} {odoo.search_count('pos.wags.tree', dom):>10,}")

    print("\n-- guardrails against the live database --")
    for label, args in (
        ("unknown metric", {"metric": "profit"}),
        ("raw domain injected", {"metric": "sales", "domain": [["id", "=", 1]]}),
        ("products by day", {"metric": "products", "group_by": ["day"]}),
        ("payments by branch", {"metric": "payments", "group_by": ["branch"]}),
        ("bad branch keyword", {"metric": "sales",
                                "filters": {"branch_keyword": "jeddah"}}),
    ):
        res = runner.run(args)
        state = "REJECTED" if not res.ok else "!! ALLOWED !!"
        print(f"  {label:24} {state}  {res.error[:70]}")

    if slow:
        print(f"\nSLOW (> {SLOW_MS}ms): " +
              ", ".join(f"{l} {ms:.0f}ms" for l, ms in slow))
    else:
        print(f"\nNothing slower than {SLOW_MS}ms.")


if __name__ == "__main__":
    main()
