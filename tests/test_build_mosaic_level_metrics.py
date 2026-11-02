"""Tests for create-level-metric and create-weighted-average (memory/reference_mosaic_derived_metrics.md
§0d rules 1 and 2). Token streams are pinned to the shapes used live; a fake client stands in for MSTR.
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
AMOUNT = {"objectId": "A" * 32, "subType": "fact_metric", "name": "Original Amount (raw)"}
ACCOUNT = {"objectId": "B" * 32, "subType": "attribute", "name": "Account"}
SCORE = {"objectId": "C" * 32, "subType": "attribute", "name": "Credit Score"}
FMT = [{"type": "number_category", "value": "1"}]


def _values(tokens):
    return [t["value"] for t in tokens]


def _routes(created_ids=("1" * 32, "2" * 32, "3" * 32), fail_at=None, derived=None):
    ids = iter(created_ids)
    posts = {"n": 0}

    def post_metric():
        posts["n"] += 1
        if fail_at == posts["n"]:
            return _Resp(400, {"message": "bad"})
        return _Resp(201, {"information": {"objectId": next(ids)}})

    class Routes(dict):
        def get(self, key, default=None):
            if key == ("POST", f"{MP}/metrics"):
                return post_metric()
            return super().get(key, default)

    return Routes({
        ("GET", f"{MP}/factMetrics"): _Resp(200, {"factMetrics": [{"information": {
            "objectId": AMOUNT["objectId"], "name": AMOUNT["name"]}}]}),
        ("GET", f"{MP}/metrics"): _Resp(200, {"metrics": derived or []}),
        ("GET", f"{MP}/attributes"): _Resp(200, {"attributes": [
            {"information": {"objectId": ACCOUNT["objectId"], "name": "Account"}},
            {"information": {"objectId": SCORE["objectId"], "name": "Credit Score"}}]}),
        ("GET", f"{MP}/factMetrics/{AMOUNT['objectId']}"): _Resp(200, {"format": {"header": [], "values": FMT}}),
        ("POST", "/api/model/changesets"): _Resp(201, {"id": "CS1"}),
        ("POST", "/api/model/changesets/CS1/commit"): _Resp(204),
        ("DELETE", "/api/model/changesets/CS1"): _Resp(204),
    })


def _run(cmd, fake, args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        cmd(fake, args)
    return json.loads(out.getvalue())


def _level_args(**kw):
    base = dict(model_id="M1", name="Original Amount", source_metric="Original Amount (raw)",
                entity_attribute="Account", inner="Max", outer="Sum", format=None, description="", dry_run=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _wa_args(**kw):
    base = dict(model_id="M1", name="Weighted Avg Score", value_attribute="Credit Score", value_metric=None,
                weight_metric="Original Amount (raw)", entity_attribute="Account", numerator_name=None,
                denominator_name=None, format="fixed2", description="", dry_run=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


class TokenTests(unittest.TestCase):
    def test_level_tokens(self):
        tokens = bm.level_metric_tokens(AMOUNT, ACCOUNT)
        self.assertEqual(_values(tokens), ["Sum", "(", "Max", "(", "[Original Amount (raw)]", ")", "{", "~", "+", ",",
                                           "[Account]", "+", "}", ")", "{", "~", "+", "}", ""])
        self.assertEqual(tokens[4]["target"], AMOUNT)
        self.assertEqual(tokens[10]["target"], ACCOUNT)
        self.assertNotIn("target", tokens[0])                  # functions go by name
        self.assertEqual(_values(bm.level_metric_tokens(AMOUNT, ACCOUNT, "Min", "Avg"))[:3], ["Avg", "(", "Min"])

    def test_weighted_average_tokens(self):
        num, den = bm.weighted_average_tokens(SCORE, AMOUNT, ACCOUNT)
        per = lambda r: ["Max", "(", f"[{r['name']}]", ")", "{", "~", "+", ",", "[Account]", "+", "}"]  # noqa: E731
        self.assertEqual(_values(num), ["Sum", "("] + per(SCORE) + ["*"] + per(AMOUNT) + [")", "{", "~", "+", "}", ""])
        self.assertEqual(_values(den), ["Sum", "("] + per(AMOUNT) + ["*"] + per(SCORE) + ["/"] + per(SCORE)
                         + [")", "{", "~", "+", "}", ""])

    def test_body_is_report_level(self):
        body = bm.level_metric_body("x", [], description="d", fmt_values=FMT)
        self.assertEqual([u["dimtyUnitType"] for u in body["dimty"]["dimtyUnits"]], ["report_base_level"])
        self.assertEqual(body["format"]["values"], FMT)
        self.assertEqual(body["information"]["description"], "d")


class LevelMetricFlowTests(unittest.TestCase):
    def test_create_in_one_changeset_with_source_format(self):
        fake = _FakeMSTR(_routes())
        out = _run(bm.cmd_create_level_metric, fake, _level_args())
        self.assertEqual(out, {"ok": True, "metric_id": "1" * 32})
        writes = [(m, p) for m, p, _, _ in fake.calls if m != "GET"]
        self.assertEqual(writes, [("POST", "/api/model/changesets"), ("POST", f"{MP}/metrics"),
                                  ("POST", "/api/model/changesets/CS1/commit")])
        body = next(kw["json"] for m, p, kw, _ in fake.calls if (m, p) == ("POST", f"{MP}/metrics"))
        self.assertEqual(body["format"]["values"], FMT)
        self.assertEqual(body["expression"]["tokens"], bm.level_metric_tokens(AMOUNT, ACCOUNT))

    def test_dry_run_and_existing_name(self):
        fake = _FakeMSTR(_routes())
        out = _run(bm.cmd_create_level_metric, fake, _level_args(dry_run=True, format="currency"))
        self.assertEqual([(m, p) for m, p, _, _ in fake.calls if m != "GET"], [])
        self.assertEqual(out["plan"][0]["body"]["format"]["values"], bm._resolve_metric_format("currency"))
        fake = _FakeMSTR(_routes(derived=[{"information": {"objectId": "9" * 32, "name": "original amount"}}]))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bm.cmd_create_level_metric(fake, _level_args())


class WeightedAverageFlowTests(unittest.TestCase):
    def test_creates_helpers_then_outer_join_ratio(self):
        fake = _FakeMSTR(_routes())
        out = _run(bm.cmd_create_weighted_average, fake, _wa_args())
        self.assertEqual(out, {"ok": True, "metric_id": "3" * 32, "numerator_id": "1" * 32, "denominator_id": "2" * 32})
        bodies = [kw["json"] for m, p, kw, _ in fake.calls if (m, p) == ("POST", f"{MP}/metrics")]
        self.assertEqual([b["information"]["name"] for b in bodies],
                         ["Weighted Avg Score numerator", "Weighted Avg Score denominator", "Weighted Avg Score"])
        avg = bodies[2]
        self.assertIsNone(avg["dimty"])
        self.assertEqual(avg["formulaJoinType"], "outer")
        self.assertEqual(avg["expression"]["tree"]["function"], "divide")
        self.assertEqual([c["target"]["objectId"] for c in avg["expression"]["tree"]["children"]], ["1" * 32, "2" * 32])
        self.assertEqual(bodies[1]["format"]["values"], FMT)          # denominator takes the weight's format
        self.assertEqual(sum(1 for m, p, _, _ in fake.calls if p == "/api/model/changesets/CS1/commit"), 3)

    def test_failure_after_helpers_lists_them(self):
        fake = _FakeMSTR(_routes(fail_at=3))
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            bm.cmd_create_weighted_average(fake, _wa_args())
        self.assertIn("created before the failure", err.getvalue())
        self.assertIn("1" * 32, err.getvalue())

    def test_value_choice_names_and_dry_run(self):
        fake = _FakeMSTR(_routes())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bm.cmd_create_weighted_average(fake, _wa_args(value_metric="Original Amount (raw)"))
        self.assertEqual(fake.calls, [])
        fake = _FakeMSTR(_routes(derived=[{"information": {"objectId": "9" * 32, "name": "Weighted Avg Score numerator"}}]))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bm.cmd_create_weighted_average(fake, _wa_args())
        fake = _FakeMSTR(_routes())
        out = _run(bm.cmd_create_weighted_average, fake, _wa_args(dry_run=True, numerator_name="N", denominator_name="D"))
        self.assertEqual([s["body"]["information"]["name"] for s in out["plan"]], ["N", "D", "Weighted Avg Score"])
        self.assertEqual([(m, p) for m, p, _, _ in fake.calls if m != "GET"], [])


if __name__ == "__main__":
    unittest.main()
