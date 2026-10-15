"""Generate isolated synthetic workbook/query splits for prompt experiments."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
import json
import random
from pathlib import Path
import sys
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from .query_loader import load_queries


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_ABSOLUTE_CHANGES = (5, 9, 13, 17, 22, 29, 38, 47, 59, 71)


@dataclass(frozen=True)
class SyntheticSplit:
    name: str
    queries_path: Path
    data_dir: Path
    labels_path: Path
    table_count: int
    query_count: int


def _xml_bytes(node: ET.Element) -> bytes:
    return ET.tostring(node, encoding="utf-8", xml_declaration=True)


def _write_workbook(path: Path, rows: list[list[str | int]]) -> None:
    ET.register_namespace("", MAIN_NS)
    ET.register_namespace("r", DOC_REL_NS)
    content_types = ET.Element(f"{{{CONTENT_NS}}}Types")
    ET.SubElement(content_types, f"{{{CONTENT_NS}}}Default", {
        "Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"})
    ET.SubElement(content_types, f"{{{CONTENT_NS}}}Default", {"Extension": "xml", "ContentType": "application/xml"})
    ET.SubElement(content_types, f"{{{CONTENT_NS}}}Override", {
        "PartName": "/xl/workbook.xml",
        "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"})
    ET.SubElement(content_types, f"{{{CONTENT_NS}}}Override", {
        "PartName": "/xl/worksheets/sheet1.xml",
        "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"})

    package_rels = ET.Element(f"{{{PKG_REL_NS}}}Relationships")
    ET.SubElement(package_rels, f"{{{PKG_REL_NS}}}Relationship", {
        "Id": "rId1", "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
        "Target": "xl/workbook.xml"})

    workbook = ET.Element(f"{{{MAIN_NS}}}workbook")
    sheets = ET.SubElement(workbook, f"{{{MAIN_NS}}}sheets")
    ET.SubElement(sheets, f"{{{MAIN_NS}}}sheet", {"name": "Metrics", "sheetId": "1",
                                                  f"{{{DOC_REL_NS}}}id": "rId1"})
    workbook_rels = ET.Element(f"{{{PKG_REL_NS}}}Relationships")
    ET.SubElement(workbook_rels, f"{{{PKG_REL_NS}}}Relationship", {
        "Id": "rId1", "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
        "Target": "worksheets/sheet1.xml"})

    worksheet = ET.Element(f"{{{MAIN_NS}}}worksheet")
    ET.SubElement(worksheet, f"{{{MAIN_NS}}}dimension", {"ref": f"A1:C{len(rows)}"})
    sheet_data = ET.SubElement(worksheet, f"{{{MAIN_NS}}}sheetData")
    for row_index, values in enumerate(rows, start=1):
        row = ET.SubElement(sheet_data, f"{{{MAIN_NS}}}row", {"r": str(row_index)})
        for col_index, value in enumerate(values, start=1):
            column = chr(ord("A") + col_index - 1)
            cell = ET.SubElement(row, f"{{{MAIN_NS}}}c", {"r": f"{column}{row_index}"})
            if isinstance(value, str):
                cell.set("t", "inlineStr")
                inline = ET.SubElement(cell, f"{{{MAIN_NS}}}is")
                ET.SubElement(inline, f"{{{MAIN_NS}}}t").text = value
            else:
                ET.SubElement(cell, f"{{{MAIN_NS}}}v").text = str(value)

    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xml_bytes(content_types))
        archive.writestr("_rels/.rels", _xml_bytes(package_rels))
        archive.writestr("xl/workbook.xml", _xml_bytes(workbook))
        archive.writestr("xl/_rels/workbook.xml.rels", _xml_bytes(workbook_rels))
        archive.writestr("xl/worksheets/sheet1.xml", _xml_bytes(worksheet))


def _table_values(rng: random.Random) -> tuple[list[str], list[int], list[int]]:
    for _ in range(1000):
        bases = [-rng.randint(35, 80), rng.randint(25, 50), rng.randint(65, 105), rng.randint(120, 160)]
        delta_one = rng.choice(_ABSOLUTE_CHANGES)
        delta_two = -bases[1]  # Include a zero-valued observation in the next-year column.
        candidates = [change for change in _ABSOLUTE_CHANGES if change not in {delta_one, abs(delta_two)}]
        delta_three = rng.choice(candidates) * rng.choice((-1, 1))
        changes = [delta_one, delta_two, delta_three, 0]
        if len({abs(change) for change in changes}) != len(changes):
            continue
        percentage_magnitudes = {round(abs(change / base), 10) for change, base in zip(changes, bases)}
        if len(percentage_magnitudes) != len(changes):
            continue
        regions = ["Sector A", "Sector B", "Sector C", "Sector D"]
        values_2025 = [base + change for base, change in zip(bases, changes)]
        return regions, bases, values_2025
    raise RuntimeError("Could not generate a synthetic table without tied rankings")


def _answers_from_rows(rows: list[list[str | int]]) -> dict[str, str]:
    """Small label oracle used by generation and metamorphic data checks."""
    if not rows or rows[0] != ["Sector", "2024", "2025"]:
        raise ValueError("Synthetic table must have Sector, 2024, 2025 columns")
    candidates = [row for row in rows[1:] if row and str(row[0]).strip().casefold() != "total"]
    if not candidates:
        raise ValueError("Synthetic table needs at least one non-TOTAL data row")
    if any(len(row) != 3 or type(row[1]) is not int or type(row[2]) is not int for row in candidates):
        raise ValueError("Synthetic data rows must contain a name and two integer values")
    old_values = [row[1] for row in candidates]
    new_values = [row[2] for row in candidates]
    changes = [new - old for old, new in zip(old_values, new_values)]
    percentage_magnitudes = [abs(change / old) for change, old in zip(changes, old_values)]
    for values in (list(map(abs, changes)), percentage_magnitudes):
        maximum = max(values)
        if values.count(maximum) != 1:
            raise ValueError("Synthetic ranking oracle rejects tied winners")
    return {
        "q01": candidates[max(range(len(candidates)), key=lambda index: abs(changes[index]))][0],
        "q02": candidates[max(range(len(candidates)), key=lambda index: percentage_magnitudes[index])][0],
        "q03": str(sum(new_values)),
        "q04": "Yes" if sum(new_values) > sum(old_values) else "No",
    }


def generate_synthetic_split(root: Path, *, name: str, tables: int, seed: int) -> SyntheticSplit:
    if name not in {"dev", "holdout"}:
        raise ValueError("Synthetic split name must be dev or holdout")
    if type(tables) is not int or tables < 1:
        raise ValueError("tables must be a positive integer")
    root = Path(root)
    data_dir = root / "xlsx_files"
    queries: list[dict[str, str]] = []
    labels: list[dict[str, str]] = []
    for table_index in range(tables):
        rng = random.Random(seed + table_index * 104729 + (0 if name == "dev" else 1_000_003))
        sectors, values_2024, values_2025 = _table_values(rng)
        filename = f"synthetic_{name}_t{table_index:02}.xlsx"
        workbook_rows: list[list[str | int]] = [["Sector", "2024", "2025"]]
        workbook_rows.extend([[sector, value_2024, value_2025]
                              for sector, value_2024, value_2025 in zip(sectors, values_2024, values_2025)])
        _write_workbook(data_dir / filename, workbook_rows)

        answer_by_suffix = _answers_from_rows(workbook_rows)
        table_queries = [
            ("q01", "Which sector name had the largest absolute change from 2024 to 2025? Answer with only the sector name.", answer_by_suffix["q01"]),
            ("q02", "Which sector name had the largest absolute percentage change from 2024 to 2025? Answer with only the sector name.", answer_by_suffix["q02"]),
            ("q03", "What was the total value for 2025? Answer with only the integer total.", answer_by_suffix["q03"]),
            ("q04", "Was the total for 2025 greater than the total for 2024? Answer with only Yes or No.", answer_by_suffix["q04"]),
        ]
        for suffix, question, answer in table_queries:
            query_id = f"syn_{name}_t{table_index:02}_{suffix}"
            queries.append({"id": query_id, "file_name": filename, "query": question})
            labels.append({"id": query_id, "answer": answer})

    root.mkdir(parents=True, exist_ok=True)
    queries_path = root / "queries.json"
    queries_path.write_text(json.dumps(queries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    labels_path = root / "labels.csv"
    with labels_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["id", "answer"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(labels)

    # Validate generated inputs with the same boundaries used by normal runs.
    loaded = load_queries(queries_path, data_dir)
    if len(loaded) != len(queries):
        raise RuntimeError("Generated synthetic query count did not round-trip")
    return SyntheticSplit(name=name, queries_path=queries_path, data_dir=data_dir,
                          labels_path=labels_path, table_count=tables, query_count=len(queries))


def generate_synthetic_corpus(root: Path, *, dev_tables: int = 4, holdout_tables: int = 4,
                              seed: int = 20261015) -> dict[str, SyntheticSplit]:
    return {
        "dev": generate_synthetic_split(Path(root) / "dev", name="dev", tables=dev_tables, seed=seed),
        "holdout": generate_synthetic_split(Path(root) / "holdout", name="holdout",
                                             tables=holdout_tables, seed=seed),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate independent synthetic Table QA dev/holdout splits")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dev-tables", type=int, default=4)
    parser.add_argument("--holdout-tables", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20261015)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        splits = generate_synthetic_corpus(args.output_dir, dev_tables=args.dev_tables,
                                           holdout_tables=args.holdout_tables, seed=args.seed)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"SYNTHETIC_DATA_ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({name: {**asdict(split), "queries_path": str(split.queries_path),
                             "data_dir": str(split.data_dir), "labels_path": str(split.labels_path)}
                      for name, split in splits.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
