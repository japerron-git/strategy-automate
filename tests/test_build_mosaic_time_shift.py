"""Tests for create-time-shift-metric: a Mosaic time-shift derived metric, Agg(PreviousMonth({Date} (ID), n, M)).

The token builder is pinned to the sequence a Studio-built PreviousMonth metric stores (captured 2026-11-17,
memory/reference_mosaic_derived_metrics.md §0e). A fake client stands in for MSTR, so nothing touches the network.
"""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _hermetic  # noqa: E402,F401  (scrub MSTR_*/proxy env, private secret store)

import contextlib
import io
import json
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "skills", "build-mosaic-model", "scripts"))

import build_mosaic as bm  # noqa: E402
from test_build_mosaic_conditional_metric import _FakeMSTR, _Resp  # noqa: E402

MP = "/api/model/dataModels/M1"
DATE = {"objectId": "A" * 32, "subType": "attribute", "name": "Order Date"}
SALES = {"objectId": "B" * 32, "subType": "fact_metric", "name": "Sales"}
NEW_ID = "C" * 32
EXISTING_ID = "D" * 32
FMT = [{"type": "number_category", "value": "0"}]
ALL_INTERVALS = ["day_interval", "week_interval", "month_interval", "quarter_interval", "year_interval"]


def _routes(intervals=ALL_INTERVALS, derived=(), read_back="Sum(PreviousMonth({Order Date} (ID),1,Sales))",
            write_status=201):
    return {
        ("GET", f"{MP}/attributes"): _Resp(200, {"attributes": [{"information": dict(DATE)}]}),
        ("GET", f"{MP}/attributes/{DATE['objectId']}"): _Resp(200, {"smartAttributes": [
            {"objectId": f"{i:032d}", "definition": {"smartAttributeType": t}} for i, t in enumerate(intervals)]}),
        ("GET", f"{MP}/factMetrics"): _Resp(200, {"factMetrics": [{"information": dict(SALES)}]}),
        ("GET", f"{MP}/metrics"): _Resp(200, {"metrics": [{"information": dict(d)} for d in derived]}),
        ("GET", f"{MP}/factMetrics/{SALES['objectId']}"): _Resp(200, {"format": {"header": [], "values": FMT}}),
        ("GET", f"{MP}/metrics/{EXISTING_ID}"): _Resp(200, {
            "information": {"objectId": EXISTING_ID, "name": "Sales (previous month)", "description": "Kept"},
            "format": {"header": [], "values": [{"type": "number_category", "value": "9"}]},
            "expression": {"text": read_back}}),
        ("GET", f"{MP}/metrics/{NEW_ID}"): _Resp(200, {"expression": {"text": read_back}}),
        ("POST", "/api/model/changesets"): _Resp(201, {"id": "CS1"}),
        ("POST", f"{MP}/metrics"): _Resp(write_status, {"information": {"objectId": NEW_ID}}),
        ("PUT", f"{MP}/metrics/{EXISTING_ID}"): _Resp(200, {}),
        ("POST", "/api/model/changesets/CS1/commit"): _Resp(204),
        ("DELETE", "/api/model/changesets/CS1"): _Resp(204),
    }


def _args(**overrides):
    base = {"model_id": "M1", "name": "Sales (previous month)", "function": "PreviousMonth",
            "time_attribute": "order date", "periods": None, "source_metric": "sales", "aggregation": None,
            "format": None, "description": "", "replace": False, "unverified_ok": False, "dry_run": False}
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _writes(fake):
    return [(method, path) for method, path, _, _ in fake.calls if method != "GET"]


def _run(fake, **overrides):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        bm.cmd_create_time_shift_metric(fake, _args(**overrides))
    return json.loads(out.getvalue())


class TokenTests(unittest.TestCase):
    def test_tokens_match_the_studio_built_sequence(self):
        tokens = bm.time_shift_tokens("PreviousMonth", DATE, 1, SALES)
        self.assertEqual([t["value"] for t in tokens], [
            "Sum", "<", "UseLookupForAttributes", "=", "False", ">", "(", "PreviousMonth", "(",
            "[Order Date]@ID", ",", "1", ",", "Sales", ")", "{", "~", "+", "}", ")", "{", "~", "+", "}", ""])
        self.assertEqual([t["type"] for t in tokens][7:14], [
            "function", "character", "object_at_form", "character", "integer", "character", "object_reference"])
        # platform function objectIds, the same on every tenant
        self.assertEqual(tokens[0]["target"]["objectId"], "8107C31BDD9911D3B98100C04F2233EA")
        self.assertEqual(tokens[7]["target"]["objectId"], "B73B646EAF134F989008DFBC7272EDB4")
        self.assertEqual(tokens[9]["attributeForm"], {"objectId": bm.ID_FORM})
        self.assertEqual(tokens[9]["target"], DATE)
        self.assertEqual(tokens[13]["target"], SALES)
        self.assertTrue(all("value" in t for t in tokens))

    def test_to_date_has_no_periods_and_text_follows(self):
        values = [t["value"] for t in bm.time_shift_tokens("MonthToDate", DATE, None, SALES, "Max")]
        self.assertEqual(values[:13], ["Max", "<", "UseLookupForAttributes", "=", "False", ">", "(", "MonthToDate",
                                       "(", "[Order Date]@ID", ",", "Sales", ")"])
        self.assertEqual(bm.time_shift_text("PreviousYear", DATE, 2, SALES, "Sum"),
                         "Sum(PreviousYear({Order Date} (ID),2,Sales))")
        self.assertEqual(bm.time_shift_text("YearToDate", DATE, None, SALES, "Sum"),
                         "Sum(YearToDate({Order Date} (ID),Sales))")

    def test_body_is_report_level(self):
        body = bm.time_shift_metric_body("X", "PreviousMonth", DATE, 1, SALES, fmt_values=FMT, description="d")
        self.assertEqual(body["information"], {"name": "X", "subType": "metric", "description": "d"})
        self.assertEqual(body["dimty"]["dimtyUnits"], [{"dimtyUnitType": "report_base_level",
                                                        "aggregation": "normal", "filtering": "apply",
                                                        "groupBy": True}])
        self.assertEqual(body["format"], {"header": [], "values": FMT})


class CommandTests(unittest.TestCase):
    def test_creates_in_one_changeset_and_reads_the_formula_back(self):
        fake = _FakeMSTR(_routes())
        result = _run(fake)
        self.assertEqual(_writes(fake), [("POST", "/api/model/changesets"), ("POST", f"{MP}/metrics"),
                                         ("POST", "/api/model/changesets/CS1/commit")])
        post = next(kw for method, path, kw, _ in fake.calls if (method, path) == ("POST", f"{MP}/metrics"))
        self.assertEqual(post["json"]["format"]["values"], FMT)   # the source fact metric's format
        self.assertEqual(result, {"ok": True, "metric_id": NEW_ID, "action": "created",
                                  "formula": "Sum(PreviousMonth({Order Date} (ID),1,Sales))",
                                  "expected": "Sum(PreviousMonth({Order Date} (ID),1,Sales))"})

    def test_a_different_read_back_fails_after_commit(self):
        fake = _FakeMSTR(_routes(read_back="Sum(Sales)"))
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            bm.cmd_create_time_shift_metric(fake, _args())
        self.assertIn("check it in Studio", err.getvalue())

    def test_failed_create_discards_the_changeset(self):
        fake = _FakeMSTR(_routes(write_status=400))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bm.cmd_create_time_shift_metric(fake, _args())
        self.assertIn(("DELETE", "/api/model/changesets/CS1"), _writes(fake))

    def test_missing_day_or_period_interval_stops_before_any_write(self):
        for intervals in (["month_interval", "quarter_interval", "year_interval"], ["day_interval"]):
            fake = _FakeMSTR(_routes(intervals=intervals))
            err = io.StringIO()
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
                bm.cmd_create_time_shift_metric(fake, _args())
            self.assertEqual(_writes(fake), [])
            self.assertIn("publish the model from Studio", err.getvalue())

    def test_existing_name_needs_replace_and_replace_keeps_id_description_format(self):
        existing = [{"objectId": EXISTING_ID, "name": "Sales (previous month)"}]
        for dry_run in (False, True):
            fake = _FakeMSTR(_routes(derived=existing))
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                bm.cmd_create_time_shift_metric(fake, _args(dry_run=dry_run))
            self.assertEqual(_writes(fake), [])
        fake = _FakeMSTR(_routes(derived=existing))
        result = _run(fake, replace=True)
        self.assertEqual(_writes(fake), [("POST", "/api/model/changesets"), ("PUT", f"{MP}/metrics/{EXISTING_ID}"),
                                         ("POST", "/api/model/changesets/CS1/commit")])
        put = next(kw for method, _, kw, _ in fake.calls if method == "PUT")["json"]
        self.assertEqual(put["information"]["description"], "Kept")
        self.assertEqual(put["format"]["values"], [{"type": "number_category", "value": "9"}])
        self.assertEqual(result["action"], "replaced")
        fake = _FakeMSTR(_routes())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bm.cmd_create_time_shift_metric(fake, _args(replace=True))

    def test_fact_metric_name_stops(self):
        fake = _FakeMSTR(_routes())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bm.cmd_create_time_shift_metric(fake, _args(name="Sales", replace=True))
        self.assertEqual(_writes(fake), [])

    def test_unverified_functions_and_bad_periods_stop_before_sign_in(self):
        for overrides in ({"function": "PreviousYear"}, {"function": "MonthToDate", "unverified_ok": True,
                                                          "periods": 1},
                          {"periods": 0}, {"aggregation": "Median"}):
            fake = _FakeMSTR(_routes())
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                bm.cmd_create_time_shift_metric(fake, _args(**overrides))
            self.assertEqual(fake.calls, [], overrides)
        result = _run(_FakeMSTR(_routes()), function="PreviousYear", unverified_ok=True, dry_run=True)
        self.assertEqual(result["formula"], "Sum(PreviousYear({Order Date} (ID),1,Sales))")

    def test_dry_run_writes_nothing_and_shows_the_body(self):
        fake = _FakeMSTR(_routes())
        result = _run(fake, dry_run=True, periods=2)
        self.assertEqual(_writes(fake), [])
        self.assertEqual(result["formula"], "Sum(PreviousMonth({Order Date} (ID),2,Sales))")
        self.assertEqual(result["plan"][0]["method"], "POST")
        self.assertIn("day_interval", result["time_attributes"])

    def test_cli(self):
        args = bm.build_parser().parse_args([
            "create-time-shift-metric", "--model-id", "M", "--name", "N", "--function", "PreviousMonth",
            "--time-attribute", "Order Date", "--source-metric", "Sales", "--periods", "1", "--replace"])
        self.assertEqual((args.function, args.periods, args.replace), ("PreviousMonth", 1, True))


if __name__ == "__main__":
    unittest.main()
