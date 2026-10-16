from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
import zipfile

from table_qa.contracts import AnswerSpec, PipelineError, TableView
from table_qa.adapters.raw import RawWorkbookAdapter
from table_qa.contract_validation import (
    validate_answer_spec, validate_plan, validate_query, validate_query_context,
    validate_table_context, validate_table_view,
)
from table_qa.context_builder import parse_answer_spec
from table_qa.answer_spec import format_answer

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "q006"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TableContractTests(unittest.TestCase):
    def test_raw_reader_infers_missing_worksheet_dimension(self):
        path = ROOT / "data/Public data 1/xlsx_files/p4_t01.xlsx"
        if not path.exists():
            self.skipTest("Public workbook with omitted worksheet dimension is not installed")
        table = RawWorkbookAdapter().load(path)
        sheet = table.sheets[0]
        self.assertEqual(sheet.dimensions, "A1:G22")
        self.assertEqual(max(cell.row_index for row in sheet.raw_rows for cell in row.cells), 22)

    def test_q006_raw_and_parsed_agree_on_evidence_and_values(self):
        raw = validate_table_context(read("table_raw.json"))
        parsed = validate_table_context(read("table_parsed.json"))
        expected = read("expected.json")
        raw_cells = {c.cell: c for row in raw.sheets[0].raw_rows for c in row.cells}
        parsed_cells = {c.cell: c for c in parsed.sheets[0].parsed_cells}
        self.assertEqual(raw.source_hash, parsed.source_hash)
        self.assertEqual(len(raw_cells), 469)
        self.assertEqual(set(raw_cells), set(parsed_cells))
        self.assertEqual(raw_cells["A24"].value_raw, "Women")
        for index, evidence in enumerate(expected["evidence"]):
            coord = evidence["cell"]
            self.assertEqual(raw_cells[coord].value_raw, expected["values"][index])
            self.assertEqual(parsed_cells[coord].value_raw, expected["values"][index])
            self.assertEqual(parsed_cells[coord].section_path, ["Women"])
        for mode in ("raw", "parsed"):
            context = validate_query_context(read(f"query_context_{mode}.json"))
            plan = validate_plan(read(f"plan_{mode}.json"), context)
            for index, step in enumerate(plan["steps"][:2]):
                args = step["args"]
                if "cell" in args:
                    matches = [raw_cells[args["cell"]]]
                else:
                    matches = [c for c in parsed_cells.values()
                               if c.sheet == args["sheet"]
                               and c.section_path == args["section_path"]
                               and c.row_header_path == args["row_header_path"]
                               and c.column_header_path == args["column_header_path"]]
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0].cell, expected["evidence"][index]["cell"])
        # Arithmetic assertion audits the public fixture, not a production executor.
        self.assertEqual(str(abs(raw_cells["E25"].value_raw - raw_cells["E26"].value_raw)), expected["answer"])

    def test_fixture_hash_and_full_cell_coverage_match_original_workbook(self):
        path = ROOT / "data/Public data 1/xlsx_files/p4_t04.xlsx"
        if not path.exists():
            self.skipTest("Original public workbook is not installed")
        raw = read("table_raw.json")
        self.assertEqual(raw["source_hash"], sha256(path.read_bytes()).hexdigest())
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with zipfile.ZipFile(path) as z:
            sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        source_cells = {c.attrib["r"] for c in sheet.findall("s:sheetData/s:row/s:c", ns)
                        if c.get("t") in ("inlineStr", "str", "s") or c.find("s:v", ns) is not None or c.find("s:is", ns) is not None or c.find("s:f", ns) is not None}
        fixture_cells = {c["cell"] for row in raw["sheets"][0]["raw_rows"] for c in row["cells"]}
        self.assertEqual(source_cells, fixture_cells)

    def test_corrupt_provenance_unknown_fields_and_ambiguous_storage_rejected(self):
        mutations = [
            lambda s: s["raw_rows"][0]["cells"][0].update(sheet="other"),
            lambda s: s["raw_rows"][0]["cells"][0].update(row_index=2),
            lambda s: s["raw_rows"][0]["cells"].append(deepcopy(s["raw_rows"][0]["cells"][0])),
            lambda s: s.update(parsed_cells=[]),
            lambda s: s.update(dimensions="I68:A1"),
            lambda s: s.update(header_rows=[999]),
            lambda s: s["raw_rows"][0]["cells"][0].update(value_type="number"),
            lambda s: s["raw_rows"][0]["cells"][0].update(extra="unknown"),
            lambda s: s["raw_rows"][0]["cells"][0].update(merged_anchor="I68"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                table = read("table_raw.json")
                mutate(table["sheets"][0])
                with self.assertRaises(PipelineError) as caught:
                    validate_table_context(table)
                self.assertEqual(caught.exception.code, "INVALID_TABLE_CONTEXT")

    def test_mixed_modes_duplicate_sheets_and_missing_epoch_rejected(self):
        for mode in ("mixed", "duplicate", "epoch"):
            with self.subTest(mode=mode):
                table = read("table_raw.json")
                if mode == "mixed":
                    other = read("table_parsed.json")["sheets"][0]
                    other["name"] = "other"
                    for c in other["parsed_cells"]:
                        c["sheet"] = "other"
                    table["sheets"].append(other)
                elif mode == "duplicate":
                    table["sheets"].append(deepcopy(table["sheets"][0]))
                else:
                    table["metadata"].pop("date_system")
                with self.assertRaises(PipelineError):
                    validate_table_context(table)

    def test_context_hash_truncation_and_sheet_ranges(self):
        for mutate in (
            lambda q: q.update(table_view=q["table_view"] + " changed"),
            lambda q: q["context_meta"].update(truncated=True),
            lambda q: q["context_meta"].update(sheets_included=["different"]),
        ):
            with self.subTest(mutation=mutate):
                context = read("query_context_raw.json")
                mutate(context)
                with self.assertRaises(PipelineError):
                    validate_query_context(context)

    def test_table_view_contract_and_hash(self):
        q = read("query_context_raw.json")
        view = TableView(text=q["table_view"], table_view_hash=q["context_meta"]["table_view_hash"], context_meta=validate_query_context(q).context_meta)
        self.assertEqual(validate_table_view(view), view)
        view.table_view_hash = "0" * 64
        with self.assertRaises(PipelineError):
            validate_table_view(view)

    def test_query_basename_and_unknown_keys(self):
        validate_query(read("query.json"))
        for filename in ("../p4_t04.xlsx", "/tmp/table.xlsx", "x\\table.xlsx", "table.csv"):
            q = read("query.json")
            q["file_name"] = filename
            with self.subTest(filename=filename), self.assertRaises(PipelineError):
                validate_query(q)


class AnswerContractTests(unittest.TestCase):
    def spec(self, kind, **kwargs):
        return {"kind": kind, "decimals": None, "separator": None, "allowed_values": None, **kwargs}

    def test_supported_specs(self):
        specs = [self.spec("integer"), self.spec("fixed_decimal", decimals=2),
                 self.spec("yes_no", allowed_values=["Yes", "No"]), self.spec("category"),
                 self.spec("last_name"), self.spec("ordered_labels", separator=", ", item_count=3),
                 self.spec("number_pair", separator=", ", item_count=2),
                 self.spec("decimal_pair", decimals=2, separator=", ", item_count=2),
                 self.spec("generic_string")]
        for spec in specs:
            with self.subTest(kind=spec["kind"]):
                answer = validate_answer_spec(spec)
                self.assertEqual(answer, AnswerSpec.from_dict(json.loads(answer.to_json())))

    def test_unknown_private_question_uses_generic_string_fallback(self):
        spec = parse_answer_spec("Which entry best matches the requested condition?")
        self.assertEqual(spec.kind, "generic_string")
        self.assertEqual(format_answer(42, spec), "42")

    def test_explicit_format_language_is_not_guessed_into_a_semantic_kind(self):
        questions = (
            "Answer as `group, difference` using exactly two decimal places.",
            "Chỉ trả lời bằng số với đúng một chữ số thập phân.",
            "Répondez uniquement par le nombre avec une décimale.",
        )
        self.assertTrue(all(parse_answer_spec(question).kind == "generic_string"
                            for question in questions))

    def test_inconsistent_specs_rejected(self):
        for spec in (self.spec("integer", decimals=2), self.spec("fixed_decimal"),
                     self.spec("yes_no"), self.spec("number_pair", separator=", ", item_count=3),
                     self.spec("ordered_labels", separator=","), self.spec("decimal_pair", decimals=2, separator=", "),
                     self.spec("category", rounding="ROUND_HALF_EVEN")):
            with self.subTest(spec=spec), self.assertRaises(PipelineError):
                validate_answer_spec(spec)


if __name__ == "__main__":
    unittest.main()
