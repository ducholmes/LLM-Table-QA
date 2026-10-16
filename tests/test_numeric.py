from decimal import Decimal
import unittest

from table_qa.numeric import MissingNumericValue, excel_percent_count, parse_numeric_text


class NumericParsingTests(unittest.TestCase):
    def test_common_spreadsheet_number_formats(self):
        cases = {
            "1,882": "1882",
            "$1,882": "1882",
            "USD $1,882.50": "1882.50",
            "1 882.50": "1882.50",
            "(1,250)": "-1250",
            "18.5%": "18.5",
            "−42": "-42",
            ".75": ".75",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_numeric_text(text), Decimal(expected))

    def test_missing_and_malformed_values_are_distinct(self):
        for text in ("", "-", "--", "..", "...", "N/A", "NA", "—"):
            with self.subTest(text=text), self.assertRaises(MissingNumericValue):
                parse_numeric_text(text)
        for text in ("1,23", "1.234,50", "12%3", "(12", "abc", "1 22"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_numeric_text(text)

    def test_excel_percent_tokens_ignore_literals(self):
        self.assertEqual(excel_percent_count("0.00%"), 1)
        self.assertEqual(excel_percent_count('0.00"%"'), 0)
        self.assertEqual(excel_percent_count(r"0.00\%"), 0)


if __name__ == "__main__":
    unittest.main()
