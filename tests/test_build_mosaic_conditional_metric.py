"""Tests for create-conditional-metric (the verified 2026-09-29 write path).

Pins the token/body builders and the one-changeset call sequence documented in
memory/reference_mosaic_derived_metrics.md §0c: POST /metrics → POST
/metrics/{id}/embeddedObjects → PUT /metrics/{id} → commit, with the changeset
in the X-MSTR-MS-Changeset header. A fake client stands in for MSTR, so nothing
touches the network.
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


SOURCE = {"objectId": "A" * 32, "subType": "fact_metric", "name": "Revenue"}
ATTR = {"objectId": "B" * 32, "subType": "attribute", "name": "Region"}
FILTER_ID = "C" * 32
METRIC_ID = "D" * 32
MP = "/api/model/dataModels/M1"
FMT_VALUES = [{"type": "number_category", "value": "1"},
              {"type": "number_format", "value": '"$"#,##0.00;"$"-#,##0.00'}]


class TokenBuilderTests(unittest.TestCase):
    def test_unfiltered_tokens_spell_sum_lookup_false_report_level(self):
        tokens = bm.conditional_metric_tokens("Sum", SOURCE)

        self.assertEqual(
            [t["value"] for t in tokens],
            ["Sum", "<", "UseLookupForAttributes", "=", "False", ">",
             "(", "[Revenue]", ")", "{", "~", "+", "}", ""],
        )
        # Sum's objectId is a platform constant, identical on every tenant.
        self.assertEqual(tokens[0]["target"]["objectId"], "8107C31BDD9911D3B98100C04F2233EA")
        self.assertEqual(tokens[7]["target"], SOURCE)
        self.assertEqual(tokens[-1]["type"], "end_of_text")

    def test_every_token_carries_a_value(self):
        for filter_id in ("", FILTER_ID):
            for token in bm.conditional_metric_tokens("Sum", SOURCE, filter_id):
                self.assertIn("value", token)

    def test_filter_binding_sits_just_before_end_of_text(self):
        tokens = bm.conditional_metric_tokens("Sum", SOURCE, FILTER_ID)

        self.assertEqual([t["value"] for t in tokens[-5:]], ["}", "<", "", ">", ""])
        self.assertEqual(tokens[-3]["type"], "object_reference")
        self.assertEqual(tokens[-3]["target"],
                         {"objectId": FILTER_ID, "subType": "filter", "isEmbedded": True})


class BodyBuilderTests(unittest.TestCase):
    def test_create_body_is_report_level_without_conditionality(self):
        body = bm.conditional_metric_body("Revenue East", "Sum", SOURCE, FMT_VALUES)

        self.assertEqual(body["information"], {"name": "Revenue East", "subType": "metric"})
        self.assertEqual([u["dimtyUnitType"] for u in body["dimty"]["dimtyUnits"]],
                         ["report_base_level"])
        self.assertEqual(body["format"], {"header": [], "values": FMT_VALUES})
        self.assertNotIn("conditionality", body)

    def test_bind_body_adds_conditionality(self):
        body = bm.conditional_metric_body("Revenue East", "Sum", SOURCE, filter_id=FILTER_ID)

        self.assertEqual(body["conditionality"], {
            "filter": {"objectId": FILTER_ID, "subType": "filter", "isEmbedded": True},
            "embedMethod": "report_into_metric_filter",
            "removeElements": False,
        })
        self.assertEqual(body["expression"]["tokens"],
                         bm.conditional_metric_tokens("Sum", SOURCE, FILTER_ID))

    def test_embedded_filter_is_element_list_with_attribute_suffixed_ids(self):
        body = bm.embedded_element_filter_body(ATTR, ["East", "West"])

        self.assertEqual(body["subType"], "filter")
        tree = body["qualification"]["tree"]
        self.assertEqual(tree["type"], "predicate_element_list")
        self.assertEqual(tree["predicateTree"]["attribute"], ATTR)
        self.assertEqual(tree["predicateTree"]["function"], "in")
        self.assertEqual(tree["predicateTree"]["elements"], [
            {"elementId": f"hEast;{ATTR['objectId']}", "display": "East"},
            {"elementId": f"hWest;{ATTR['objectId']}", "display": "West"},
        ])


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.text = "" if body is None else json.dumps(body)

    def json(self):
        return self._body


class _FakeMSTR:
    """Routes (method, path) to canned responses; records each call together
    with the changeset header in force when it was made."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.s = types.SimpleNamespace(headers={})
        self.verbose = False
        self.identity = None

    def login(self, *, identity=False):
        self.identity = identity

    def _call(self, method, path, **kw):
        self.calls.append((method, path, kw, self.s.headers.get("X-MSTR-MS-Changeset")))
        return self.routes.get((method, path), _Resp(404, {"message": "unrouted"}))

    def get(self, path, **kw):
        return self._call("GET", path, **kw)

    def post(self, path, **kw):
        return self._call("POST", path, **kw)

    def put(self, path, **kw):
        return self._call("PUT", path, **kw)

    def delete(self, path, **kw):
        return self._call("DELETE", path, **kw)


EXISTING_ID = "E" * 32
OLD_FILTER_ID = "F" * 32


def _routes(bind_status=200, derived=()):
    return {
        ("GET", f"{MP}/factMetrics"): _Resp(200, {"factMetrics": [
            {"information": {"objectId": SOURCE["objectId"], "name": "Revenue"}}]}),
        ("GET", f"{MP}/metrics"): _Resp(200, {"metrics": [{"information": dict(d)} for d in derived]}),
        ("GET", f"{MP}/metrics/{EXISTING_ID}"): _Resp(200, {
            "information": {"objectId": EXISTING_ID, "name": "Revenue East", "description": "Kept description"},
            "format": {"header": [], "values": [{"type": "number_category", "value": "0"}]},
            "conditionality": {"filter": {"objectId": OLD_FILTER_ID, "subType": "filter", "isEmbedded": True}}}),
        ("POST", f"{MP}/metrics/{EXISTING_ID}/embeddedObjects"): _Resp(201, {"id": FILTER_ID}),
        ("PUT", f"{MP}/metrics/{EXISTING_ID}"): _Resp(bind_status, {}),
        ("GET", f"{MP}/attributes"): _Resp(200, {"attributes": [
            {"information": {"objectId": ATTR["objectId"], "name": "Region"}}]}),
        ("GET", f"{MP}/factMetrics/{SOURCE['objectId']}"): _Resp(200, {
            "format": {"header": [], "values": FMT_VALUES}}),
        ("POST", "/api/model/changesets"): _Resp(201, {"id": "CS1"}),
        ("POST", f"{MP}/metrics"): _Resp(201, {"information": {"objectId": METRIC_ID}}),
        ("POST", f"{MP}/metrics/{METRIC_ID}/embeddedObjects"): _Resp(201, {"id": FILTER_ID}),
        ("PUT", f"{MP}/metrics/{METRIC_ID}"): _Resp(bind_status, {}),
        ("POST", "/api/model/changesets/CS1/commit"): _Resp(204),
        ("DELETE", "/api/model/changesets/CS1"): _Resp(204),
    }


def _args(**overrides):
    base = {"model_id": "M1", "name": "Revenue East", "source_metric": "revenue",
            "attribute": "Region", "elements": ["East"], "function": "Sum", "description": ""}
    base.update(overrides)
    return types.SimpleNamespace(**base)


class CreateConditionalMetricFlowTests(unittest.TestCase):
    def test_create_embed_bind_commit_in_one_changeset(self):
        fake = _FakeMSTR(_routes())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bm.cmd_create_conditional_metric(fake, _args())

        self.assertTrue(fake.identity)
        writes = [(method, path) for method, path, _, _ in fake.calls if method != "GET"]
        self.assertEqual(writes, [
            ("POST", "/api/model/changesets"),
            ("POST", f"{MP}/metrics"),
            ("POST", f"{MP}/metrics/{METRIC_ID}/embeddedObjects"),
            ("PUT", f"{MP}/metrics/{METRIC_ID}"),
            ("POST", "/api/model/changesets/CS1/commit"),
        ])
        for method, path, kw, changeset in fake.calls:
            self.assertNotIn("changesetId", path)
            if method != "GET" and path.startswith(MP):
                self.assertEqual(changeset, "CS1")
        self.assertNotIn("X-MSTR-MS-Changeset", fake.s.headers)

        post_kw = next(kw for method, path, kw, _ in fake.calls
                       if method == "POST" and path == f"{MP}/metrics")
        self.assertEqual(post_kw["params"], {"showAdvancedProperties": "true"})
        self.assertNotIn("conditionality", post_kw["json"])
        self.assertEqual(post_kw["json"]["format"]["values"], FMT_VALUES)
        put_kw = next(kw for method, _, kw, _ in fake.calls if method == "PUT")
        self.assertEqual(put_kw["params"], {"showAdvancedProperties": "true"})
        self.assertEqual(put_kw["json"]["conditionality"]["filter"]["objectId"], FILTER_ID)
        self.assertEqual(json.loads(out.getvalue()),
                         {"ok": True, "metric_id": METRIC_ID, "embedded_filter_id": FILTER_ID})

    def test_failed_bind_discards_the_changeset(self):
        fake = _FakeMSTR(_routes(bind_status=400))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bm.cmd_create_conditional_metric(fake, _args())

        writes = [(method, path) for method, path, _, _ in fake.calls if method != "GET"]
        self.assertIn(("DELETE", "/api/model/changesets/CS1"), writes)
        self.assertNotIn(("POST", "/api/model/changesets/CS1/commit"), writes)
        self.assertNotIn("X-MSTR-MS-Changeset", fake.s.headers)

    def test_unknown_source_metric_stops_before_any_write(self):
        fake = _FakeMSTR(_routes())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bm.cmd_create_conditional_metric(fake, _args(source_metric="Margin"))

        self.assertEqual([method for method, _, _, _ in fake.calls], ["GET"])


EXISTING = {"objectId": EXISTING_ID, "name": "Revenue East"}


def _writes(fake):
    return [(method, path) for method, path, _, _ in fake.calls if method != "GET"]


class ExistingNameTests(unittest.TestCase):
    def test_existing_name_stops_before_any_write_without_replace(self):
        for dry_run in (False, True):
            fake = _FakeMSTR(_routes(derived=[EXISTING]))
            err = io.StringIO()
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(io.StringIO()):
                bm.cmd_create_conditional_metric(fake, _args(dry_run=dry_run))
            self.assertEqual(_writes(fake), [])
            self.assertIn("--replace", err.getvalue())

    def test_name_match_is_case_insensitive(self):
        fake = _FakeMSTR(_routes(derived=[{"objectId": EXISTING_ID, "name": "REVENUE EAST"}]))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bm.cmd_create_conditional_metric(fake, _args())
        self.assertEqual(_writes(fake), [])

    def test_fact_metric_name_stops_even_with_replace(self):
        fake = _FakeMSTR(_routes())
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            bm.cmd_create_conditional_metric(fake, _args(name="Revenue", replace=True))
        self.assertEqual(_writes(fake), [])
        self.assertIn("fact metric", err.getvalue())

    def test_replace_without_existing_metric_stops(self):
        fake = _FakeMSTR(_routes())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bm.cmd_create_conditional_metric(fake, _args(replace=True))
        self.assertEqual(_writes(fake), [])

    def test_replace_embeds_and_puts_on_the_existing_id_in_one_changeset(self):
        fake = _FakeMSTR(_routes(derived=[EXISTING]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bm.cmd_create_conditional_metric(fake, _args(replace=True))

        self.assertEqual(_writes(fake), [
            ("POST", "/api/model/changesets"),
            ("POST", f"{MP}/metrics/{EXISTING_ID}/embeddedObjects"),
            ("PUT", f"{MP}/metrics/{EXISTING_ID}"),
            ("POST", "/api/model/changesets/CS1/commit"),
        ])
        for method, path, _, changeset in fake.calls:
            if method != "GET" and path.startswith(MP):
                self.assertEqual(changeset, "CS1")
        put = next(kw for method, _, kw, _ in fake.calls if method == "PUT")["json"]
        self.assertEqual(put["conditionality"]["filter"]["objectId"], FILTER_ID)
        self.assertEqual(put["information"]["description"], "Kept description")
        self.assertEqual(put["format"]["values"], [{"type": "number_category", "value": "0"}])
        self.assertEqual(json.loads(out.getvalue()), {
            "ok": True, "metric_id": EXISTING_ID, "embedded_filter_id": FILTER_ID, "action": "replaced",
            "previous_embedded_filter_id": OLD_FILTER_ID})

    def test_replace_takes_a_new_description_and_format_when_given(self):
        fake = _FakeMSTR(_routes(derived=[EXISTING]))
        with contextlib.redirect_stdout(io.StringIO()):
            bm.cmd_create_conditional_metric(fake, _args(replace=True, description="New text", format="integer"))
        put = next(kw for method, _, kw, _ in fake.calls if method == "PUT")["json"]
        self.assertEqual(put["information"]["description"], "New text")
        self.assertEqual(put["format"]["values"], bm._resolve_metric_format("integer"))

    def test_failed_replace_discards_the_changeset(self):
        fake = _FakeMSTR(_routes(bind_status=400, derived=[EXISTING]))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bm.cmd_create_conditional_metric(fake, _args(replace=True))
        self.assertIn(("DELETE", "/api/model/changesets/CS1"), _writes(fake))
        self.assertNotIn(("POST", "/api/model/changesets/CS1/commit"), _writes(fake))

    def test_replace_dry_run_names_the_existing_id_and_writes_nothing(self):
        fake = _FakeMSTR(_routes(derived=[EXISTING]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bm.cmd_create_conditional_metric(fake, _args(replace=True, dry_run=True))
        self.assertEqual(_writes(fake), [])
        plan = json.loads(out.getvalue())
        self.assertEqual(plan["action"], "replace")
        self.assertEqual(plan["previous_embedded_filter_id"], OLD_FILTER_ID)
        self.assertEqual([(s["method"], s["path"]) for s in plan["plan"]], [
            ("POST", f"{MP}/metrics/{EXISTING_ID}/embeddedObjects"), ("PUT", f"{MP}/metrics/{EXISTING_ID}")])

    def test_cli_accepts_replace(self):
        args = bm.build_parser().parse_args(["create-conditional-metric", "--model-id", "M1", "--name", "X",
                                             "--source-metric", "Revenue", "--attribute", "Region",
                                             "--elements", "East", "--replace"])
        self.assertTrue(args.replace)


if __name__ == "__main__":
    unittest.main()
