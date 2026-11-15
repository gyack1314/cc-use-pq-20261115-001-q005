from collections import Counter, defaultdict

from ..values import safe_ratio


def source_reliability(period):
    by_source = defaultdict(list)
    for cell in period["coverage"]:
        by_source[cell["source"]].append(cell)
    orders = defaultdict(list)
    for order in period["orders"]:
        orders[order["source"]].append(order)
    result = []
    for source, cells in sorted(by_source.items()):
        counts = Counter(cell["status"] for cell in cells)
        reports = Counter(cell.get("report_status", cell["status"]) for cell in cells)
        applicability = Counter(cell.get("applicability", "applicable") for cell in cells)
        applicable = [cell for cell in cells if cell.get("applicability", "applicable") == "applicable"]
        records = orders[source]
        revised = [order for order in records if order["sequence"] > 1]
        active = [order for order in records if order["state"] == "booked"]
        cancellations = [order for order in records if order["state"] == "cancelled"]
        net = sum(order["gross_cents"] - order["refund_cents"] for order in records)
        result.append({"source": source, "cells": len(cells), "expected_cells": len(applicable),
                       "complete_cells": counts["complete"], "partial_cells": counts["partial"],
                       "missing_cells": counts["missing"],
                       "not_applicable_cells": applicability["not_applicable"],
                       "unconfirmed_cells": applicability["unconfirmed"],
                       "ambiguous_cells": applicability["ambiguous"],
                       "report_cells": dict(reports),
                       "coverage_fraction": safe_ratio(counts["complete"], len(applicable)),
                       "order_heads": len(records), "active_orders": len(active),
                       "cancelled_orders": len(cancellations), "revised_heads": len(revised),
                       "revised_fraction": safe_ratio(len(revised), len(records)), "net_cents": net})
    return result


def scope_review(period):
    """What was undeclared, ambiguous, or contradicted by observed orders."""
    if not period.get("scope_disputes") and "scope" not in period:
        return None
    return {"disputes": period.get("scope_disputes", []),
            "gaps": period.get("scope_gaps", []),
            "declarations": [{"id": rule["id"], "version": rule["version"], "source": rule["source"],
                              "city": rule["city"], "start": rule["start"], "end": rule["end"],
                              "applicable": rule["applicable"], "status": rule["status"]}
                             for rule in (period.get("scope") or {}).get("rules", [])]}


def missing_intervals(period):
    groups = defaultdict(list)
    for row in period["coverage"]:
        groups[(row["source"], row["city"])].append(row)
    result = []
    for key, rows in sorted(groups.items()):
        current = []
        runs = []
        for row in sorted(rows, key=lambda item: item["day"]):
            status = row.get("report_status", row["status"])
            if status in {"observed", "confirmed_zero", "not_applicable"} or status == "complete":
                if current:
                    runs.append(current)
                    current = []
            else:
                current.append(row)
        if current:
            runs.append(current)
        for run in runs:
            result.append({"source": key[0], "city": key[1], "start": run[0]["day"],
                           "end": run[-1]["day"], "days": len(run),
                           "statuses": dict(Counter(row.get("report_status", row["status"]) for row in run))})
    return result


def revision_exposure(period):
    counts = Counter(order["sequence"] for order in period["orders"])
    groups = defaultdict(lambda: {"heads": 0, "revised_heads": 0, "latest_ingested_revision": 0})
    for order in period["orders"]:
        item = groups[order["origin"]]
        item["heads"] += 1
        item["revised_heads"] += int(order["sequence"] > 1)
        item["latest_ingested_revision"] = max(item["latest_ingested_revision"], order["ingested_revision"])
    return {"sequence_histogram": [{"sequence": key, "heads": value} for key, value in sorted(counts.items())],
            "cities": [{"city": key, **value, "revised_fraction": safe_ratio(value["revised_heads"], value["heads"])}
                       for key, value in sorted(groups.items())]}


def source_city_balance(period):
    grouped = defaultdict(lambda: {"orders": 0, "net_cents": 0, "heads": 0})
    coverage = defaultdict(Counter)
    applicability = defaultdict(Counter)
    for order in period["orders"]:
        key = (order["source"], order["origin"])
        grouped[key]["heads"] += 1
        grouped[key]["orders"] += int(order["state"] == "booked")
        grouped[key]["net_cents"] += order["gross_cents"] - order["refund_cents"]
    for cell in period["coverage"]:
        key = (cell["source"], cell["city"])
        coverage[key][cell["status"]] += 1
        applicability[key][cell.get("applicability", "applicable")] += 1
    result = []
    for key in sorted(set(grouped) | set(coverage)):
        counts = coverage[key]
        scope = applicability[key]
        measures = grouped[key]
        total = sum(counts.values())
        applicable = scope["applicable"] if scope else total
        result.append({"source": key[0], "city": key[1], **measures,
                       "cells": total, "expected_days": applicable,
                       "not_applicable_days": scope["not_applicable"] if scope else 0,
                       "unconfirmed_days": scope["unconfirmed"] if scope else 0,
                       "ambiguous_days": scope["ambiguous"] if scope else 0,
                       "complete_days": counts["complete"],
                       "partial_days": counts["partial"], "missing_days": counts["missing"],
                       "complete": applicable > 0 and counts["complete"] == applicable,
                       "observed_orders_per_day": safe_ratio(measures["orders"], applicable)})
    return result


def cancellation_retention(period):
    groups = defaultdict(lambda: {"cancelled_orders": 0, "gross_cents": 0, "refund_cents": 0, "retained_cents": 0})
    for order in period["orders"]:
        if order["state"] != "cancelled":
            continue
        item = groups[order["source"]]
        item["cancelled_orders"] += 1
        item["gross_cents"] += order["gross_cents"]
        item["refund_cents"] += order["refund_cents"]
        item["retained_cents"] += order["gross_cents"] - order["refund_cents"]
    return [{"source": source, **values, "retention_fraction": safe_ratio(values["retained_cents"], values["gross_cents"])}
            for source, values in sorted(groups.items())]


def diagnostics(period):
    return {"source_reliability": source_reliability(period), "missing_intervals": missing_intervals(period),
            "revision_exposure": revision_exposure(period), "source_city_balance": source_city_balance(period),
            "cancellation_retention": cancellation_retention(period), "scope_review": scope_review(period)}
