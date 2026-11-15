"""Helpers for reading coverage that may carry a declared operating scope."""

OBSERVED_REPORT_STATUSES = {"observed", "confirmed_zero"}


def is_declared(period):
    return any("applicability" in cell for cell in period.get("coverage", []))


def applicable(cell):
    return cell.get("applicability", "applicable") == "applicable"


def observed(cell):
    if "report_status" in cell:
        return cell["report_status"] in OBSERVED_REPORT_STATUSES
    return cell["status"] == "complete"


def complete_city_days(period):
    """City-day is usable only when every applicable source cell is confirmed complete.

    Not-applicable cells never count as missing days, and are never inflating the
    denominator either. Unconfirmed or ambiguous scope keeps the day unusable.
    """
    grouped = {}
    for cell in period["coverage"]:
        grouped.setdefault((cell["city"], cell["day"]), []).append(cell)
    result = {}
    for key, cells in grouped.items():
        applicable_cells = [cell for cell in cells if applicable(cell)]
        result[key] = bool(applicable_cells) and all(observed(cell) for cell in applicable_cells)
    return result
