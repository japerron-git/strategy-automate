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
        body, _, problems = bm.time_attribute_body(_attr("Order Month Key", "integer"), ["year"])
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


class TimeAttributeNameTests(unittest.TestCase):
    def test_a_new_interval_takes_the_given_name(self):
        body, added, problems = bm.time_attribute_body(_attr("Start Date", "date", ["month"]), ["year", "month"],
                                                       {"year": "Year"}, {"start date", "start date (month)"})
        self.assertEqual((added, problems), (["year_interval"], []))
        self.assertEqual([e["name"] for e in body["smartAttributes"]], ["Start Date (Month)", "Year"])

    def test_a_name_another_attribute_holds_is_refused(self):
        taken = {"year", "start date"}
        body, _, problems = bm.time_attribute_body(_attr("Start Date", "date"), ["year"], {"year": "Year"}, taken)
        self.assertIsNone(body)
        self.assertIn("'Year' (the year time attribute of 'Start Date') is already the name", problems[0])

    def test_two_new_names_in_one_run_collide(self):
        taken = set()
        self.assertIsNotNone(bm.time_attribute_body(_attr("A", "date"), ["year"], {"year": "Year"}, taken)[0])
        self.assertIsNone(bm.time_attribute_body(_attr("B", "date"), ["year"], {"year": "Year"}, taken)[0])

    def test_an_existing_time_attribute_is_not_renamed(self):
        body, _, problems = bm.time_attribute_body(_attr("D", "date", ["year"]), ["year"], {"year": "Year"})
        self.assertIsNone(body)
        self.assertIn("isn't renamed here", problems[0])

    def test_name_option(self):
        class A:
            spec, attribute, intervals, names = None, "Start Date", "year,month", ["year=Year", "month = Month"]
        self.assertEqual(bm.time_attribute_items(A())[0]["names"], {"year": "Year", "month": "Month"})
        for bad in (["year"], ["week=Week"]):
            class B:
                spec, attribute, intervals, names = None, "Start Date", "year", bad
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                bm.time_attribute_items(B())
