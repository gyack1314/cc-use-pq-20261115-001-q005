from ..values import safe_ratio


def percent(value):
    if value is None:
        return "无法计算"
    return f"{value * 100:.1f}%"


def money(cents):
    return f"{cents / 100:,.2f}元"


def observation(code, sentence, evidence):
    return {"code": code, "text": sentence, "evidence": evidence}


def summarize_volume(period, period_name):
    total = period["cube"]["totals"]
    coverage = period["coverage_summary"]
    declared = "applicability" in coverage
    complete = coverage["fully_observed"]
    if declared:
        prefix = "完整覆盖的应报范围" if complete else "应报范围内的已收到样本记录"
        scope_note = (f"应报{coverage['expected_cells']}个来源城市日期，"
                      f"不适用{coverage['not_applicable_cells']}个，"
                      f"尚未确认{coverage['unconfirmed_cells']}个。")
    else:
        prefix = "完整覆盖窗口" if complete else "已收到的样本记录"
        scope_note = ""
    result = [observation("volume", f"{period_name}的{prefix}包含{total['orders']}笔有效订单、"
                                    f"{total['travelers']}人次，净收入{money(total['net_cents'])}。{scope_note}",
                          {"totals": total, "coverage": coverage})]
    if total["cancelled_orders"]:
        result.append(observation("cancelled", f"另有{total['cancelled_orders']}笔取消订单；其未退金额保留在收入中，出游人数不计。",
                                  {"cancelled_orders": total["cancelled_orders"]}))
    if not complete:
        result.append(observation("coverage_limit", "缺报与部分覆盖不按零需求解释，当前总量不能代表整个选择范围。", coverage))
    if declared and coverage["not_applicable_cells"]:
        result.append(observation("not_applicable", "声明为不经营的来源城市日期不进入应报范围和暴露分母，也不按零订单计。",
                                  {"not_applicable_cells": coverage["not_applicable_cells"]}))
    if declared and coverage["unconfirmed_cells"]:
        result.append(observation("unconfirmed_scope", "未登记经营范围的来源城市日期视为尚未确认，不能自动当作不经营。",
                                  {"unconfirmed_cells": coverage["unconfirmed_cells"]}))
    if declared and coverage["ambiguous_cells"]:
        result.append(observation("ambiguous_scope", "经营范围规则存在重叠冲突，分析保留冲突而不是选定解释。",
                                  {"ambiguous_cells": coverage["ambiguous_cells"]}))
    disputes = period.get("scope_disputes") or []
    if disputes:
        result.append(observation("outside_scope_records",
                                  f"收到{len(disputes)}笔落在被声明为不经营范围的订单，已列为冲突并阻止发布。",
                                  {"count": len(disputes), "examples": disputes[:20]}))
    return result


def summarize_days(period):
    mix = period["cube"]["day_mix"]
    weekday = mix["weekday"]
    weekend = mix["weekend"]
    ratio = mix["weekday_weekend_ratio"]
    if ratio is None:
        return [observation("day_ratio_unavailable", "缺少工作日或周末暴露，或周末有效订单为零，无法给出工作日与周末热度比。", mix)]
    sentence = f"按城市日期暴露折算，工作日与周末有效订单日均之比为{ratio:.3f}。"
    return [observation("day_ratio", sentence, {"weekday": weekday, "weekend": weekend, "ratio": ratio})]


def summarize_peaks(period, names):
    result = []
    for row in period["cube"]["peak_days"]:
        name = names.get(row["city"], row["city"])
        if not row["days"]:
            result.append(observation("no_peak", f"{name}未观察到有效订单，不指定峰值日期。", row))
            continue
        days = "、".join(row["days"])
        in_holiday = "全部落在已登记假期内" if row["holiday_peak"] else "并非全部位于已登记假期内"
        result.append(observation("peak", f"{name}峰值为{row['orders']}笔，日期为{days}，{in_holiday}。", row))
    return result


def summarize_comparison(comparison):
    if comparison is None:
        return []
    result = []
    for metric, label in (("orders", "有效订单"), ("net_cents", "净收入")):
        change = comparison["totals"][metric]
        if change["baseline_zero"]:
            sentence = f"{label}对比期为零，仅报告绝对变化，不计算增长率。"
        else:
            sentence = f"已观察{label}较对比期变化{percent(change['relative_change'])}。"
        result.append(observation("period_change", sentence, {"metric": metric, **change,
                                                               "fully_comparable": comparison["fully_comparable"]}))
    for warning in comparison["warnings"]:
        result.append(observation("comparison_limit", warning, {}))
    comparability = comparison.get("scope_comparability")
    if comparability:
        for change in comparability["revised_rules"]:
            result.append(observation(
                "scope_revision_change",
                f"经营范围规则{change['scope_id']}当前为第{change['active_version']}版，"
                f"受修订字段：{'、'.join(change['changed_fields'])}；报告按当前版本重述两期。",
                change))
        for shift in comparability["shifted_exposure"]:
            result.append(observation(
                "scope_exposure_shift",
                f"{shift['source']}/{shift['city']}当前期不适用{shift['current_not_applicable']}天、"
                f"对比期不适用{shift['baseline_not_applicable']}天，暴露分母随适用范围变化。",
                shift))
    return result


def explain_result(result):
    names = {row["id"]: row["name"] for row in result["references"]["cities"]}
    observations = []
    for name, period in result["periods"].items():
        observations += summarize_volume(period, "当前期" if name == "current" else "对比期")
    current = result["periods"]["current"]
    observations += summarize_days(current)
    observations += summarize_peaks(current, names)
    observations += summarize_comparison(result["comparison"])
    blockers = [issue for issue in result["quality"]["issues"] if issue["severity"] == "error"]
    return {"data_revision": result["data_revision"], "publishable": not blockers,
            "observations": observations, "publication_blockers": blockers,
            "scope_note": "以上只描述所选来源样本，不推断整体游客规模或秋假政策因果效应"}
