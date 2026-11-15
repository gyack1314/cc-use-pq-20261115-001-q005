from ..errors import require
from ..values import fingerprint, utc_now
from .comparison import comparable_coverage, compare_periods
from .cubes import build_cubes
from .quality import quality_report, validate_result
from .windows import daily_grid, exposure_summary, match_order, normalize_request, request_windows
from .segments import profile_period, cross_shares
from .trends import daily_trends, weekday_seasonality
from .diagnostics import diagnostics
from .calendar_view import calendar_analysis
from .invariants import inspect_result


class AnalysisEngine:
    def __init__(self, database, catalog, calendars, coverage, intake, scopes):
        self.database = database
        self.catalog = catalog
        self.calendars = calendars
        self.coverage = coverage
        self.intake = intake
        self.scopes = scopes

    def run(self, value, connection=None):
        request = normalize_request(value)
        if connection is None:
            with self.database.transaction() as opened:
                return self.run(request, opened)
        revision = self.database.snapshot_revision(connection, request.get("revision"))
        references = self.validate_references(request, revision, connection)
        heads = self.intake.heads(revision, request["sources"], connection)
        windows = request_windows(request)
        declared = request.get("scope_mode") == "declared"
        periods = {}
        lineage = []
        for name, window in windows.items():
            selected = [order for order in heads if match_order(order, request, window)]
            exposure = self.calendars.exposure(request["cities"], window.start, window.end, revision, connection)
            grid = daily_grid(window, request["cities"], exposure)
            matrix = self.coverage.matrix(request["sources"], request["cities"],
                                          window.start, window.end, revision, connection)
            order_counts = {}
            for order in selected:
                key = (order["source"], order["origin"], order["travel_day"])
                order_counts[key] = order_counts.get(key, 0) + 1
            scopes = None
            scope_cells = None
            if declared:
                scope_cells = self.scopes.matrix(request["sources"], request["cities"],
                                                 window.start, window.end, revision, connection)
                matrix = self.scopes.annotate(scope_cells, matrix, order_counts)
                scopes = self.scopes.snapshot(request["sources"], request["cities"],
                                              revision, connection)
            cube = build_cubes(selected, window, request["cities"], grid, matrix)
            # 声明口径下，范围外订单只进入 excluded_orders 与 scope_disputes，
            # 统计、画像、趋势和谱系统一只用应报范围内的订单；不静默丢弃也不混入分母。
            measured = cube["measured_orders"]
            exposure_rows = cube["tables"]["daily"] if declared else grid
            periods[name] = {"window": window.as_dict(), "orders": measured,
                             "excluded_orders": cube["outside_scope_orders"], "exposure": exposure_rows,
                             "exposure_summary": exposure_summary(exposure_rows), "coverage": matrix,
                             "coverage_summary": self.coverage.summary(matrix),
                             "coverage_gaps": self.coverage.missing_groups(matrix), "cube": cube,
                             "scope_mode": request.get("scope_mode", "legacy")}
            period = periods[name]
            if declared:
                summary = self.scopes.summary(matrix)
                period["scope"] = scopes
                period["scope_cells"] = scope_cells
                period["scope_summary"] = summary
                period["coverage_summary"] = summary
                period["scope_gaps"] = self.scopes.gaps(matrix)
                period["coverage_gaps"] = period["scope_gaps"]
                period["scope_declarations"] = self.scopes.list(revision=revision, connection=connection)
                period["scope_disputes"] = self.scope_disputes(selected, matrix)
            period["profiles"] = {dimension: profile_period(period, references, dimension)
                                  for dimension in ("origin", "destination", "source")}
            period["route_association"] = cross_shares(measured, "origin", "destination")
            period["daily_trends"] = daily_trends(period)
            period["weekday_seasonality"] = weekday_seasonality(period)
            period["diagnostics"] = diagnostics(period)
            period["calendar_analysis"] = calendar_analysis(period, references)
            for order in measured:
                lineage.append({"period": name, "source": order["source"], "order_id": order["order_id"],
                                "event_id": order["event_id"], "sequence": order["sequence"],
                                "revision": order["ingested_revision"], "digest": fingerprint(order)})
        comparison = None
        if "baseline" in periods:
            pairs = comparable_coverage(periods["current"]["coverage"], periods["baseline"]["coverage"],
                                        request["sources"], request["cities"])
            comparison = compare_periods(periods["current"]["cube"], periods["baseline"]["cube"],
                                         windows["current"], windows["baseline"], pairs)
            if declared:
                comparability = self.scope_comparability(periods["current"], periods["baseline"],
                                                         pairs)
                comparison["scope_comparability"] = comparability
        quality = quality_report(periods, request, comparison)
        result = {"format_version": 1, "data_revision": revision, "request": request,
                  "references": references, "periods": periods, "comparison": comparison,
                  "quality": quality, "lineage": lineage, "lineage_digest": fingerprint(lineage),
                  "calculated_at": utc_now(), "semantics": self.semantics(declared)}
        result["integrity"] = validate_result(result)
        result["statistical_checks"] = inspect_result(result)
        require(result["statistical_checks"]["ok"], "statistical_integrity", "统计口径一致性失败",
                details={"failures": result["statistical_checks"]["failures"]})
        require(result["integrity"]["ok"], "analysis_integrity", "分析内部一致性失败",
                details=result["integrity"])
        return result

    def scope_disputes(self, orders, matrix):
        """Orders received for a cell not declared applicable to the source.

        Not-applicable cells are direct contradictions; unconfirmed or ambiguous
        cells are unresolved scope, both listed rather than dropped or reinterpreted.
        """
        indexed = {(cell["source"], cell["city"], cell["day"]): cell for cell in matrix}
        disputes = []
        for order in orders:
            cell = indexed.get((order["source"], order["origin"], order["travel_day"]))
            if cell is None or cell["applicability"] == "applicable":
                continue
            reason = ("订单落在声明为不经营的范围" if cell["applicability"] == "not_applicable"
                      else "订单落在规则冲突、无法唯一解释的范围" if cell["applicability"] == "ambiguous"
                      else "订单落在尚未声明经营范围的格子")
            disputes.append({"source": order["source"], "order_id": order["order_id"],
                             "event_id": order["event_id"], "sequence": order["sequence"],
                             "city": order["origin"], "day": order["travel_day"],
                             "applicability": cell["applicability"],
                             "scope_ids": cell["scope_ids"],
                             "exclusion_reason": {
                                 "not_applicable": "declared_not_applicable",
                                 "unconfirmed": "unconfirmed_scope",
                                 "ambiguous": "ambiguous_scope"}[cell["applicability"]],
                             "message": reason + "，需修正数据或经营范围后才能发布"})
        return disputes

    def validate_references(self, request, revision, connection):
        references = {"cities": [], "sources": [], "destinations": []}
        for city in request["cities"]:
            references["cities"].append(self.catalog.get("city", city, revision, connection))
        for source in request["sources"]:
            references["sources"].append(self.catalog.get("source", source, revision, connection))
        for destination in request["destinations"]:
            references["destinations"].append(self.catalog.get("destination", destination, revision, connection))
        if not request["destinations"]:
            references["destinations"] = self.catalog.list("destination", revision, connection)
        references["calendars"] = [item for item in self.calendars.list(revision=revision, connection=connection)
                                    if item["city"] in request["cities"]]
        if request.get("scope_mode") == "declared":
            references["operating_scopes"] = self.scopes.list(revision=revision, connection=connection)
        return references

    def semantics(self, declared=False):
        result = {"order_identity": "source+order_id", "head": "不超过数据版本的最高sequence",
                  "amount_unit": "人民币分", "travel_day": "供应商给出的目的地本地出游日期",
                  "cancelled": "取消订单不计出游量；未退金额仍计入净收入",
                  "refund": "refund_cents为订单累计退款，不是本次退款增量",
                  "coverage": "complete表示来源确认该城市日期数据已齐，未登记不能当零",
                  "denominator": "日均按选定城市日期暴露计，不按有订单的日期计",
                  "scope": "仅描述所选来源订单，不推断总体或因果效应"}
        result["coverage_mode"] = "declared" if declared else "legacy"
        result["scope_interpretation"] = (
            "声明经营范围内complete/partial/missing沿用原覆盖含义；范围外标不适用；"
            "未声明范围视为尚未确认，不自动当作不经营；范围冲突或范围外订单阻止发布"
            if declared else "旧请求沿用全组合覆盖语义：未登记格子按missing处理")
        return result

    def scope_comparability(self, current, baseline, pairs):
        """Show, inside one report, how declared scope shapes comparability.

        A report always interprets both windows from one snapshot, so the rules
        themselves are identical across periods. What can still change is exposure:
        a rule whose date range only covers one window moves cells between
        not-applicable and applicable. Rule *revisions* across reports are traced by
        ``Editions.compare_editions`` using the stored scope snapshot.
        """
        scope = current.get("scope", {})
        chains = {}
        for row in scope.get("versions", []):
            chains.setdefault(row["id"], []).append(row)
        revised = []
        for identity, chain in sorted(chains.items()):
            active = max(chain, key=lambda row: row["version"])
            if len(chain) < 2:
                continue
            previous = max((row for row in chain if row["version"] < active["version"]),
                           key=lambda row: row["version"], default=None)
            revised.append({"scope_id": identity, "active_version": active["version"],
                            "versions": len(chain),
                            "changed_fields": sorted(
                                field for field in ("source", "city", "start", "end",
                                                    "applicable", "status", "note")
                                if (previous or {}).get(field) != active.get(field)),
                            "before": previous,
                            "after": {key: active[key] for key in
                                      ("id", "version", "source", "city", "start", "end",
                                       "applicable", "status", "note")}})
        shifted = [{"source": row["source"], "city": row["city"],
                    "current_not_applicable": row["current_not_applicable"],
                    "baseline_not_applicable": row["baseline_not_applicable"],
                    "current_expected": row["current_expected"],
                    "baseline_expected": row["baseline_expected"],
                    "comparable": row["comparable"]}
                   for row in pairs
                   if row["current_not_applicable"] != row["baseline_not_applicable"]
                   or row["current_expected"] != row["baseline_expected"]]
        return {"scope_fingerprint": scope.get("digest"),
                "same_rules_across_periods": True,
                "revised_rules": revised,
                "shifted_exposure": shifted,
                "comparable_source_cities": [pair for pair in pairs if pair["scope_declared"]],
                "note": ("规则按数据版本整体生效，两期使用同一范围；窗口内适用范围不同的来源城市会"
                         "改变暴露和可比性。跨报告的范围修订由版本对比列出。")}

    def preview(self, value):
        result = self.run(value)
        return {"data_revision": result["data_revision"], "request": result["request"],
                "current": result["periods"]["current"]["cube"]["totals"],
                "baseline": result["periods"].get("baseline", {}).get("cube", {}).get("totals"),
                "quality": result["quality"], "lineage_digest": result["lineage_digest"],
                "integrity": result["integrity"]}

    def reconcile_revision(self, value, expected_digest):
        result = self.run(value)
        require(result["lineage_digest"] == expected_digest, "lineage_changed", "订单证据与预期不同", 409)
        return result
