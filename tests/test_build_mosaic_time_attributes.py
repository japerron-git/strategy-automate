"""add-time-attributes: the request body, from the shapes a model export shows (a date attribute built in
Studio with day/week/month/quarter/year intervals; an integer yyyyMM attribute without)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "build-mosaic-model", "scripts"))
import build_mosaic as bm  # noqa: E402

KEY = "45C11FA478E745FEA08D781CEA190FE5"


def _attr(name, typ, smart=()):
    return {"information": {"objectId": "A-" + name, "name": name}, "keyForm": {"id": KEY},
            "forms": [{"id": KEY, "dataType": {"type": typ, "precision": 0, "scale": 0}}],
            "smartAttributes": [{"information": {"objectId": f"S-{t}", "subType": "attribute_smart",
                                                 "name": f"{name} ({t.title()})"},
                                 "smartAttributeType": f"{t}_interval", "ascending": True} for t in smart]}


class TimeAttributeBodyTests(unittest.TestCase):
    def test_new_intervals_on_a_date(self):
        body, added, problems = bm.time_attribute_body(_attr("Month Date", "date"), ["year", "quarter", "month"])
        self.assertEqual(problems, [])
        self.assertEqual(added, ["year_interval", "quarter_interval", "month_interval"])
        self.assertEqual(body["smartAttributes"][0], {"subType": "attribute_smart", "name": "Month Date (Year)",
                                                      "definition": {"smartAttributeType": "year_interval",
                                                                     "parameters": [], "ascending": True}})

    def test_existing_intervals_are_resent_with_their_ids(self):
        body, added, _ = bm.time_attribute_body(_attr("Month Date", "date", ["day", "year"]), ["year", "quarter"])
        self.assertEqual(added, ["quarter_interval"])
        self.assertEqual([(e.get("objectId"), e["name"], e["definition"]["smartAttributeType"])
                          for e in body["smartAttributes"]],
                         [("S-day", "Month Date (Day)", "day_interval"), ("S-year", "Month Date (Year)", "year_interval"),
                          (None, "Month Date (Quarter)", "quarter_interval")])

    def test_nothing_to_add(self):
        self.assertEqual(bm.time_attribute_body(_attr("D", "date", ["year"]), ["year"]), (None, [], []))

    def test_a_number_must_be_converted_first(self):
        body, _, problems = bm.time_attribute_body(_attr("Activity Month Key", "integer"), ["year"])
        self.assertIsNone(body)
        self.assertIn("convert it first", problems[0])

    def test_timestamp_is_accepted(self):
        self.assertIsNotNone(bm.time_attribute_body(_attr("T", "time_stamp"), ["day"])[0])

    def test_items_validate_intervals(self):
        class A:
            spec, attribute, intervals = None, "Month Date", "year,fortnight"
        with self.assertRaises(SystemExit):
            bm.time_attribute_items(A())


if __name__ == "__main__":
    unittest.main()
