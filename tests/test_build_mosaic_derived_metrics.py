"""Tests for the derived-metric commands beyond the single-condition path:

- create-conditional-metric: distinct count of an attribute, Count of a fact metric, a
  latest/first-period level rule, ANDed conditions, --format, --dry-run.
- create-compound-metric: arithmetic → expression tree with dimty null, formulaJoinType,
  --replace (full-body PUT), --dry-run.
- delete-derived-metric: derived metrics only, dependents check, --force, --dry-run.

Shapes follow memory/reference_mosaic_derived_metrics.md §0, §0c and §0d. A fake client
stands in for MSTR, so nothing touches the network.
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
CS_ROUTES = {
    ("POST", "/api/model/changesets"): _Resp(201, {"id": "CS1"}),
    ("POST", "/api/model/changesets/CS1/commit"): _Resp(204),
    ("DELETE", "/api/model/changesets/CS1"): _Resp(204),
}


def _oid(ch):
    return ch * 32


def _info(ch, name):
    return {"information": {"objectId": _oid(ch), "name": name}}


def _ref(ch, name, sub_type):
    return {"objectId": _oid(ch), "subType": sub_type, "name": name}


def _writes(fake):
    return [(method, path) for method, path, _, _ in fake.calls if method != "GET"]


def _run(cmd, fake, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        cmd(fake, args)
    return json.loads(out.getvalue())


def _fails(test, cmd, fake, args):
    with test.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), \
            contextlib.redirect_stdout(io.StringIO()):
        cmd(fake, args)


# ── create-conditional-metric ─────────────────────────────────────────────────────────────

SALES = _ref("A", "Net Sales", "fact_metric")
CUSTOMER = _ref("E", "Customer", "attribute")
STATUS = _ref("F", "Account Status", "attribute")
CHANNEL = _ref("1", "Channel", "attribute")
PERIOD = _ref("2", "Snapshot Month", "attribute")
METRIC_ID = _oid("D")
FILTER_ID = _oid("C")


def _cond_routes():
    routes = dict(CS_ROUTES)
    routes.update({
        ("GET", f"{MP}/factMetrics"): _Resp(200, {"factMetrics": [_info("A", "Net Sales")]}),
        ("GET", f"{MP}/attributes"): _Resp(200, {"attributes": [
            _info("E", "Customer"), _info("F", "Account Status"), _info("1", "Channel"),
            _info("2", "Snapshot Month")]}),
        ("GET", f"{MP}/factMetrics/{SALES['objectId']}"): _Resp(200, {"format": {"header": [], "values": [
            {"type": "number_category", "value": "1"}]}}),
        ("POST", f"{MP}/metrics"): _Resp(201, {"information": {"objectId": METRIC_ID}}),
        ("POST", f"{MP}/metrics/{METRIC_ID}/embeddedObjects"): _Resp(201, {"id": FILTER_ID}),
        ("PUT", f"{MP}/metrics/{METRIC_ID}"): _Resp(200, {}),
    })
    return routes


def _cond_args(**overrides):
    base = {"model_id": "M1", "name": "Active Customers", "source_metric": None,
            "count_attribute": "Customer", "attribute": "Account Status", "elements": ["Active"],
            "and_conditions": None, "function": None, "level_rule": None, "level_attribute": None,
            "format": None, "description": "", "dry_run": False}
    base.update(overrides)
    return types.SimpleNamespace(**base)


class ConditionalTokenTests(unittest.TestCase):
    def test_distinct_count_of_an_attribute(self):
        tokens = bm.conditional_metric_tokens("Count", CUSTOMER, distinct=True)

        self.assertEqual([t["value"] for t in tokens],
                         ["Count", "<", "Distinct", "=", "True", ",", "UseLookupForAttributes", "=", "False",
                          ">", "(", "[Customer]", ")", "{", "~", "+", "}", ""])
        # Count goes by name (the verified shape); Sum/Avg keep their platform objectIds.
        self.assertNotIn("target", tokens[0])
        self.assertEqual(tokens[11]["target"], CUSTOMER)

    def test_count_of_a_fact_metric_has_no_distinct(self):
        values = [t["value"] for t in bm.conditional_metric_tokens("Count", SALES)]

        self.assertEqual(values[:3], ["Count", "<", "UseLookupForAttributes"])
        self.assertNotIn("Distinct", values)

    def test_level_rule_adds_an_attribute_unit_after_report_level(self):
        body = bm.conditional_metric_body("Ending Sales", "Sum", SALES, level=(PERIOD, "last"))

        units = body["dimty"]["dimtyUnits"]
        self.assertEqual([u["dimtyUnitType"] for u in units], ["report_base_level", "attribute"])
        self.assertEqual(units[1]["target"], PERIOD)
        self.assertEqual(units[1]["aggregation"], "last_in_fact")
        self.assertEqual(bm.level_rule_unit(PERIOD, "first_lookup")["aggregation"], "first_in_relationship")

    def test_one_condition_is_a_bare_element_list(self):
        self.assertEqual(bm.embedded_filter_body([(STATUS, ["Active"])]),
                         bm.embedded_element_filter_body(STATUS, ["Active"]))
        self.assertEqual(bm.embedded_filter_body([(STATUS, ["Active"])])["qualification"]["tree"]["type"],
                         "predicate_element_list")

    def test_several_conditions_are_anded(self):
        for n in (2, 3):
            conds = [(STATUS, ["Active"]), (CHANNEL, ["Web", "Store"]), (PERIOD, ["202601"])][:n]
            tree = bm.embedded_filter_body(conds)["qualification"]["tree"]

            self.assertEqual((tree["type"], tree["function"]), ("operator", "and"))
            self.assertEqual([c["predicateTree"]["attribute"] for c in tree["children"]], [a for a, _ in conds])
        second = bm.embedded_filter_body(conds)["qualification"]["tree"]["children"][1]
        self.assertEqual([e["elementId"] for e in second["predicateTree"]["elements"]],
                         [f"hWeb;{CHANNEL['objectId']}", f"hStore;{CHANNEL['objectId']}"])


class ConditionalFlowTests(unittest.TestCase):
    def test_distinct_count_with_and_and_level_rule(self):
        fake = _FakeMSTR(_cond_routes())
        out = _run(bm.cmd_create_conditional_metric, fake, _cond_args(
            and_conditions=[["Channel", "Web", "Store"]], level_rule="last", level_attribute="Snapshot Month"))

        self.assertEqual(out["metric_id"], METRIC_ID)
        self.assertEqual(_writes(fake), [
            ("POST", "/api/model/changesets"),
            ("POST", f"{MP}/metrics"),
            ("POST", f"{MP}/metrics/{METRIC_ID}/embeddedObjects"),
            ("PUT", f"{MP}/metrics/{METRIC_ID}"),
            ("POST", "/api/model/changesets/CS1/commit"),
        ])
        # A count has no source fact metric to copy a format from.
        self.assertNotIn(("GET", f"{MP}/factMetrics/{SALES['objectId']}"),
                         [(method, path) for method, path, _, _ in fake.calls])
        post = next(kw["json"] for method, path, kw, _ in fake.calls if (method, path) == ("POST", f"{MP}/metrics"))
        self.assertEqual(post["format"]["values"], bm._resolve_metric_format("integer"))
        self.assertEqual(post["dimty"]["dimtyUnits"][1]["target"]["name"], "Snapshot Month")
        self.assertIn("Distinct", [t["value"] for t in post["expression"]["tokens"]])
        emb = next(kw["json"] for method, path, kw, _ in fake.calls if path.endswith("/embeddedObjects"))
        tree = emb["qualification"]["tree"]
        self.assertEqual(tree["function"], "and")
        self.assertEqual([c["predicateTree"]["attribute"]["name"] for c in tree["children"]],
                         ["Account Status", "Channel"])
        put = next(kw["json"] for method, _, kw, _ in fake.calls if method == "PUT")
        self.assertEqual(put["dimty"], post["dimty"])
        self.assertEqual(put["conditionality"]["removeElements"], False)

    def test_count_of_fact_rows_and_explicit_format(self):
        fake = _FakeMSTR(_cond_routes())
        _run(bm.cmd_create_conditional_metric, fake, _cond_args(
            count_attribute=None, source_metric="Net Sales", function="Count", format="fixed2"))

        post = next(kw["json"] for method, path, kw, _ in fake.calls if (method, path) == ("POST", f"{MP}/metrics"))
        self.assertEqual(post["expression"]["tokens"][0], {"type": "function", "value": "Count"})
        self.assertEqual(post["format"]["values"], bm._resolve_metric_format("fixed2"))

    def test_dry_run_reads_but_writes_nothing(self):
        fake = _FakeMSTR(_cond_routes())
        out = _run(bm.cmd_create_conditional_metric, fake, _cond_args(dry_run=True))

        self.assertEqual(_writes(fake), [])
        self.assertEqual([s["step"] for s in out["plan"]], ["create", "embed filter", "bind filter"])
        self.assertIn("conditionality", out["plan"][2]["body"])

    def test_bad_combinations_stop_before_any_call(self):
        bad = [
            {"source_metric": "Net Sales"},                       # both sources
            {"count_attribute": None},                            # neither
            {"function": "Sum"},                                  # a distinct count is always Count
            {"level_rule": "last"},                               # rule without its attribute
            {"level_attribute": "Snapshot Month"},                # attribute without a rule
            {"and_conditions": [["Channel"]]},                    # --and without values
            {"description": "x" * (bm.DESCRIPTION_MAX + 1)},
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides):
                fake = _FakeMSTR(_cond_routes())
                _fails(self, bm.cmd_create_conditional_metric, fake, _cond_args(**overrides))
                self.assertEqual(fake.calls, [])

    def test_unknown_and_attribute_stops_before_any_write(self):
        fake = _FakeMSTR(_cond_routes())
        _fails(self, bm.cmd_create_conditional_metric, fake, _cond_args(and_conditions=[["Region", "East"]]))

        self.assertEqual(_writes(fake), [])


# ── create-compound-metric ────────────────────────────────────────────────────────────────

RETURNS = _ref("B", "Returns", "fact_metric")
UNITS = _ref("3", "Units Sold", "fact_metric")
MARGIN = _ref("4", "Gross Margin", "metric")
NEW_ID = _oid("5")


def _metrics_routes(derived=None, metric_bodies=None):
    routes = dict(CS_ROUTES)
    routes.update({
        ("GET", f"{MP}/factMetrics"): _Resp(200, {"factMetrics": [
            _info("A", "Net Sales"), _info("B", "Returns"), _info("3", "Units Sold")]}),
        ("GET", f"{MP}/metrics"): _Resp(200, {"metrics": derived if derived is not None
                                              else [_info("4", "Gross Margin")]}),
        ("POST", f"{MP}/metrics"): _Resp(201, {"information": {"objectId": NEW_ID}}),
    })
    for oid, body in (metric_bodies or {}).items():
        routes[("GET", f"{MP}/metrics/{oid}")] = _Resp(200, body)
        routes[("PUT", f"{MP}/metrics/{oid}")] = _Resp(200, {})
        routes[("DELETE", f"{MP}/metrics/{oid}")] = _Resp(204)
    return routes


def _compound_args(**overrides):
    base = {"model_id": "M1", "name": "Sales After Returns", "formula": "[Net Sales] - [Returns]",
            "join": None, "format": None, "description": "", "replace": False, "dry_run": False,
            "unverified_ok": False}
    base.update(overrides)
    return types.SimpleNamespace(**base)


class CompoundTreeTests(unittest.TestCase):
    index = bm._metric_operand_index(
        [_info("A", "Net Sales"), _info("B", "Returns"), _info("3", "Units Sold")],
        [_info("4", "Gross Margin")])

    def tree(self, formula):
        return bm.compound_metric_tree(formula, lambda raw: bm._resolve_metric_operand(self.index, raw))

    @staticmethod
    def ref(r):
        return {"type": "object_reference", "target": r}

    def test_precedence_and_parentheses(self):
        tree = self.tree("([Net Sales] + [Returns]) / [Units Sold] * 100")

        self.assertEqual(tree, {"type": "operator", "function": "times", "children": [
            {"type": "operator", "function": "divide", "children": [
                {"type": "operator", "function": "plus", "children": [self.ref(SALES), self.ref(RETURNS)]},
                self.ref(UNITS)]},
            {"type": "constant", "variant": {"type": "int64", "value": "100"}}]})

    def test_unary_minus_derived_operand_and_case_insensitive_names(self):
        tree = self.tree("-[gross margin] + [NET SALES]")

        self.assertEqual(tree["function"], "plus")
        self.assertEqual(tree["children"][0],
                         {"type": "operator", "function": "unary_minus", "children": [self.ref(MARGIN)]})

    def test_old_space_separated_object_id_syntax_still_works(self):
        tree = self.tree(f"{SALES['objectId']} - {RETURNS['objectId'].lower()}")

        self.assertEqual(tree, {"type": "operator", "function": "minus",
                                "children": [self.ref(SALES), self.ref(RETURNS)]})

    def test_prefix_picks_between_a_fact_and_a_derived_metric_with_one_name(self):
        index = bm._metric_operand_index([_info("A", "Net Sales")], [_info("6", "Net Sales")])

        def tree(formula):
            return bm.compound_metric_tree(formula, lambda raw: bm._resolve_metric_operand(index, raw))

        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            tree("[Net Sales] * 2")
        self.assertEqual(tree("[metric:Net Sales] * 2")["children"][0]["target"]["objectId"], _oid("6"))
        self.assertEqual(tree("[fact_metric:Net Sales] * 2")["children"][0]["target"]["subType"], "fact_metric")

    def test_unreadable_formulas_stop(self):
        for formula in ("[Net Sales] * 0.5", "[Unknown] - [Returns]", "([Net Sales] - [Returns]",
                        "[Net Sales] -", "100 * 2", "[Net Sales] % 2"):
            with self.subTest(formula=formula), self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.tree(formula)

    def test_body_has_null_dimty_and_optional_join_and_format(self):
        tree = self.tree("[Net Sales] - [Returns]")
        plain = bm.compound_metric_body("Sales After Returns", tree)
        outer = bm.compound_metric_body("Sales After Returns", tree, join="outer",
                                        fmt_values=bm._resolve_metric_format("currency"))

        self.assertIsNone(plain["dimty"])
        self.assertNotIn("formulaJoinType", plain)
        self.assertNotIn("format", plain)
        self.assertEqual(outer["formulaJoinType"], "outer")
        self.assertEqual(outer["format"]["values"], bm._resolve_metric_format("currency"))


class CompoundFlowTests(unittest.TestCase):
    def test_create_posts_the_tree_in_one_changeset(self):
        fake = _FakeMSTR(_metrics_routes())
        out = _run(bm.cmd_create_compound_metric, fake, _compound_args(join="outer"))

        self.assertEqual(out, {"ok": True, "metric_id": NEW_ID, "action": "created"})
        self.assertEqual(_writes(fake), [("POST", "/api/model/changesets"), ("POST", f"{MP}/metrics"),
                                         ("POST", "/api/model/changesets/CS1/commit")])
        method, path, kw, changeset = next(c for c in fake.calls if c[:2] == ("POST", f"{MP}/metrics"))
        self.assertEqual(changeset, "CS1")
        self.assertEqual(kw["params"], {"showAdvancedProperties": "true"})
        self.assertEqual(kw["json"]["expression"]["tree"]["function"], "minus")
        self.assertIsNone(kw["json"]["dimty"])
        self.assertEqual(kw["json"]["formulaJoinType"], "outer")

    def test_existing_name_needs_replace(self):
        fake = _FakeMSTR(_metrics_routes())
        _fails(self, bm.cmd_create_compound_metric, fake, _compound_args(name="gross margin"))

        self.assertEqual(_writes(fake), [])

    def test_replace_puts_full_body_and_keeps_description_and_format(self):
        fmt = bm._resolve_metric_format("percent")
        fake = _FakeMSTR(_metrics_routes(metric_bodies={MARGIN["objectId"]: {
            "information": {"name": "Gross Margin", "description": "Sales less returns, as a share of sales"},
            "format": {"header": [], "values": fmt}}}))
        out = _run(bm.cmd_create_compound_metric, fake, _compound_args(
            name="Gross Margin", formula="([Net Sales] - [Returns]) / [Net Sales]", replace=True))

        self.assertEqual(out["action"], "replaced")
        self.assertEqual(_writes(fake), [("POST", "/api/model/changesets"),
                                         ("PUT", f"{MP}/metrics/{MARGIN['objectId']}"),
                                         ("POST", "/api/model/changesets/CS1/commit")])
        put = next(kw["json"] for method, _, kw, _ in fake.calls if method == "PUT")
        self.assertEqual(put["information"]["description"], "Sales less returns, as a share of sales")
        self.assertEqual(put["format"]["values"], fmt)
        self.assertIsNone(put["dimty"])

    def test_replace_of_a_missing_metric_stops(self):
        fake = _FakeMSTR(_metrics_routes())
        _fails(self, bm.cmd_create_compound_metric, fake, _compound_args(replace=True))

        self.assertEqual(_writes(fake), [])

    def test_dry_run_writes_nothing(self):
        fake = _FakeMSTR(_metrics_routes())
        out = _run(bm.cmd_create_compound_metric, fake, _compound_args(dry_run=True))

        self.assertEqual(_writes(fake), [])
        self.assertEqual(out["plan"][0]["method"], "POST")


# ── delete-derived-metric ─────────────────────────────────────────────────────────────────

AOV = _ref("7", "Average Order Value", "metric")
ORDERS = _ref("8", "Order Count", "metric")


def _delete_routes(dependent_mentions_aov=True):
    uses = ({"expression": {"tokens": [{"type": "object_reference", "target": AOV}]}}
            if dependent_mentions_aov else {"expression": {"text": "{Net Sales} / {Order Count}"}})
    return _metrics_routes(
        derived=[_info("7", "Average Order Value"), _info("8", "Order Count"), _info("9", "AOV Index")],
        metric_bodies={AOV["objectId"]: {"expression": {"text": "{Net Sales} / {Order Count}"}},
                       ORDERS["objectId"]: {"expression": {"text": "Count({Orders})"}},
                       _oid("9"): uses})


def _delete_args(**overrides):
    base = {"model_id": "M1", "name": "Average Order Value", "object_id": None,
            "yes": True, "force": False, "dry_run": False}
    base.update(overrides)
    return types.SimpleNamespace(**base)


class DeleteDerivedMetricTests(unittest.TestCase):
    def test_needs_yes(self):
        fake = _FakeMSTR(_delete_routes())
        _fails(self, bm.cmd_delete_derived_metric, fake, _delete_args(yes=False))

        self.assertEqual(fake.calls, [])

    def test_refuses_a_fact_metric(self):
        fake = _FakeMSTR(_delete_routes())
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            bm.cmd_delete_derived_metric(fake, _delete_args(name="Net Sales"))

        self.assertIn("fact metric", err.getvalue())
        self.assertEqual(_writes(fake), [])

    def test_dependents_block_the_delete_without_force(self):
        fake = _FakeMSTR(_delete_routes())
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            bm.cmd_delete_derived_metric(fake, _delete_args())

        self.assertIn("AOV Index", err.getvalue())
        self.assertEqual(_writes(fake), [])

    def test_force_deletes_in_a_changeset_and_reports_dependents(self):
        fake = _FakeMSTR(_delete_routes())
        out = _run(bm.cmd_delete_derived_metric, fake, _delete_args(force=True))

        self.assertEqual(_writes(fake), [("POST", "/api/model/changesets"),
                                         ("DELETE", f"{MP}/metrics/{AOV['objectId']}"),
                                         ("POST", "/api/model/changesets/CS1/commit")])
        self.assertEqual([d["name"] for d in out["dependents"]], ["AOV Index"])

    def test_name_in_expression_text_counts_as_a_dependent(self):
        fake = _FakeMSTR(_delete_routes(dependent_mentions_aov=False))
        out = _run(bm.cmd_delete_derived_metric, fake, _delete_args(name="Order Count", dry_run=True, yes=False))

        self.assertEqual(sorted(d["name"] for d in out["dependents"]), ["AOV Index", "Average Order Value"])
        self.assertEqual(_writes(fake), [])

    def test_no_dependents_deletes_by_object_id(self):
        fake = _FakeMSTR(_delete_routes(dependent_mentions_aov=False))
        out = _run(bm.cmd_delete_derived_metric, fake,
                   _delete_args(name=None, object_id=_oid("9").lower()))

        self.assertEqual(out["deleted"]["name"], "AOV Index")
        self.assertIn(("DELETE", f"{MP}/metrics/{_oid('9')}"), _writes(fake))


if __name__ == "__main__":
    unittest.main()
