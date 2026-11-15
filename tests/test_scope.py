import io
import zipfile

from autumn_ledger.demo import request_data
from autumn_ledger.errors import LedgerError
from autumn_ledger.analysis.narrative import explain_result
from tests.support import LedgerCase


def declared(**changes):
    return request_data(scope_mode="declared", **changes)


class OperatingScopeTests(LedgerCase):
    def declare(self, identity, source, city, start, end, applicable=True, version=0, status="confirmed"):
        return self.ledger.scopes.put(identity, {"source": source, "city": city, "start": start, "end": end,
                                                 "applicable": applicable, "status": status}, version)

    def declare_core(self, sh_applicable=False):
        self.declare("alpha-sz", "alpha", "sz", "2026-11-09", "2026-11-22", True)
        self.declare("alpha-nj", "alpha", "nj", "2026-11-09", "2026-11-22", True)
        self.declare("alpha-sh", "alpha", "sh", "2026-11-09", "2026-11-22", sh_applicable)

    def test_scope_is_versioned_and_replayable(self):
        first = self.declare("alpha-sz", "alpha", "sz", "2026-11-09", "2026-11-22")
        self.assertEqual(first["version"], 1)
        snapshot = self.ledger.database.revision()
        revised = self.declare("alpha-sz", "alpha", "sz", "2026-11-09", "2026-11-15", version=1)
        self.assertEqual(revised["version"], 2)
        self.assertEqual(self.ledger.scopes.get("alpha-sz", snapshot)["end"], "2026-11-22")
        self.assertEqual(self.ledger.scopes.get("alpha-sz")["end"], "2026-11-15")
        history = self.ledger.scopes.history("alpha-sz")["history"]
        self.assertEqual([row["version"] for row in history], [1, 2])
        self.assertEqual(history[1]["changed_fields"], ["end"])
        self.assertError("conflict", self.declare, "alpha-sz", "alpha", "sz", "2026-11-09", "2026-11-22", True, 1)

    def test_scope_cannot_change_source_or_city(self):
        self.declare("alpha-sz", "alpha", "sz", "2026-11-09", "2026-11-22")
        self.assertError("scope_source_change", self.declare, "alpha-sz", "beta", "sz",
                         "2026-11-09", "2026-11-22", True, 1)
        self.assertError("scope_city_change", self.declare, "alpha-sz", "alpha", "nj",
                         "2026-11-09", "2026-11-22", True, 1)

    def test_conflicting_overlap_is_rejected_atomically(self):
        before = self.ledger.database.revision()
        self.declare("alpha-sz-open", "alpha", "sz", "2026-11-09", "2026-11-22", True)
        error = self.assertError("scope_ambiguity", self.declare, "alpha-sz-closed", "alpha", "sz",
                                 "2026-11-14", "2026-11-16", False)
        self.assertEqual(error.details["overlapping_rule"], "alpha-sz-open")
        self.assertEqual(self.ledger.database.revision(), before + 1)
        self.assertError("not_found", self.ledger.scopes.get, "alpha-sz-closed")
        self.assertEqual(len(self.ledger.scopes.list()), 1)

    def test_agreeing_retraction_and_nonoverlapping_rules_are_allowed(self):
        self.declare("alpha-sz-open", "alpha", "sz", "2026-11-09", "2026-11-22", True)
        nested = self.declare("alpha-sz-extra", "alpha", "sz", "2026-11-12", "2026-11-13", True)
        self.assertEqual(nested["version"], 1)
        closed = self.declare("alpha-sz-closed", "alpha", "sz", "2026-11-23", "2026-11-25", False)
        self.assertEqual(closed["applicable"], False)
        matrix = self.ledger.scopes.matrix(["alpha"], ["sz"], "2026-11-23", "2026-11-23")
        self.assertEqual(matrix[0]["applicability"], "not_applicable")

    def test_withdrawn_rule_does_not_gate_new_rules(self):
        self.declare("alpha-sz-old", "alpha", "sz", "2026-11-09", "2026-11-22", True)
        self.declare("alpha-sz-old", "alpha", "sz", "2026-11-09", "2026-11-22", True, 1, "withdrawn")
        replacement = self.declare("alpha-sz-new", "alpha", "sz", "2026-11-09", "2026-11-22", False)
        self.assertEqual(replacement["applicable"], False)
        matrix = self.ledger.scopes.matrix(["alpha"], ["sz"], "2026-11-16", "2026-11-16")
        self.assertEqual(matrix[0]["applicability"], "not_applicable")

    def test_undeclared_cells_are_unconfirmed_not_not_applicable(self):
        self.declare("alpha-sz", "alpha", "sz", "2026-11-09", "2026-11-22")
        matrix = self.ledger.scopes.matrix(["alpha"], ["sz", "nj"], "2026-11-16", "2026-11-16")
        by_city = {row["city"]: row for row in matrix}
        self.assertEqual(by_city["sz"]["applicability"], "applicable")
        self.assertEqual(by_city["nj"]["applicability"], "unconfirmed")

    def test_declared_analysis_separates_report_states(self):
        self.declare_core()
        result = self.ledger.analysis.run(declared(cities=["sz", "nj", "sh"]))
        current = result["periods"]["current"]
        summary = current["coverage_summary"]
        self.assertEqual(summary["cells"], 21)
        self.assertEqual(summary["applicable_cells"], 14)
        self.assertEqual(summary["not_applicable_cells"], 7)
        self.assertEqual(summary["unconfirmed_cells"], 0)
        self.assertEqual(summary["expected_cells"], 14)
        rows = {(row["city"], row["day"]): row for row in current["coverage"]}
        self.assertEqual(rows[("sz", "2026-11-16")]["report_status"], "observed")
        self.assertEqual(rows[("sh", "2026-11-16")]["report_status"], "not_applicable")
        self.assertEqual(rows[("sz", "2026-11-18")]["report_status"], "confirmed_zero")
        self.assertTrue(result["quality"]["publishable"])
        self.assertTrue(result["comparison"]["fully_comparable"])
        self.assertEqual(len(current["cube"]["tables"]["daily"]), 14)
        self.assertEqual(current["cube"]["day_mix"]["weekday"]["city_days"], 10)
        self.assertEqual(current["scope"]["rule_count"], 3)
        self.assertEqual(current["scope_disputes"], [])

    def test_declared_partial_and_missing_keep_original_coverage_meaning(self):
        self.declare("alpha-sz", "alpha", "sz", "2026-11-09", "2026-11-22", True)
        self.declare("alpha-nj", "alpha", "nj", "2026-11-09", "2026-11-22", True)
        self.ledger.coverage.put({"source": "alpha", "city": "sz", "day": "2026-11-18",
                                  "status": "partial", "note": "未齐"}, 1)
        self.ledger.coverage.put({"source": "alpha", "city": "sz", "day": "2026-11-19",
                                  "status": "missing", "note": "缺报"}, 1)
        result = self.ledger.analysis.run(declared())
        current = result["periods"]["current"]
        rows = {(row["city"], row["day"]): row for row in current["coverage"]}
        self.assertEqual(rows[("sz", "2026-11-18")]["status"], "partial")
        self.assertEqual(rows[("sz", "2026-11-18")]["report_status"], "partial")
        self.assertEqual(rows[("sz", "2026-11-19")]["report_status"], "missing")
        self.assertFalse(result["quality"]["publishable"])
        codes = {issue["code"] for issue in result["quality"]["issues"]}
        self.assertIn("partial_coverage", codes)
        self.assertIn("missing_coverage", codes)

    def test_undeclared_scope_blocks_publication_but_not_analysis(self):
        self.declare("alpha-sz", "alpha", "sz", "2026-11-09", "2026-11-22", True)
        result = self.ledger.analysis.run(declared())
        current = result["periods"]["current"]
        self.assertEqual(current["coverage_summary"]["unconfirmed_cells"], 7)
        self.assertFalse(result["quality"]["publishable"])
        codes = {issue["code"] for issue in result["quality"]["issues"]}
        self.assertIn("undeclared_scope", codes)
        # 未声明格子的订单不被当作应报数据，但也没有丢失，单独列在冲突区。
        self.assertEqual(current["cube"]["totals"]["orders"], 2)
        self.assertEqual(current["cube"]["outside_scope"]["order_heads"], 1)
        # 未声明格子没有被填零并计入分母：nj 整段没有暴露行。
        self.assertTrue(all(row["city"] == "sz" for row in current["cube"]["tables"]["daily"]))

    def test_order_outside_declared_scope_is_conflict_and_blocks_release(self):
        self.declare("alpha-sz", "alpha", "sz", "2026-11-09", "2026-11-22", True)
        self.declare("alpha-nj", "alpha", "nj", "2026-11-09", "2026-11-22", False)
        result = self.ledger.analysis.run(declared())
        self.assertEqual([row["order_id"] for row in result["periods"]["current"]["scope_disputes"]], ["a3"])
        self.assertEqual([row["order_id"] for row in result["periods"]["baseline"]["scope_disputes"]], ["b2"])
        self.assertFalse(result["quality"]["publishable"])
        codes = {issue["code"] for issue in result["quality"]["issues"]}
        self.assertIn("records_outside_scope", codes)
        # 冲突订单没有被静默丢弃，也没有被并进应报范围的统计；它单独列在分析里。
        outside = result["periods"]["current"]["cube"]["outside_scope"]
        self.assertEqual(outside["order_heads"], 1)
        self.assertEqual(result["periods"]["current"]["cube"]["totals"]["orders"], 2)

    def test_outside_scope_order_blocks_approval_and_publication(self):
        self.declare("alpha-sz", "alpha", "sz", "2026-11-09", "2026-11-22", True)
        self.declare("alpha-nj", "alpha", "nj", "2026-11-09", "2026-11-22", False)
        edition = self.ledger.editions.create("scope-blocked", declared())
        error = self.assertError("quality_blocked", self.approve, edition)
        self.assertEqual(error.status, 409)
        self.assertEqual(self.ledger.editions.get(edition["id"])["state"], "draft")
        readiness = self.ledger.publication_readiness.inspect(edition["id"])
        self.assertFalse(readiness["can_publish"])
        self.assertIn("scope_consistency", readiness["blockers"])
        # 修正范围后新报告可通过。
        self.declare("alpha-nj", "alpha", "nj", "2026-11-09", "2026-11-22", True, 1)
        fixed = self.ledger.editions.create("scope-fixed", declared())
        self.assertTrue(fixed["quality"]["publishable"])
        self.approve(fixed)

    def test_records_outside_scope_are_listed_for_investigation(self):
        self.declare("alpha-sz", "alpha", "sz", "2026-11-09", "2026-11-22", True)
        self.declare("alpha-nj", "alpha", "nj", "2026-11-09", "2026-11-22", False)
        edition = self.ledger.editions.create("scope-dispute", declared())
        disputes = self.ledger.investigation.coverage_disputes(edition["id"])
        self.assertEqual({row["order_id"] for row in disputes["scope_disputes"]}, {"a3"})
        nj = next(row for row in disputes["cells"] if row["city"] == "nj"
                  and row["report_status"] == "not_applicable")
        self.assertEqual(nj["applicability"], "not_applicable")
        self.assertEqual(nj["interpretation"], "声明为不经营")

    def test_legacy_request_semantics_are_preserved(self):
        legacy = self.ledger.analysis.run(request_data())
        self.assertNotIn("scope_mode", legacy["request"])
        self.assertNotIn("scope", legacy["periods"]["current"])
        self.assertEqual(legacy["semantics"]["coverage_mode"], "legacy")
        self.assertEqual(legacy["periods"]["current"]["coverage_summary"]["expected_cells"], 14)
        declared_result = self.ledger.analysis.run(declared())
        self.assertEqual(declared_result["semantics"]["coverage_mode"], "declared")
        # 同名请求在加入经营范围规则后仍按旧语义重算，结果不因新规则漂移。
        self.declare_core()
        legacy_after = self.ledger.analysis.run(request_data())
        self.assertEqual(legacy_after["periods"], legacy["periods"])
        with self.assertRaises(LedgerError) as context:
            self.ledger.analysis.run(request_data(scope_mode="invented"))
        self.assertEqual(context.exception.code, "invalid_scope_mode")

    def test_multi_source_city_day_exposed_when_any_source_applicable(self):
        # beta 刚接入：sz 应报但无覆盖；nj 对 alpha 应报、对 beta 不适用。
        self.declare("alpha-sz", "alpha", "sz", "2026-11-09", "2026-11-22", True)
        self.declare("alpha-nj", "alpha", "nj", "2026-11-09", "2026-11-22", True)
        self.declare("beta-sz", "beta", "sz", "2026-11-09", "2026-11-22", True)
        self.declare("beta-nj", "beta", "nj", "2026-11-09", "2026-11-22", False)
        result = self.ledger.analysis.run(declared(sources=["alpha", "beta"]))
        current = result["periods"]["current"]
        summary = current["coverage_summary"]
        self.assertEqual(summary["applicable_cells"], 21)
        self.assertEqual(summary["not_applicable_cells"], 7)
        self.assertEqual(summary["missing_cells"], 7)  # beta 的 sz 覆盖全部缺失
        self.assertEqual(len(current["cube"]["tables"]["daily"]), 14)  # 每个城市日期只出现一次
        codes = {issue["code"] for issue in result["quality"]["issues"]}
        self.assertIn("missing_coverage", codes)
        self.assertFalse(result["quality"]["publishable"])

    def test_scope_revision_keeps_old_report_reproducible(self):
        self.declare("alpha-sz", "alpha", "sz", "2026-11-09", "2026-11-22", True)
        self.declare("alpha-nj", "alpha", "nj", "2026-11-09", "2026-11-22", False)
        original = self.ledger.analysis.run(declared())
        revision = self.ledger.database.revision()
        first = self.ledger.editions.create("scope-before", declared())
        self.declare("alpha-nj", "alpha", "nj", "2026-11-16", "2026-11-22", True, 1)
        revised = self.ledger.analysis.run(declared())
        # 同一版本内两期共用一份范围快照，修订影响的是重述口径。
        self.assertEqual(revised["periods"]["current"]["coverage_summary"]["not_applicable_cells"], 0)
        self.assertEqual(revised["periods"]["baseline"]["coverage_summary"]["not_applicable_cells"], 0)
        comparability = revised["comparison"]["scope_comparability"]
        self.assertTrue(comparability["same_rules_across_periods"])
        self.assertEqual([row["scope_id"] for row in comparability["revised_rules"]], ["alpha-nj"])
        self.assertEqual(comparability["revised_rules"][0]["active_version"], 2)
        self.assertEqual(comparability["revised_rules"][0]["changed_fields"],
                         ["applicable", "start"])
        codes = {issue["code"] for issue in revised["quality"]["issues"]}
        self.assertIn("scope_revision", codes)
        narrative = explain_result(revised)
        self.assertTrue(any(item["code"] == "scope_revision_change" for item in narrative["observations"]))
        # 旧版本仍可完整重现旧范围下的报告。
        old = self.ledger.analysis.run(declared(revision=revision))
        self.assertEqual(old["periods"], original["periods"])
        self.assertEqual(old["lineage_digest"], original["lineage_digest"])
        self.assertEqual(old["comparison"], original["comparison"])
        # 两个版本对比能说明范围修订对可比范围的影响。
        second = self.ledger.editions.create("scope-after", declared())
        difference = self.ledger.editions.compare_editions(first["id"], second["id"])
        self.assertFalse(difference["scope"]["same_scope"])
        self.assertEqual([row["scope_id"] for row in difference["scope"]["changes"]], ["alpha-nj"])
        self.assertEqual(difference["scope"]["changes"][0]["type"], "revised")

    def test_window_specific_rule_shifts_exposure_between_periods(self):
        # 该城市在对比期声明为不经营、当前期声明为应报，范围完整但两期暴露不同。
        self.declare("alpha-sz", "alpha", "sz", "2026-11-09", "2026-11-22", True)
        self.declare("alpha-nj-base", "alpha", "nj", "2026-11-09", "2026-11-15", False)
        self.declare("alpha-nj-now", "alpha", "nj", "2026-11-16", "2026-11-22", True)
        result = self.ledger.analysis.run(declared())
        self.assertTrue(result["comparison"]["fully_comparable"])
        self.assertEqual(result["periods"]["current"]["coverage_summary"]["expected_cells"], 14)
        self.assertEqual(result["periods"]["baseline"]["coverage_summary"]["expected_cells"], 7)
        self.assertEqual(result["periods"]["baseline"]["coverage_summary"]["not_applicable_cells"], 7)
        shifted = result["comparison"]["scope_comparability"]["shifted_exposure"]
        self.assertEqual([row["city"] for row in shifted], ["nj"])
        self.assertEqual(shifted[0]["current_expected"], 7)
        self.assertEqual(shifted[0]["baseline_expected"], 0)
        codes = {issue["code"] for issue in result["quality"]["issues"]}
        self.assertIn("scope_revision", codes)

    def test_declared_cells_never_divide_by_not_applicable(self):
        self.declare_core()
        current = self.ledger.analysis.run(declared(cities=["sz", "nj", "sh"]))["periods"]["current"]
        self.assertEqual(current["coverage_summary"]["complete_fraction"], 1)
        rows = current["diagnostics"]["source_city_balance"]
        sz = next(row for row in rows if row["city"] == "sz")
        sh = next(row for row in rows if row["city"] == "sh")
        self.assertEqual(sz["expected_days"], 7)
        self.assertEqual(sh["not_applicable_days"], 7)
        self.assertEqual(sh["expected_days"], 0)
        self.assertIsNone(sh["observed_orders_per_day"])

    def test_declared_invariants_and_exports(self):
        self.declare_core()
        edition = self.ledger.editions.create("declared-export", declared(cities=["sz", "nj", "sh"]))
        self.assertTrue(edition["quality"]["publishable"])
        content = self.ledger.export.coverage(edition["id"])
        self.assertIn("report_status", content.splitlines()[0])
        self.assertIn("applicability", content.splitlines()[0])
        bundle = self.ledger.export.bundle(edition["id"])
        self.assertTrue(self.ledger.export.verify_bundle(bundle)["ok"])
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertIn("current/scope.csv", archive.namelist())
        filtered = self.ledger.query.coverage_filter(edition["id"], report_status="not_applicable")
        self.assertEqual(filtered["total_rows"], 7)
        self.assertError("invalid_report_status", self.ledger.query.coverage_filter,
                         edition["id"], report_status="zero")

    def test_scope_ambiguity_survives_legacy_data_and_is_reported(self):
        self.declare("alpha-sz-open", "alpha", "sz", "2026-11-09", "2026-11-22", True)
        # 规则登记时会拒绝冲突；这里模拟数据库里已存在的遗留冲突，分析必须保留冲突。
        with self.ledger.database.transaction(write=True) as connection:
            payload = {"source": "alpha", "city": "sz", "start": "2026-11-12", "end": "2026-11-13",
                       "applicable": False, "status": "confirmed", "note": ""}
            from autumn_ledger.values import canonical
            revision = self.ledger.database.advance(connection, "test.inject", "alpha-sz-conflict",
                                                    "tester", payload)
            connection.execute("INSERT INTO operating_scopes VALUES (?,?,?,?,?,?,?,?)",
                               ("alpha-sz-conflict", 1, revision, "alpha", "sz", "2026-11-12",
                                "2026-11-13", canonical(payload)))
        result = self.ledger.analysis.run(declared(cities=["sz", "nj"]))
        baseline = result["periods"]["baseline"]
        self.assertEqual(baseline["coverage_summary"]["ambiguous_cells"], 2)
        rows = {(row["city"], row["day"]): row for row in baseline["coverage"]}
        self.assertEqual(rows[("sz", "2026-11-12")]["applicability"], "ambiguous")
        self.assertEqual(rows[("sz", "2026-11-12")]["report_status"], "ambiguous")
        self.assertFalse(result["quality"]["publishable"])
        codes = {issue["code"] for issue in result["quality"]["issues"]}
        self.assertIn("ambiguous_scope", codes)
        # nj 完全没有声明，仍以尚未确认为主，不与冲突混淆。
        self.assertEqual(result["periods"]["current"]["coverage_summary"]["ambiguous_cells"], 0)
        self.assertEqual(result["periods"]["current"]["coverage_summary"]["unconfirmed_cells"], 7)
