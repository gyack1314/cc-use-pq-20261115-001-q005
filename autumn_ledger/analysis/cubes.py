from collections import Counter, defaultdict

from ..errors import require
from ..records.contracts import contribution
from ..values import safe_ratio
from .windows import day_type


MEASURES = ("orders", "cancelled_orders", "travelers", "children", "gross_cents",
            "refund_cents", "net_cents", "family_orders")


def empty_measures():
    return {name: 0 for name in MEASURES}


def add_measures(target, values):
    for name in MEASURES:
        target[name] += values[name]


def enrich(values):
    return {**values, "average_net_cents": safe_ratio(values["net_cents"], values["orders"]),
            "family_share": safe_ratio(values["family_orders"], values["orders"]),
            "refund_fraction": safe_ratio(values["refund_cents"], values["gross_cents"]),
            "cancellation_fraction": safe_ratio(values["cancelled_orders"], values["orders"] + values["cancelled_orders"])}


def totals(orders):
    result = empty_measures()
    for order in orders:
        add_measures(result, contribution(order))
    return enrich(result)


def aggregate(orders, dimensions, classifier=None):
    groups = {}
    for order in orders:
        derived = classifier(order) if classifier else {}
        dimensions_value = {key: derived[key] if key in derived else order[key] for key in dimensions}
        identity = tuple(dimensions_value[key] for key in dimensions)
        if identity not in groups:
            groups[identity] = {"dimensions": dimensions_value, "measures": empty_measures(), "order_ids": []}
        group = groups[identity]
        add_measures(group["measures"], contribution(order))
        group["order_ids"].append(f"{order['source']}:{order['order_id']}:{order['sequence']}")
    result = []
    for key in sorted(groups, key=lambda v: tuple(str(x) for x in v)):
        group = groups[key]
        result.append({"dimensions": group["dimensions"], "measures": enrich(group["measures"]),
                       "lineage_count": len(group["order_ids"])})
    return result


def daily(orders, window, cities, grid, matrix=None):
    grouped = {(item["dimensions"]["origin"], item["dimensions"]["travel_day"]): item["measures"]
               for item in aggregate(orders, ["origin", "travel_day"])}
    applicable_city_days = {}
    counts = {}
    if matrix is not None:
        for cell in matrix:
            if "applicability" not in cell:
                continue
            key = (cell["city"], cell["day"])
            applicable_city_days[key] = (applicable_city_days.get(key, False)
                                         or cell["applicability"] == "applicable")
            if "orders" in cell:
                counts[key] = counts.get(key, 0) + cell["orders"]
    result = []
    for row in grid:
        key = (row["city"], row["day"])
        if key in applicable_city_days and not applicable_city_days[key]:
            # 该城市日期对所选来源都不在应报范围：不进入暴露分母，也不伪造零需求格。
            continue
        measures = grouped.get(key, enrich(empty_measures()))
        item = {**row, "measures": measures}
        if counts:
            item["observed_order_heads"] = counts.get(key, 0)
        result.append(item)
    return result


def rank(orders, dimension, metric="orders"):
    require(metric in MEASURES, "invalid_metric", "未知指标")
    grouped = aggregate(orders, [dimension])
    grouped.sort(key=lambda item: (-item["measures"][metric], str(item["dimensions"][dimension])))
    total = sum(item["measures"][metric] for item in grouped)
    previous = None
    position = 0
    result = []
    for index, item in enumerate(grouped, 1):
        value = item["measures"][metric]
        if previous != value:
            position = index
        result.append({"rank": position, "id": item["dimensions"][dimension],
                       "measures": item["measures"], "share": safe_ratio(value, total)})
        previous = value
    return result


def peak_days(daily_rows):
    by_city = defaultdict(list)
    for row in daily_rows:
        by_city[row["city"]].append(row)
    result = []
    for city, rows in sorted(by_city.items()):
        maximum = max(row["measures"]["orders"] for row in rows)
        if maximum == 0:
            result.append({"city": city, "orders": 0, "days": [], "holiday_peak": None})
            continue
        peaks = [row for row in rows if row["measures"]["orders"] == maximum]
        result.append({"city": city, "orders": maximum, "days": [row["day"] for row in peaks],
                       "holiday_peak": all(row["holiday"] for row in peaks)})
    return result


def distribution(orders):
    buckets = {"under_100": empty_measures(), "100_to_499": empty_measures(),
               "500_to_1999": empty_measures(), "2000_plus": empty_measures()}
    for order in orders:
        net = order["gross_cents"] - order["refund_cents"]
        if net < 10000:
            key = "under_100"
        elif net < 50000:
            key = "100_to_499"
        elif net < 200000:
            key = "500_to_1999"
        else:
            key = "2000_plus"
        add_measures(buckets[key], contribution(order))
    return [{"bucket": key, "measures": enrich(value)} for key, value in buckets.items()]


def day_mix(orders, daily_rows):
    exposures = Counter(row["day_type"] for row in daily_rows)
    groups = {item["dimensions"]["day_type"]: item["measures"]
              for item in aggregate(orders, ["day_type"], lambda o: {"day_type": day_type(o["travel_day"])})}
    result = {}
    for kind in ("weekday", "weekend"):
        measures = groups.get(kind, enrich(empty_measures()))
        result[kind] = {"measures": measures, "city_days": exposures[kind],
                        "orders_per_city_day": safe_ratio(measures["orders"], exposures[kind]),
                        "net_cents_per_city_day": safe_ratio(measures["net_cents"], exposures[kind])}
    result["weekday_weekend_ratio"] = None
    weekday = result["weekday"]["orders_per_city_day"]
    weekend = result["weekend"]["orders_per_city_day"]
    if weekday is not None and weekend is not None:
        result["weekday_weekend_ratio"] = safe_ratio(weekday, weekend)
    return result


def holiday_mix(orders, daily_rows):
    lookup = {(row["city"], row["day"]): row["holiday"] for row in daily_rows}
    grouped = {True: [], False: []}
    for order in orders:
        grouped[lookup[(order["origin"], order["travel_day"])]].append(order)
    exposures = Counter(row["holiday"] for row in daily_rows)
    result = {}
    for holiday in (True, False):
        measures = totals(grouped[holiday])
        result["holiday" if holiday else "outside_holiday"] = {
            "measures": measures, "city_days": exposures[holiday],
            "orders_per_city_day": safe_ratio(measures["orders"], exposures[holiday])}
    return result


def reconcile(orders, tables):
    expected = totals(orders)
    result = []
    for name, rows in tables.items():
        actual = empty_measures()
        for row in rows:
            add_measures(actual, row["measures"])
        differences = {key: actual[key] - expected[key] for key in MEASURES}
        result.append({"table": name, "ok": all(value == 0 for value in differences.values()),
                       "differences": differences})
    return result


def add_orders(target, orders):
    for order in orders:
        add_measures(target, contribution(order))


def split_measured(orders, matrix):
    """Split orders into declared-applicable ones and everything else.

    Orders on not-applicable, unconfirmed, or ambiguous cells are kept visible as a
    conflict set: never silently dropped, never used to widen a declared scope, and
    never left in a denominator whose applicability is unknown.
    """
    if matrix is None:
        return orders, []
    declared = [cell for cell in matrix if "applicability" in cell]
    if not declared:
        return orders, []
    by_cell = {(cell["source"], cell["city"], cell["day"]): cell for cell in declared}
    inside = []
    outside = []
    for order in orders:
        cell = by_cell.get((order["source"], order["origin"], order["travel_day"]))
        if cell is not None and cell["applicability"] == "applicable":
            inside.append(order)
        else:
            applicability = cell["applicability"] if cell else None
            reason = {"not_applicable": "declared_not_applicable",
                      "ambiguous": "ambiguous_scope",
                      "unconfirmed": "unconfirmed_scope"}.get(applicability, "outside_declared_matrix")
            outside.append({**order, "scope_applicability": applicability,
                            "scope_ids": cell["scope_ids"] if cell else [],
                            "exclusion_reason": reason})
    return inside, outside


def build_cubes(orders, window, cities, grid, matrix=None):
    measured, outside = split_measured(orders, matrix)
    daily_rows = daily(measured, window, cities, grid, matrix)
    tables = {"origins": aggregate(measured, ["origin"]),
              "destinations": aggregate(measured, ["destination"]),
              "sources": aggregate(measured, ["source"]),
              "routes": aggregate(measured, ["origin", "destination"]),
              "daily": daily_rows}
    checks = reconcile(measured, tables)
    require(all(item["ok"] for item in checks), "reconciliation_failed", "分析分组与总量未对账")
    totals_value = totals(measured)
    excluded_total = empty_measures()
    add_orders(excluded_total, outside)
    return {"totals": totals_value, "tables": tables, "reconciliation": checks,
            "origin_ranking": rank(measured, "origin"), "destination_ranking": rank(measured, "destination"),
            "peak_days": peak_days(tables["daily"]), "spending_distribution": distribution(measured),
            "day_mix": day_mix(measured, daily_rows), "holiday_mix": holiday_mix(measured, daily_rows),
            "exposure_city_days": len(daily_rows),
            "measured_orders": measured, "outside_scope_orders": outside,
            "outside_scope": {"order_heads": len(outside),
                              "measured": enrich(excluded_total) if outside else enrich(empty_measures()),
                              "order_ids": [f"{order['source']}:{order['order_id']}" for order in outside]}}
