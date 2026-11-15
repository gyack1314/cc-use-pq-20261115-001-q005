import json

from ..errors import LedgerError, conflict, missing, require
from ..values import boolean, canonical, date_range, fields, fingerprint, identifier, integer

REPORT_STATUSES = ("observed", "confirmed_zero", "partial", "missing",
                   "not_applicable", "unconfirmed", "ambiguous")
SCOPE_STATUSES = {"confirmed", "withdrawn"}


def validate_payload(value):
    fields(value, {"source", "city", "start", "end", "applicable", "status", "note"},
           {"source", "city", "start", "end"})
    source = identifier(value["source"], "source")
    city = identifier(value["city"], "city")
    days = date_range(value["start"], value["end"], 366)
    applicable = boolean(value.get("applicable", True), "applicable")
    status = value.get("status", "confirmed")
    require(status in SCOPE_STATUSES, "invalid_status", "经营范围状态必须是confirmed/withdrawn")
    note = value.get("note", "")
    require(isinstance(note, str) and len(note) <= 500, "invalid_note", "经营范围说明过长")
    return {"source": source, "city": city,
            "start": days[0].isoformat(), "end": days[-1].isoformat(),
            "applicable": applicable, "status": status, "note": note}


def covers(rule, day):
    return rule["start"] <= day <= rule["end"]


def ranges_overlap(left, right):
    return max(left["start"], right["start"]) <= min(left["end"], right["end"])


def classify(rules, day):
    """Classify one source/city/day cell from confirmed rules.

    Undeclared cells are ``unconfirmed`` - never silently ``not_applicable``.
    Contradictory confirmed rules stay ``ambiguous`` instead of picking a winner.
    """
    applicability = None
    matched = []
    for rule in rules:
        if not covers(rule, day):
            continue
        value = "applicable" if rule["applicable"] else "not_applicable"
        if applicability is not None and applicability != value:
            applicability = "ambiguous"
        elif applicability is None:
            applicability = value
        matched.append(rule)
    versions = sorted({(rule["id"], rule["version"]) for rule in matched})
    return {"applicability": applicability or "unconfirmed",
            "scope_ids": sorted(rule["id"] for rule in matched),
            "scope_versions": versions}


def report_status(applicability, coverage_status, orders):
    if applicability == "ambiguous":
        return "ambiguous"
    if applicability == "not_applicable":
        return "not_applicable"
    if applicability == "unconfirmed":
        return "unconfirmed"
    if coverage_status == "partial":
        return "partial"
    if coverage_status == "missing":
        return "missing"
    # 来源声明完整不等于没有订单：有订单是 observed，无订单才是确认零。
    return "observed" if orders else "confirmed_zero"


class OperatingScopes:
    def __init__(self, database, catalog):
        self.database = database
        self.catalog = catalog

    def put(self, identity, value, expected_version=0, actor="operator"):
        identifier(identity)
        integer(expected_version, "expected_version")
        payload = validate_payload(value)
        with self.database.transaction(write=True) as connection:
            self.catalog.get("source", payload["source"], connection=connection)
            self.catalog.get("city", payload["city"], connection=connection)
            current = self.get(identity, connection=connection, required=False)
            version = current["version"] if current else 0
            if version != expected_version:
                raise conflict("经营范围版本已变化", {"current_version": version})
            if current:
                require(current["source"] == payload["source"], "scope_source_change",
                        "经营范围不能更换所属来源")
                require(current["city"] == payload["city"], "scope_city_change",
                        "经营范围不能更换所属城市")
            if payload["status"] == "confirmed":
                for other in self.list(payload["source"], payload["city"], connection=connection):
                    if other["id"] == identity or other["status"] != "confirmed":
                        continue
                    if not ranges_overlap(other, payload):
                        continue
                    if other["applicable"] == payload["applicable"]:
                        continue
                    raise LedgerError(
                        "scope_ambiguity",
                        "经营范围与其他有效规则重叠且无法唯一解释，请先修订旧规则",
                        409,
                        {"source": payload["source"], "city": payload["city"],
                         "overlapping_rule": other["id"], "overlapping_version": other["version"],
                         "overlap_start": max(other["start"], payload["start"]),
                         "overlap_end": min(other["end"], payload["end"])},
                    )
            if current and all(current.get(key) == val for key, val in payload.items()):
                return current
            revision = self.database.advance(connection, "operating_scope.put", identity, actor, payload)
            connection.execute("INSERT INTO operating_scopes VALUES (?,?,?,?,?,?,?,?)",
                               (identity, version + 1, revision, payload["source"], payload["city"],
                                payload["start"], payload["end"], canonical(payload)))
            return {**payload, "id": identity, "version": version + 1, "revision": revision}

    def get(self, identity, revision=None, connection=None, required=True):
        identifier(identity)
        if connection is None:
            with self.database.transaction() as opened:
                return self.get(identity, revision, opened, required)
        revision = self.database.snapshot_revision(connection, revision)
        row = connection.execute(
            "SELECT * FROM operating_scopes WHERE id=? AND revision<=? ORDER BY version DESC LIMIT 1",
            (identity, revision),
        ).fetchone()
        if row is None:
            if required:
                raise missing("经营范围", identity)
            return None
        return {**json.loads(row["payload"]), "id": identity,
                "version": row["version"], "revision": row["revision"]}

    def list(self, source=None, city=None, revision=None, connection=None):
        if connection is None:
            with self.database.transaction() as opened:
                return self.list(source, city, revision, opened)
        revision = self.database.snapshot_revision(connection, revision)
        parameters = [revision]
        clauses = []
        if source is not None:
            identifier(source, "source")
            clauses.append("source=?")
            parameters.append(source)
        if city is not None:
            identifier(city, "city")
            clauses.append("city=?")
            parameters.append(city)
        clause = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = connection.execute(
            "SELECT s.* FROM operating_scopes s JOIN (SELECT id,MAX(version) v FROM operating_scopes "
            "WHERE revision<=? GROUP BY id) h ON s.id=h.id AND s.version=h.v" + clause
            + " ORDER BY s.source,s.city,s.start_day,s.id",
            parameters,
        ).fetchall()
        return [{**json.loads(row["payload"]), "id": row["id"],
                 "version": row["version"], "revision": row["revision"]} for row in rows]

    def confirmed(self, source, city, revision=None, connection=None):
        return [rule for rule in self.list(source, city, revision, connection)
                if rule["status"] == "confirmed"]

    def history(self, identity):
        identifier(identity)
        self.get(identity)
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM operating_scopes WHERE id=? ORDER BY version", (identity,)
            ).fetchall()
        previous = None
        changes = []
        for row in rows:
            payload = json.loads(row["payload"])
            altered = []
            if previous is not None:
                altered = sorted(key for key in payload if payload[key] != previous.get(key))
            changes.append({"version": row["version"], "revision": row["revision"], "value": payload,
                            "changed_fields": altered, "initial": previous is None})
            previous = payload
        return {"id": identity, "history": changes}

    def matrix(self, sources, cities, start, end, revision=None, connection=None):
        """Classify every source/city/day cell; never invents an unstated scope."""
        if connection is None:
            with self.database.transaction() as opened:
                return self.matrix(sources, cities, start, end, revision, opened)
        revision = self.database.snapshot_revision(connection, revision)
        days = [day.isoformat() for day in date_range(start, end)]
        rules = {(source, city): self.confirmed(source, city, revision, connection)
                 for source in sorted(sources) for city in sorted(cities)}
        result = []
        for source in sorted(sources):
            for city in sorted(cities):
                for day in days:
                    result.append({"source": source, "city": city, "day": day,
                                   **classify(rules[(source, city)], day)})
        return result

    def snapshot(self, sources, cities, revision, connection):
        """Confirmed rules and their full version chains for the requested sources/cities.

        The snapshot is window-independent so the same analysis revision describes
        both periods identically; window effects are derived from rule ranges.
        """
        rules = []
        versions = []
        for source in sorted(sources):
            for city in sorted(cities):
                for rule in self.confirmed(source, city, revision, connection):
                    rules.append({key: rule[key] for key in
                                  ("id", "version", "source", "city", "start", "end",
                                   "applicable", "status", "note")})
                versions += self.versions(source, city, revision, connection)
        rules.sort(key=lambda row: (row["source"], row["city"], row["start"], row["id"]))
        versions.sort(key=lambda row: (row["source"], row["city"], row["id"], row["version"]))
        return {"rules": rules, "versions": versions,
                "digest": fingerprint(rules), "rule_count": len(rules)}

    def versions(self, source, city, revision=None, connection=None):
        """Every recorded version of a source/city rule up to the revision."""
        if connection is None:
            with self.database.transaction() as opened:
                return self.versions(source, city, revision, opened)
        revision = self.database.snapshot_revision(connection, revision)
        identifier(source, "source")
        identifier(city, "city")
        rows = connection.execute(
            "SELECT * FROM operating_scopes WHERE source=? AND city=? AND revision<=? ORDER BY id,version",
            (source, city, revision),
        ).fetchall()
        return [{**json.loads(row["payload"]), "id": row["id"],
                 "version": row["version"], "revision": row["revision"]} for row in rows]

    def annotate(self, scope_cells, coverage_cells, order_counts):
        """Join declared scope with registered coverage; keep every interpretation visible."""
        coverage = {(cell["source"], cell["city"], cell["day"]): cell for cell in coverage_cells}
        result = []
        for scope in scope_cells:
            key = (scope["source"], scope["city"], scope["day"])
            coverage_cell = coverage.get(key) or {
                "source": key[0], "city": key[1], "day": key[2], "version": 0, "revision": 0,
                "status": "missing", "note": "尚未登记覆盖"}
            count = order_counts.get(key, 0)
            result.append({
                **coverage_cell,
                "applicability": scope["applicability"],
                "scope_ids": scope["scope_ids"],
                "scope_versions": scope["scope_versions"],
                "orders": count,
                "report_status": report_status(scope["applicability"], coverage_cell["status"], count),
            })
        return result

    def summary(self, cells):
        applicable = [cell for cell in cells if cell["applicability"] == "applicable"]
        complete = sum(cell["status"] == "complete" for cell in applicable)
        partial = sum(cell["status"] == "partial" for cell in applicable)
        missing = sum(cell["status"] == "missing" for cell in applicable)
        counts = {status: sum(cell["report_status"] == status for cell in cells)
                  for status in REPORT_STATUSES}
        return {"cells": len(cells), "expected_cells": len(applicable),
                "applicable_cells": len(applicable),
                "not_applicable_cells": sum(cell["applicability"] == "not_applicable" for cell in cells),
                "unconfirmed_cells": sum(cell["applicability"] == "unconfirmed" for cell in cells),
                "ambiguous_cells": sum(cell["applicability"] == "ambiguous" for cell in cells),
                "complete_cells": complete, "partial_cells": partial, "missing_cells": missing,
                "report_cells": counts,
                "complete_fraction": complete / len(applicable) if applicable else None,
                "fully_observed": bool(applicable) and complete == len(applicable),
                "fully_declared": not any(cell["applicability"] in {"unconfirmed", "ambiguous"}
                                          for cell in cells)}

    def gaps(self, cells):
        groups = {}
        for cell in cells:
            if cell["report_status"] in {"observed", "confirmed_zero", "not_applicable"}:
                continue
            key = (cell["source"], cell["city"])
            groups.setdefault(key, []).append(cell)
        return [{"source": key[0], "city": key[1],
                 "days": [{"day": cell["day"], "status": cell["status"],
                           "report_status": cell["report_status"],
                           "applicability": cell["applicability"],
                           "scope_ids": cell["scope_ids"], "note": cell["note"]}
                          for cell in sorted(items, key=lambda row: row["day"])]}
                for key, items in sorted(groups.items())]
