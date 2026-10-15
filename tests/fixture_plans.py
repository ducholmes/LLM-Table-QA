"""Hand-authored, offline-only execution plans for the 20 public queries."""

from __future__ import annotations

from typing import Any


def plan_for(query_id: str, table_id: str) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []

    def add(op: str, args: dict[str, Any]) -> str:
        step_id = f"s{len(steps) + 1}"
        steps.append({"id": step_id, "op": op, "args": args})
        return f"${step_id}"

    def cell(coordinate: str, sheet: str = "Sheet1") -> str:
        return add("lookup_cell", {"sheet": sheet, "cell": coordinate})

    def collect(items: list[Any]) -> str:
        return add("collect", {"items": items})

    def arithmetic(op: str, left: str, right: str) -> str:
        return add(op, {"left": left, "right": right})

    def finish(result: str) -> dict[str, Any]:
        return {"version": "plan_v1", "query_id": query_id, "table_id": table_id,
                "steps": steps, "return": {"ref": result}}

    if query_id in ("p4_q001", "p4_q002"):
        source = add("select_records", {"sheet": "People", "row_range": "3:22", "fields": {
            "last_name": {"column": "D", "as_type": "string"},
            "birth_date": {"column": "E", "as_type": "date"},
            "admission_date": {"column": "F", "as_type": "date"},
            "score": {"column": "G", "as_type": "number"},
        }})
        ages = add("derive", {"source": source, "field": "age_days", "fn": "date_diff_days",
                               "inputs": ["admission_date", "birth_date"]})
        order = "asc" if query_id == "p4_q001" else "desc"
        ordered = add("sort", {"source": ages, "by": "age_days", "order": order})
        selected = add("take", {"source": ordered, "offset": 1, "count": 1})
        result_field = "last_name" if query_id == "p4_q001" else "score"
        return finish(add("project", {"source": selected, "field": result_field, "single": True}))

    if query_id == "p4_q003":
        source = add("select_records", {"sheet": "People", "row_range": "3:22", "fields": {
            "birth_date": {"column": "E", "as_type": "date"},
            "score": {"column": "G", "as_type": "number"},
        }})
        ordered = add("sort", {"source": source, "by": "birth_date", "order": "desc"})
        youngest = add("take", {"source": ordered, "offset": 0, "count": 10})
        highest = add("argmax", {"source": youngest, "by": "score"})
        return finish(add("project", {"source": highest, "field": "score", "single": True}))

    if query_id == "p4_q004":
        old, new = cell("F4", "Monsumtable"), cell("G4", "Monsumtable")
        return finish(add("compare", {"left": new, "right": old, "predicate": "gt"}))

    if query_id == "p4_q005":
        candidates: list[dict[str, str]] = []
        for start, end in ((4, 13), (15, 22), (24, 42)):
            source = add("select_records", {"sheet": "Sheet0", "row_range": f"{start}:{end}", "fields": {
                "category": {"column": "B", "as_type": "string"},
                "percent_change": {"column": "H", "as_type": "number"},
            }})
            magnitudes = add("derive", {"source": source, "field": "magnitude", "fn": "absolute",
                                         "inputs": ["percent_change"]})
            maximum = add("argmax", {"source": magnitudes, "by": "magnitude"})
            label = add("project", {"source": maximum, "field": "category", "single": True})
            magnitude = add("project", {"source": maximum, "field": "magnitude", "single": True})
            candidates.append({"category": label, "magnitude": magnitude})
        combined = collect(candidates)
        highest = add("argmax", {"source": combined, "by": "magnitude"})
        return finish(add("project", {"source": highest, "field": "category", "single": True}))

    if query_id == "p4_q006":
        left, right = cell("E25", "cpsaat07"), cell("E26", "cpsaat07")
        return finish(arithmetic("abs_diff", left, right))

    if query_id == "p4_q007":
        values = [cell(f"{column}31", "cpsaat07") for column in "BCDEFGHI"]
        return finish(add("aggregate", {"source": collect(values), "fn": "mean"}))

    if query_id == "p4_q008":
        fields = {"age": {"column": "A", "as_type": "string"}}
        fields.update({f"i{index}": {"column": chr(ord("B") + index - 1), "as_type": "number"}
                       for index in range(1, 13)})
        rows = add("select_records", {"sheet": "cpsaat14", "row_range": "9:11", "fields": fields})
        # Sum each row across the 12 industry columns using binary derive steps.
        for index in range(1, 7):
            rows = add("derive", {"source": rows, "field": f"pair{index}", "fn": "add",
                                  "inputs": [f"i{2 * index - 1}", f"i{2 * index}"]})
        for field_name, left_name, right_name in (
            ("quarter1", "pair1", "pair2"), ("quarter2", "pair3", "pair4"),
            ("quarter3", "pair5", "pair6"), ("half1", "quarter1", "quarter2"),
            ("total", "half1", "quarter3"),
        ):
            rows = add("derive", {"source": rows, "field": field_name, "fn": "add",
                                  "inputs": [left_name, right_name]})
        young = add("filter", {"source": rows, "predicates": [
            {"field": "age", "predicate": "eq", "value": "16 to 19 years"}]})
        twenty_four = add("filter", {"source": rows, "predicates": [
            {"field": "age", "predicate": "eq", "value": "20 to 24 years"}]})
        total_young = add("aggregate", {"source": young, "fn": "sum", "field": "total"})
        total_20_24 = add("aggregate", {"source": twenty_four, "fn": "sum", "field": "total"})
        return finish(arithmetic("subtract", total_20_24, total_young))

    if query_id == "p4_q009":
        construction, manufacturing = cell("C11", "cpsaat14"), cell("D11", "cpsaat14")
        return finish(arithmetic("add", construction, manufacturing))

    if query_id == "p4_q010":
        industries = [
            ("Mining, quarrying, and oil and gas extraction", "B9"), ("Construction", "C9"),
            ("Manufacturing", "D9"), ("Wholesale and retail trade", "E9"),
            ("Transportation and utilities", "F9"), ("Information", "G9"),
            ("Financial activities", "H9"), ("Professional and business services", "I9"),
            ("Education and health services", "J9"), ("Leisure and hospitality", "K9"),
            ("Other services", "L9"), ("Public administration", "M9"),
        ]
        items = [{"industry": {"literal": name}, "workers": cell(coordinate, "cpsaat14")}
                 for name, coordinate in industries]
        records = collect(items)
        highest = add("sort", {"source": records, "by": "workers", "order": "desc"})
        first = add("take", {"source": highest, "offset": 0, "count": 1})
        return finish(add("project", {"source": first, "field": "industry", "single": True}))

    if query_id == "p4_q011":
        transport, finance = cell("F10", "cpsaat14"), cell("H10", "cpsaat14")
        return finish(add("compare", {"left": finance, "right": transport, "predicate": "gt"}))

    if query_id == "p4_q012":
        cells = [cell(f"E{row}", "cpsaat15") for row in (8, 11, 12, 13, 14, 15, 16)]
        return finish(add("aggregate", {"source": collect(cells), "fn": "sum"}))

    if query_id == "p4_q013":
        cells = [cell(coordinate, "cpsaat17") for coordinate in ("M9", "M12", "M11")]
        records = collect([
            {"sector": {"literal": "Agriculture"}, "workers": cells[0]},
            {"sector": {"literal": "Manufacturing"}, "workers": cells[1]},
            {"sector": {"literal": "Construction"}, "workers": cells[2]},
        ])
        ordered = add("sort", {"source": records, "by": "workers", "order": "desc"})
        return finish(add("project", {"source": ordered, "field": "sector"}))

    if query_id == "p4_q014":
        protective, other_service = cell("E12", "cpsaat17"), cell("F12", "cpsaat17")
        return finish(arithmetic("add", protective, other_service))

    if query_id == "p4_q015":
        return finish(cell("C8", "cpsaat19"))

    if query_id == "p4_q016":
        source = add("select_records", {"sheet": "cpsaat19", "row_range": "6:17", "fields": {
            "category": {"column": "A", "as_type": "string"},
            "agriculture_percent": {"column": "F", "as_type": "number"},
        }})
        highest = add("argmax", {"source": source, "by": "agriculture_percent"})
        return finish(add("project", {"source": highest, "field": "category", "single": True}))

    if query_id == "p4_q017":
        construction, manufacturing = cell("B10", "cpsaat21"), cell("B11", "cpsaat21")
        return finish(arithmetic("abs_diff", construction, manufacturing))

    if query_id == "p4_q018":
        short_hours, long_hours = cell("C10", "cpsaat22"), cell("G10", "cpsaat22")
        return finish(collect([short_hours, long_hours]))

    if query_id == "p4_q019":
        men, women = cell("H57", "cpsaat22"), cell("H61", "cpsaat22")
        return finish(collect([men, women]))

    if query_id == "p4_q020":
        return finish(cell("B10", "cpsaat22"))

    raise KeyError(f"No offline fixture plan for {query_id}")
