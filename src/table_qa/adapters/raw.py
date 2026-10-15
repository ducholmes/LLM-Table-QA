"""Read complete XLSX worksheets using the Python standard library only.

This M1 reader supports the workbook parts needed to retain cell values,
coordinates, basic number/date display, worksheet visibility and merged ranges.
It never evaluates formulas or extracts files from the XLSX archive.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

from ..contracts import ContextMeta, PipelineError, RawCell, RawRow, SheetContext, TableContext, TableView
from ..contract_validation import cell_position, validate_table_context, validate_table_view

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
MAX_XLSX_UNCOMPRESSED = 300 * 1024 * 1024
DATE_BUILTINS = set(range(14, 18)) | {22, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 45, 46, 47}
BUILTIN_NUMBER_FORMATS = {
    0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00",
    9: "0%", 10: "0.00%", 11: "0.00E+00", 12: "# ?/?", 13: "# ??/??",
    14: "mm-dd-yy", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy",
    18: "h:mm AM/PM", 19: "h:mm:ss AM/PM", 20: "h:mm", 21: "h:mm:ss", 22: "m/d/yy h:mm",
    37: "#,##0 ;(#,##0)", 38: "#,##0 ;[Red](#,##0)",
    39: "#,##0.00;(#,##0.00)", 40: "#,##0.00;[Red](#,##0.00)",
    49: "@",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in node if _local(child.tag) == name]


def _child(node: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in node if _local(child.tag) == name), None)


def _text_runs(node: ET.Element) -> str:
    return "".join(child.text or "" for child in node.iter() if _local(child.tag) == "t")


def _sheet_path(target: str) -> str:
    resolved = posixpath.normpath(target.lstrip("/")) if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
    if resolved == ".." or resolved.startswith("../"):
        raise ValueError("Worksheet relationship escapes the XLSX package")
    return resolved


def _column_name(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _infer_dimension(root: ET.Element) -> str:
    """Infer a sheet range when the XLSX omits its optional dimension element."""
    positions: list[tuple[int, int]] = []
    for node in root.iter():
        if _local(node.tag) == "c" and node.attrib.get("r"):
            positions.append(cell_position(node.attrib["r"]))
        elif _local(node.tag) == "mergeCell":
            start, end = node.attrib["ref"].split(":")
            positions.extend((cell_position(start), cell_position(end)))
    if not positions:
        return "A1:A1"
    min_row = min(row for row, _ in positions)
    min_column = min(column for _, column in positions)
    max_row = max(row for row, _ in positions)
    max_column = max(column for _, column in positions)
    return f"{_column_name(min_column)}{min_row}:{_column_name(max_column)}{max_row}"


def _is_date_format(format_id: int, fmt: str) -> bool:
    if format_id in DATE_BUILTINS:
        return True
    # Ignore quoted/escaped literals, colors, conditions and locale annotations.
    stripped = re.sub(r'"(?:[^"]|"")*"|\\.|\[[^]]*\]', "", fmt).lower()
    return bool(re.search(r"[ymd]", stripped) and ("h" in stripped or "s" in stripped or "d" in stripped or "y" in stripped))


def _excel_datetime(serial: float, date_system: str) -> datetime | None:
    if not math.isfinite(serial):
        return None
    whole_days = math.floor(serial)
    fraction = serial - whole_days
    if date_system == "1904":
        base = datetime(1904, 1, 1)
    elif whole_days == 60:
        # Excel's 1900 calendar contains a fictitious 29 February 1900.
        return None
    else:
        base = datetime(1899, 12, 31) if whole_days < 60 else datetime(1899, 12, 30)
    return base + timedelta(days=whole_days, seconds=round(fraction * 86400))


def _display_number(value: int | float, fmt: str) -> str:
    if fmt == "General" or fmt == "@" or not fmt:
        return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
    section = fmt.split(";", 1)[1] if value < 0 and ";" in fmt else fmt.split(";", 1)[0]
    section = re.sub(r'"([^"]*)"', r"\1", section)
    section = re.sub(r"\[[^]]*\]", "", section)
    section = section.replace("\\", "")
    percent = "%" in section
    percent_count = section.count("%")
    numeric = re.sub(r"[^0#?,.]", "", section)
    decimals = len(numeric.rsplit(".", 1)[1]) if "." in numeric else 0
    scaled = value * (100 ** max(1, percent_count)) if percent else value
    grouped = "," in numeric.split(".", 1)[0]
    pattern = f"{',' if grouped else ''}.{decimals}f" if decimals else f"{',' if grouped else ''}.0f"
    shown = format(scaled, pattern)
    if value < 0 and section.startswith("(") and section.endswith(")"):
        shown = f"({shown.lstrip('-')})"
    prefix = re.match(r"^[^0#?,.]*", section)
    suffix = re.search(r"[^0#?,.]*$", section)
    return (prefix.group() if prefix else "") + shown + (suffix.group() if suffix else "")


class RawWorkbookAdapter:
    """Build a raw TableContext and a full, coordinate-preserving model view."""

    def load(self, path: Path) -> TableContext:
        try:
            path = Path(path).resolve(strict=True)
        except OSError as exc:
            raise PipelineError(code="TABLE_NOT_FOUND", stage="load", message=f"Cannot locate workbook: {exc}") from exc
        if path.suffix.lower() != ".xlsx" or not path.is_file():
            raise PipelineError(code="TABLE_NOT_FOUND", stage="load", message=f"Expected an XLSX workbook: {path.name}")
        try:
            with zipfile.ZipFile(path) as archive:
                if sum(info.file_size for info in archive.infolist()) > MAX_XLSX_UNCOMPRESSED:
                    raise PipelineError(code="RESOURCE_LIMIT", stage="load", message="XLSX uncompressed contents exceed the M1 safety limit")
                names = set(archive.namelist())
                workbook_xml = ET.fromstring(archive.read("xl/workbook.xml"))
                rels_xml = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
                rels = {r.attrib["Id"]: r.attrib for r in _children(rels_xml, "Relationship")}
                shared = self._shared_strings(archive, names)
                num_formats, xfs = self._number_formats(archive, names)
                props = self._properties(archive, names)
                workbook_pr = _child(workbook_xml, "workbookPr")
                date_system = "1904" if workbook_pr is not None and workbook_pr.attrib.get("date1904", "0").lower() in ("1", "true") else "1900"
                sheet_nodes = [node for node in workbook_xml.iter() if _local(node.tag) == "sheet"]
                sheets: list[SheetContext] = []
                warnings: list[str] = []
                for node in sheet_nodes:
                    rel_id = node.attrib.get(f"{{{DOC_REL}}}id")
                    relationship = rels.get(rel_id or "")
                    if relationship is None or relationship.get("TargetMode") == "External":
                        raise ValueError(f"Sheet has a missing or external relationship: {node.attrib.get('name')}")
                    if not relationship.get("Type", "").endswith("/worksheet"):
                        # Preserve workbook sheet identity even when it has no cell grid.
                        warnings.append(f"Non-worksheet sheet has no cell grid: {node.attrib.get('name')}")
                        sheets.append(SheetContext(name=node.attrib.get("name", ""),
                                                   visibility=node.attrib.get("state", "visible"),
                                                   dimensions="A1:A1", raw_rows=[], parsed_cells=None,
                                                   header_rows=[], sections=[], merged_ranges=[]))
                        continue
                    xml_path = _sheet_path(relationship["Target"])
                    if xml_path not in names:
                        raise ValueError(f"Worksheet XML is missing: {xml_path}")
                    sheet = self._read_sheet(archive, xml_path, node.attrib.get("name", ""), node.attrib.get("state", "visible"), shared, num_formats, xfs, date_system)
                    sheets.append(sheet)
            if not sheets:
                raise ValueError("Workbook has no worksheet sheets")
            digest = self._hash_file(path)
            table = TableContext(version="table_context_v1", table_id=path.name, source_hash=digest, sheets=sheets,
                                 metadata={"date_system": date_system, "title": props.get("title"), "creator": props.get("creator"), "warnings": warnings})
            return validate_table_context(table)
        except PipelineError:
            raise
        except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, OverflowError) as exc:
            raise PipelineError(code="INVALID_TABLE_CONTEXT", stage="load", message=f"Cannot read XLSX workbook {path.name}: {exc}") from exc

    def build_view(self, table: TableContext) -> TableView:
        """Serialize all cells and sheets. Never filter, rank or truncate content."""
        table = validate_table_context(table)
        lines = [f"TABLE: {table.table_id} | SHA256: {table.source_hash} | DATE_SYSTEM: {table.metadata['date_system']}"]
        if table.metadata.get("title"):
            lines.append("TITLE: " + json.dumps(table.metadata["title"], ensure_ascii=False))
        for sheet in table.sheets:
            lines.append(f"\nSHEET: {sheet.name} | VISIBILITY: {sheet.visibility} | DIMENSIONS: {sheet.dimensions}")
            if sheet.merged_ranges:
                lines.append("MERGED_RANGES: " + ", ".join(sheet.merged_ranges))
            rows: dict[int, list[RawCell]] = {}
            if sheet.raw_rows is not None:
                for row in sheet.raw_rows:
                    rows[row.row_index] = list(row.cells)
            else:
                for cell in sheet.parsed_cells or []:
                    rows.setdefault(cell.row_index, []).append(cell)
            for row_index, row_cells in sorted(rows.items()):
                parts = []
                for cell in sorted(row_cells, key=lambda item: item.column_index):
                    value = json.dumps(cell.value_raw, ensure_ascii=False, allow_nan=False)
                    detail = f"{cell.cell}={value} [{cell.value_type}; display={json.dumps(cell.display_value, ensure_ascii=False)}]"
                    if cell.date_iso:
                        detail += f" [ISO={cell.date_iso}]"
                    if cell.formula:
                        detail += f" [formula={json.dumps(cell.formula, ensure_ascii=False)}]"
                    if cell.merged_anchor:
                        detail += f" [merged_anchor={cell.merged_anchor}]"
                    parts.append(detail)
                lines.append(f"r{row_index}: " + "; ".join(parts))
                if sheet.parsed_cells is not None:
                    for cell in sorted((c for c in row_cells if c.section_path or c.row_header_path or c.column_header_path), key=lambda item: item.column_index):
                        lines.append("SEMANTIC " + json.dumps({"sheet": cell.sheet, "cell": cell.cell,
                                     "section_path": cell.section_path, "row_header_path": cell.row_header_path,
                                     "column_header_path": cell.column_header_path}, ensure_ascii=False))
        text = "\n".join(lines)
        view_hash = sha256(text.encode("utf-8")).hexdigest()
        meta = {"sheets_included": [sheet.name for sheet in table.sheets],
                "included_ranges": {sheet.name: [sheet.dimensions] for sheet in table.sheets},
                "table_view_hash": view_hash, "coverage": "full", "truncated": False}
        return validate_table_view(TableView(text=text, table_view_hash=view_hash, context_meta=ContextMeta.from_dict(meta)))

    @staticmethod
    def _shared_strings(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
        if "xl/sharedStrings.xml" not in names:
            return []
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        return [_text_runs(node) for node in _children(root, "si")]

    @staticmethod
    def _number_formats(archive: zipfile.ZipFile, names: set[str]) -> tuple[dict[int, str], list[int]]:
        custom: dict[int, str] = {}
        xfs: list[int] = [0]
        if "xl/styles.xml" not in names:
            return custom, xfs
        root = ET.fromstring(archive.read("xl/styles.xml"))
        formats = next((n for n in root.iter() if _local(n.tag) == "numFmts"), None)
        if formats is not None:
            custom = {int(n.attrib["numFmtId"]): n.attrib["formatCode"] for n in _children(formats, "numFmt")}
        xf_node = next((n for n in root.iter() if _local(n.tag) == "cellXfs"), None)
        if xf_node is not None:
            xfs = [int(n.attrib.get("numFmtId", "0")) for n in _children(xf_node, "xf")] or [0]
        return custom, xfs

    @staticmethod
    def _properties(archive: zipfile.ZipFile, names: set[str]) -> dict[str, str]:
        path = "docProps/core.xml"
        if path not in names:
            return {}
        root = ET.fromstring(archive.read(path))
        wanted = {"title", "creator"}
        return {_local(n.tag): (n.text or "") for n in root.iter() if _local(n.tag) in wanted}

    @classmethod
    def _read_sheet(cls, archive: zipfile.ZipFile, xml_path: str, name: str, state: str,
                    shared: list[str], custom_formats: dict[int, str], xfs: list[int], date_system: str) -> SheetContext:
        root = ET.fromstring(archive.read(xml_path))
        dimension_node = next((n for n in root.iter() if _local(n.tag) == "dimension"), None)
        dimension = dimension_node.attrib.get("ref") if dimension_node is not None else None
        if not dimension:
            dimension = _infer_dimension(root)
        if ":" not in dimension:
            dimension = f"{dimension}:{dimension}"
        bounds = dimension
        merged = [m.attrib["ref"] for m in root.iter() if _local(m.tag) == "mergeCell"]
        anchors = []
        for rng in merged:
            start, end = rng.split(":")
            r1, c1 = cell_position(start); r2, c2 = cell_position(end)
            anchors.append((rng, r1, c1, r2, c2, start))
        parsed: dict[int, list[RawCell]] = {}
        for row_node in (n for n in root.iter() if _local(n.tag) == "row"):
            row_index = int(row_node.attrib["r"])
            row_cells: list[RawCell] = []
            for node in _children(row_node, "c"):
                address = node.attrib.get("r")
                if not address:
                    raise ValueError(f"Cell without A1 reference on worksheet {name}")
                r, col = cell_position(address)
                if r != row_index:
                    raise ValueError(f"Cell coordinate disagrees with row on worksheet {name}: {address}")
                value_node, formula_node = _child(node, "v"), _child(node, "f")
                raw_text = value_node.text if value_node is not None else None
                cell_type = node.attrib.get("t", "n")
                if cell_type == "inlineStr":
                    inline = _child(node, "is")
                    value: int | float | str | bool | None = _text_runs(inline) if inline is not None else ""
                    value_type = "string"
                elif cell_type == "s":
                    if raw_text is None or not 0 <= int(raw_text) < len(shared):
                        raise ValueError(f"Invalid shared string index in {address}")
                    value, value_type = shared[int(raw_text)], "string"
                elif cell_type in ("str",):
                    value, value_type = raw_text or "", "string"
                elif cell_type == "b":
                    if raw_text not in ("0", "1"):
                        raise ValueError(f"Invalid boolean cached value in {address}")
                    value, value_type = raw_text == "1", "boolean"
                elif cell_type == "e":
                    value, value_type = raw_text, "error"
                elif raw_text is None:
                    if formula_node is None:
                        continue
                    value, value_type = None, "empty"
                else:
                    value = float(raw_text) if any(c in raw_text.lower() for c in ".e") else int(raw_text)
                    value_type = "number"
                    style = int(node.attrib.get("s", "0"))
                    fmt_id = xfs[style] if style < len(xfs) else 0
                    fmt = custom_formats.get(fmt_id, BUILTIN_NUMBER_FORMATS.get(fmt_id, "General"))
                    if _is_date_format(fmt_id, fmt):
                        value_type = "date"
                style = int(node.attrib.get("s", "0"))
                fmt_id = xfs[style] if style < len(xfs) else 0
                fmt = custom_formats.get(fmt_id, BUILTIN_NUMBER_FORMATS.get(fmt_id, "General"))
                date_iso = None
                if value_type == "date" and isinstance(value, (int, float)):
                    converted = _excel_datetime(float(value), date_system)
                    date_iso = converted.date().isoformat() if converted is not None else None
                    display = converted.isoformat(sep=" ", timespec="seconds") if converted is not None and converted.time() != time() else date_iso or str(value)
                elif value_type == "number":
                    display = _display_number(value, fmt)
                elif value_type == "boolean":
                    display = "TRUE" if value else "FALSE"
                else:
                    display = "" if value is None else str(value)
                anchor = None
                for rng, r1, c1, r2, c2, start in anchors:
                    if r1 <= r <= r2 and c1 <= col <= c2 and address != start:
                        anchor = start
                        break
                row_cells.append(RawCell(sheet=name, cell=address, row_index=r, column_index=col,
                                         value_raw=value, value_type=value_type, display_value=display,
                                         unit=None, number_format=fmt, formula=formula_node.text if formula_node is not None else None,
                                         date_iso=date_iso, merged_anchor=anchor))
            if row_cells:
                parsed.setdefault(row_index, []).extend(row_cells)
        rows = [RawRow(row_index=r, cells=sorted(cells, key=lambda c: c.column_index)) for r, cells in sorted(parsed.items())]
        return SheetContext(name=name, visibility=state if state in ("visible", "hidden", "veryHidden") else "visible",
                            dimensions=bounds, raw_rows=rows, parsed_cells=None, header_rows=[], sections=[], merged_ranges=merged)

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
