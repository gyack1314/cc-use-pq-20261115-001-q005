from ..values import safe_ratio
from .cubes import MEASURES, empty_measures
from .windows import aligned_day_pairs


def difference(current, baseline):
    return {"current": current, "baseline": baseline, "absolute_change": current - baseline,
            "relative_change": (current - baseline) / baseline if baseline else None,
            "baseline_zero": baseline == 0}


def compare_measures(current, baseline):
    return {metric: difference(current[metric], baseline[metric]) for metric in MEASURES}


def comparable_coverage(current_matrix, baseline_matrix, sources, cities):
    def statuses(matrix):
        result = {}
        for row in matrix:
            result.setdefault((row["source"], row["city"]), []).append(row)
        return result
    current = statuses(current_matrix)
    baseline = statuses(baseline_matrix)
    pairs = []
    for source in sources:
        for city in cities:
            key = (source, city)
            left = current.get(key, [])
            right = baseline.get(key, [])
            left_applicable = [row for row in left if row.get("applicability", "applicable") == "applicable"]
            right_applicable = [row for row in right if row.get("applicability", "applicable") == "applicable"]
            declared = any("applicability" in row for row in left + right)
            unresolved = (sum(row.get("applicability") in {"unconfirmed", "ambiguous"} for row in left)
                          + sum(row.get("applicability") in {"unconfirmed", "ambiguous"} for row in right))
            if declared:
                # 不适用格不进入可比要求；两期应报格都已确认完整即可比，即使
                # 某期该城市不适用或两期应报格数不同（差异单列在shifted_exposure）。
                complete = unresolved == 0 and all(_observed(row) for row in left_applicable + right_applicable)
            else:
                complete = (bool(left and right)
                            and all(_observed(row) for row in left_applicable + right_applicable))
            pairs.append({"source": source, "city": city, "comparable": complete,
                          "current_complete": sum(_observed(row) for row in left_applicable),
                          "baseline_complete": sum(_observed(row) for row in right_applicable),
                          "current_expected": len(left) if not declared else len(left_applicable),
                          "baseline_expected": len(right) if not declared else len(right_applicable),
                          "current_not_applicable": len(left) - len(left_applicable),
                          "baseline_not_applicable": len(right) - len(right_applicable),
                          "scope_declared": declared,
                          "current_unresolved_scope": sum(row.get("applicability") in {"unconfirmed", "ambiguous"}
                                                          for row in left),
                          "baseline_unresolved_scope": sum(row.get("applicability") in {"unconfirmed", "ambiguous"}
                                                           for row in right)})
    return pairs


def _observed(row):
    if "report_status" in row:
        return row["report_status"] in {"observed", "confirmed_zero"}
    return row["status"] == "complete"


def segment_changes(current_rows, baseline_rows, dimension):
    left = {row["dimensions"][dimension]: row["measures"] for row in current_rows}
    right = {row["dimensions"][dimension]: row["measures"] for row in baseline_rows}
    result = []
    for identity in sorted(set(left) | set(right)):
        current = left.get(identity, empty_measures())
        baseline = right.get(identity, empty_measures())
        result.append({"id": identity, "changes": compare_measures(current, baseline),
                       "present_current": identity in left, "present_baseline": identity in right})
    return result


def compare_periods(current, baseline, current_window, baseline_window, coverage_pairs):
    complete = all(pair["comparable"] for pair in coverage_pairs) and bool(coverage_pairs)
    current_count = len(current_window.days)
    baseline_count = len(baseline_window.days)
    normalized = {}
    for metric in MEASURES:
        left = current["totals"][metric] / current_count
        right = baseline["totals"][metric] / baseline_count
        normalized[metric] = difference(left, right)
    pairs = aligned_day_pairs(current_window, baseline_window)
    warnings = []
    if current_count != baseline_count:
        warnings.append("窗口天数不同，应结合日均指标阅读总量变化")
    if any(not pair["same_weekday"] for pair in pairs):
        warnings.append("按序号对齐的日期并非相同星期，周末构成可能影响变化")
    if not complete:
        warnings.append("至少一个来源城市组合存在缺报，变化只描述已收到记录")
    if any(pair.get("scope_declared") for pair in coverage_pairs):
        if any(pair["current_unresolved_scope"] or pair["baseline_unresolved_scope"]
               for pair in coverage_pairs):
            warnings.append("存在尚未声明或规则冲突的来源城市日期，不能自动当作不经营")
        shifted = [pair for pair in coverage_pairs
                   if pair["current_not_applicable"] != pair["baseline_not_applicable"]]
        if shifted:
            warnings.append("两期不适用格数不同，暴露口径已随经营范围改变，需对照范围修订说明")
    warnings.append("平台订单为观察样本，变化不代表总体增量或校历政策因果效应")
    return {"totals": compare_measures(current["totals"], baseline["totals"]),
            "per_day": normalized, "coverage_pairs": coverage_pairs,
            "fully_comparable": complete, "day_alignment": pairs, "warnings": warnings,
            "origins": segment_changes(current["tables"]["origins"], baseline["tables"]["origins"], "origin"),
            "destinations": segment_changes(current["tables"]["destinations"], baseline["tables"]["destinations"], "destination"),
            "sources": segment_changes(current["tables"]["sources"], baseline["tables"]["sources"], "source")}
