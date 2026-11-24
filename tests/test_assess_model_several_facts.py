"""Models with several fact tables (items 70, 71, 72, 74, 75, 78): which tables are facts and which describe an
entity, which attributes reach each fact (from the export, else from one-row queries), the per-fact scope, and
probe, join-paths and relationships using them. A generic retail model: an orders fact, a returns fact, a store
table that carries a metric, a calendar and a reason table. Nothing touches the network.
"""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _hermetic  # noqa: E402,F401  (scrub MSTR_*/proxy env, private secret store)

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skills", "strategy-model-assessment", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills", "build-mosaic-model", "scripts"))

import assess_model as am  # noqa: E402


def _table(rows):
    header, body = rows[0], rows[1:]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def _error(text):
    return {"isError": True, "content": [{"type": "text", "text": text}]}


ROWS = {"order lines": 1000.0, "quantity": 1000.0, "returned units": 100.0, "floor area": 20.0}
TABLE = {"order lines": "orders", "quantity": "orders", "returned units": "returns", "floor area": "stores"}
DISTINCT = {"store id": 20, "region": 4, "order date": 365, "year": 2, "month": 12, "order line": 1000,
            "return": 100, "return date": 300, "return reason": 5, "channel": 3}
# which attribute reaches which table's rows (the truth the fake connector answers from)
REACH = {"orders": {"store id", "region", "order date", "year", "month", "order line", "channel"},
         "returns": {"store id", "region", "return", "return date", "return reason"},
         "stores": {"store id", "region"}}


def _attr(name, typ="varchar"):
    return {"name": name, "type": typ, "description": "", "profile": {"distinct_values": float(DISTINCT[name])}}


SCOPE = {
    "model": "Retail Model", "generated": "2026-01-15",
    "attributes": [_attr("store id", "bigint"), _attr("region"), _attr("order date", "date"),
                   _attr("year", "bigint"), _attr("month", "bigint"), _attr("order line", "bigint"),
                   _attr("return", "bigint"), _attr("return date", "date"), _attr("return reason"),
                   _attr("channel")],
    "metrics": [{"name": "order lines", "type": "bigint", "formula": "sum(orders.{ORDER_LINES})"},
                {"name": "quantity", "type": "double", "formula": "sum(orders.{QUANTITY})"},
                {"name": "returned units", "type": "double", "formula": "sum(returns.{RETURNED_UNITS})"},
                {"name": "floor area", "type": "double", "formula": "sum(stores.{FLOOR_AREA})"},
                {"name": "returns counted", "type": "bigint", "formula": "Count(Return (ID))"},
                {"name": "return rate", "type": "double", "formula": "Sum({Returned Units}) / Sum({Quantity})"}],
    "anchor": {"metric": "order lines", "counts": dict(ROWS)},
}


def _export():
    def att(name, tables, lookup):
        return {"information": {"name": name}, "attributeLookupTable": {"name": lookup},
                "keyForm": {"id": "k"}, "forms": [{"id": "k", "name": "ID", "expressions": [
                    {"expression": {"text": f"[{name}]"}, "tables": [{"name": t} for t in tables]}]}]}

    def fm(name, table):
        return {"information": {"name": name},
                "fact": {"expressions": [{"expression": {"text": "[x]"}, "tables": [{"name": table}]}]}}
    return {"model": {
        "tables": [{"information": {"name": t}} for t in ("ORDERS", "RETURNS", "STORES", "CALENDAR", "REGIONS",
                                                         "REASONS")],
        "attributes": [att("Store ID", ["ORDERS", "RETURNS", "STORES"], "STORES"),
                       att("Region", ["STORES", "REGIONS"], "REGIONS"),
                       att("Order Date", ["ORDERS", "CALENDAR"], "CALENDAR"),
                       att("Year", ["CALENDAR"], "CALENDAR"), att("Month", ["CALENDAR"], "CALENDAR"),
                       att("Order Line", ["ORDERS"], "ORDERS"),
                       att("Channel", ["ORDERS", "CHANNEL_MAP"], "ORDERS"),   # its lookup is the fact itself
                       att("Channel Group", ["CHANNEL_MAP"], "CHANNEL_MAP"),
                       att("Return", ["RETURNS"], "RETURNS"), att("Return Date", ["RETURNS"], "RETURNS"),
                       att("Return Reason", ["RETURNS", "REASONS"], "REASONS")],
        "factMetrics": [fm("Order Lines", "ORDERS"), fm("Quantity", "ORDERS"), fm("Returned Units", "RETURNS"),
                        fm("Floor Area", "STORES")]}}


class _Connector:
    """Answers the query shapes scope, probe and join-paths ask, from ROWS / DISTINCT / REACH."""

    def __init__(self, ignored_filters=()):
        self.calls, self.ignored = [], set(ignored_filters)

    def call(self, tool, args):
        sql = args["query"]
        self.calls.append(sql)
        return self.answer(sql)

    @staticmethod
    def _anchor(sql):
        m = re.search(r'COUNT\("([^"]+)"\)', sql)
        return m.group(1) if m else ""

    def answer(self, sql):
        anchor = self._anchor(sql)
        table = TABLE.get(anchor, "")
        if "AS groups" in sql:      # probe's one-row shapes
            attrs = re.findall(r'CAST\("([^"]+)" AS VARCHAR\) AS [vp]', sql)
            if any(a not in REACH.get(table, ()) for a in attrs):
                if table == "returns":
                    return _error("Query failed: too many intermediate rows (over 50000000)")
                d = DISTINCT[attrs[0]]
                return _table([["groups", "with_rows", "kept"], [str(d), str(d), str(ROWS[anchor] * d)]])
            d = DISTINCT[attrs[-1]]
            return _table([["groups", "with_rows", "kept"], [str(d), str(d), str(ROWS[anchor])]])
        if "AS listed" in sql:
            a = re.search(r'CAST\("([^"]+)" AS VARCHAR\)', sql).group(1)
            return _table([["listed"], [str(DISTINCT[a])]])
        m = re.match(r'SELECT "([^"]+)" AS v, COUNT\("([^"]+)"\) AS n', sql)
        if m:                         # join-paths' smallest two groups
            a, n = m.group(1), ROWS[m.group(2)]
            if a not in REACH[TABLE[m.group(2)]]:
                return _table([["v", "n"], ["1", str(n)], ["2", str(n)]])
            return _table([["v", "n"], ["1", str(n / 10)], ["2", str(n / 5)]])
        m = re.match(r'SELECT COUNT\("([^"]+)"\) AS n FROM "[^"]+"(?: WHERE "([^"]+)" = )?', sql)
        if m:                         # a total, or the filter test
            n = ROWS[m.group(1)]
            if m.group(2) and m.group(2) not in self.ignored:
                n = n / 10
            return _table([["n"], [str(n)]])
        if sql.startswith("SELECT CAST(COUNT(") and " AS c0" in sql:   # anchor counts
            names = re.findall(r'COUNT\("([^"]+)"\)', sql)
            return _table([[f"c{i}" for i in range(len(names))], [str(ROWS[x]) for x in names]])
        if re.match(r'SELECT CAST\(COUNT\("[^"]+"\) AS VARCHAR\) AS n FROM', sql):
            a = self._anchor(sql)
            return _table([["n"], [str(ROWS.get(a, DISTINCT.get(a, 0)))]])
        if "AS n FROM (SELECT" in sql:   # entities
            return _table([["n"], ["20"]])
        cols = re.findall(r" AS (m\d+)", sql)
        if " AS g," in sql:
            return _table([["g"] + cols, ["a"] + ["1"] * len(cols), ["b"] + ["2"] * len(cols),
                           ["c"] + ["3"] * len(cols)])
        return _table([cols or ["x"], ["6"] * max(len(cols), 1)])


def _structured(source="export"):
    scope = dict(SCOPE, attributes=[dict(a) for a in SCOPE["attributes"]])
    runner = _Connector()
    scope.update(am.fact_structure(runner, "retail", scope, export=_export() if source == "export" else None))
    return scope, runner


class FactTableTests(unittest.TestCase):
    def test_export_tells_a_table_that_describes_an_entity_from_a_fact(self):
        scope, runner = _structured("export")
        self.assertEqual(runner.calls, [])                       # the export answers everything
        self.assertEqual([f["table"] for f in scope["facts"]], ["orders", "returns"])
        self.assertEqual([(d["table"], d["key"], d["reached_by"]) for d in scope["dimension_tables"]],
                         [("stores", "store id", "orders")])
        self.assertEqual(am.reach_of(scope, "return reason", "orders"), "not_reached")
        self.assertEqual(am.reach_of(scope, "return reason", "returns"), "reaches")
        self.assertEqual(am.reach_of(scope, "region", "returns"), "reaches")      # through the store's lookup
        self.assertEqual(am.reach_of(scope, "month", "returns"), "not_reached")   # the calendar joins orders only
        # a mapping table keyed on the fact's own attribute (a period map keyed on the fact's period)
        self.assertEqual(am.reach_of(scope, "channel group", "orders"), "reaches")
        self.assertEqual(am.reach_of(scope, "channel group", "returns"), "not_reached")

    def test_queries_tell_them_apart_without_the_export(self):
        scope, runner = _structured("queries")
        self.assertEqual([f["table"] for f in scope["facts"]], ["orders", "returns"])
        self.assertEqual(scope["dimension_tables"][0]["table"], "stores")
        self.assertEqual(scope["dimension_tables"][0]["key"], "store id")
        self.assertEqual(am.reach_of(scope, "return reason", "orders"), "cross_join")
        self.assertEqual(am.reach_of(scope, "month", "returns"), "no_path")
        self.assertEqual(am.reach_of(scope, "channel", "orders"), "reaches")
        # one fact per query, never two; and the store table is never an anchor of the reach queries
        for sql in runner.calls:
            self.assertLessEqual(len({TABLE[a] for a in re.findall(r'COUNT\("([^"]+)"\)', sql) if a in TABLE}), 1)
        self.assertFalse(any('COUNT("floor area")' in q for q in runner.calls))

    def test_per_fact_period_row_id_and_entity(self):
        for source in ("export", "queries"):
            scope, _ = _structured(source)
            facts = {f["table"]: f for f in scope["facts"]}
            self.assertEqual(facts["orders"]["period"], "order date", source)
            self.assertEqual(facts["returns"]["period"], "return date", source)
            self.assertEqual((facts["orders"]["row_id"], facts["returns"]["row_id"]), ("order line", "return"))
            self.assertEqual(scope["entity"]["attribute"], "store id", source)    # never a fact's row id
            self.assertEqual(am.guess_entity(scope), "store id")

    def test_one_metric_table_asks_nothing(self):
        one = dict(SCOPE, metrics=SCOPE["metrics"][:2], anchor={"metric": "order lines", "counts": {"order lines": 1000}})
        runner = _Connector()
        out = am.fact_structure(runner, "retail", one)
        self.assertEqual(runner.calls, [])
        self.assertEqual([f["table"] for f in out["facts"]], ["orders"])
        self.assertNotIn("reach", out)

    def test_replay_waits_for_the_counts_then_for_the_fact_tests(self):
        class Pending:
            calls = 0

            def call(self, tool, args):
                Pending.calls += 1
                raise am.PendingReply(args["query"])
        no_counts = dict(SCOPE, anchor={"metric": "order lines", "counts": {}})
        out = am.fact_structure(Pending(), "retail", no_counts)
        self.assertIn("anchor counts", out["reach_waiting"])
        self.assertEqual(Pending.calls, 0)
        out = am.fact_structure(Pending(), "retail", dict(SCOPE))
        self.assertIn("fact / dimension tests", out["reach_waiting"])
        self.assertLessEqual(Pending.calls, 3)   # one key test per smaller table per pass, as live stops at a hit

    def test_a_metric_only_round_reuses_reach(self):
        before, _ = _structured("queries")
        again = dict(SCOPE, attributes=[dict(a) for a in SCOPE["attributes"]])
        out = am.reuse_fact_structure(again, before)
        self.assertEqual(out["reach"], before["reach"])
        self.assertIn("reused", out["fact_source"])
        fewer = dict(again, metrics=[m for m in SCOPE["metrics"] if m["name"] != "floor area"])
        self.assertIsNone(am.reuse_fact_structure(fewer, before))   # a table changed: test again

    def test_relationship_pairs_skip_attributes_with_no_common_fact(self):
        scope, _ = _structured("export")
        pairs = {(p["child"], p["parent"]) for p in am.relationship_pairs(scope, max_pairs=500)}
        self.assertFalse(any({c, p} & {"return reason"} and {c, p} & {"month", "channel"} for c, p in pairs))
        self.assertTrue(am.share_a_fact(scope, "store id", "return reason"))


SEMANTICS = """## Schema: retail. Table: Retail Model
### Attributes(Dimensions)
- **store id** (bigint)
- **return reason** (varchar)
- **month** (bigint)
### Metrics(Measures)
- **order lines** (bigint)
sum(orders.{ORDER_LINES})
- **returned units** (double)
sum(returns.{RETURNED_UNITS})
- **floor area** (double)
sum(stores.{FLOOR_AREA})
"""


class ScopeCommandTests(unittest.TestCase):
    def test_scope_reads_facts_and_reach_from_the_export(self):
        class Runner(_Connector):
            def call(self, tool, args):
                if tool == "get_semantics":
                    return {"content": [{"type": "text", "text": SEMANTICS}]}
                return super().call(tool, args)
        with tempfile.TemporaryDirectory() as d:
            exp, out = os.path.join(d, "export.json"), os.path.join(d, "scope.json")
            with open(exp, "w", encoding="utf-8") as f:
                json.dump(_export(), f)
            runner = Runner()
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                code = am.main(["scope", "--project", "retail", "--model", "Retail Model", "--no-profile",
                                "--model-export", exp, "--out", out], runner_factory=lambda a: runner)
            self.assertEqual(code, 0)
            summary = json.loads(printed.getvalue())
            self.assertEqual(summary["fact_tables"], ["orders", "returns"])
            self.assertEqual(summary["dimension_tables_with_metrics"], ["stores"])
            self.assertEqual(summary["reach_from"], "export")
            with open(out, encoding="utf-8") as f:
                scope = json.load(f)
            self.assertEqual(scope["sources"]["export"]["file"], "export.json")
            self.assertEqual(am.reach_of(scope, "return reason", "orders"), "not_reached")
            self.assertEqual(len(runner.calls), 1)   # the anchor counts only


class RelationshipReachTests(unittest.TestCase):
    """Reach follows the model's parent-child relationships too: an attribute reaches a fact when it is a parent
    (grandparent...) of an attribute on it, even on a table no lookup leads to. A child doesn't reach; a
    many-to-many parent doesn't either (checked live on a second multi-fact model, 2026-11-24)."""

    def _graph(self):
        exp = _export()
        m = exp["model"]
        m["attributes"].append({"information": {"name": "Promotion"}, "attributeLookupTable": {"name": "PROMOS"},
                                "forms": [{"expressions": [{"tables": [{"name": "PROMOS"}]}]}]})
        m["attributes"].append({"information": {"name": "Return Channel"}, "attributeLookupTable": {"name": "RCH"},
                                "forms": [{"expressions": [{"tables": [{"name": "RCH"}]}]}]})
        rel = lambda p, c, kind: {"parent": {"name": p}, "child": {"name": c}, "relationshipType": kind}  # noqa: E731
        m["hierarchy"] = {"relationships": [rel("Promotion", "Order Line", "one_to_many"),
                                            rel("Return Channel", "Return", "many_to_many"),
                                            rel("Order Line", "Return", "one_to_many")]}
        return am._export_table_graph(m)

    def test_parents_reach_children_and_many_to_many_dont(self):
        g = self._graph()
        orders, returns = am.reached_attributes("orders", g), am.reached_attributes("returns", g)
        self.assertIn("promotion", orders)               # a parent of the order line, on a table no lookup reaches
        self.assertIn("order line", returns)             # the return's parent
        self.assertIn("promotion", returns)              # and its grandparent
        self.assertNotIn("return", orders)               # a child of the order line: not reached
        self.assertNotIn("return channel", returns)      # many-to-many: grouping by it repeats rows

    def test_a_header_facts_id_is_its_row_id_and_its_own_date_its_period(self):
        view = {"attributes": [_attr("order line", "bigint"), _attr("order date", "date"),
                               dict(_attr("return date", "date"), profile={"distinct_values": 900.0})],
                "facts": [{"table": "orders", "rows": 1000.0}, {"table": "returns", "rows": 100.0}],
                "reach": {"order line": {"orders": {"verdict": "reaches"}, "returns": {"verdict": "reaches"}},
                          "order date": {"orders": {"verdict": "reaches"}},
                          "return date": {"orders": {"verdict": "reaches"}}}}
        fact = view["facts"][0]
        self.assertEqual(am._row_id(view, fact, {"order line"}), "order line")   # also reaches returns
        own = am._own_attributes(view, "orders", None)                            # no export: the table's words
        self.assertEqual(own, {"order line", "order date"})
        self.assertEqual(am.guess_period(view, among=["order date", "return date"], own=own), "order date")
        self.assertEqual(am.guess_period(view, among=["order date", "return date"]), "return date")  # most values


class FormulasFromExportTests(unittest.TestCase):
    """Some connectors show no metric formulas: scope fills them from the export, in the connector's shape."""

    def test_export_expressions_take_the_connector_shape(self):
        cases = {
            "Sum<UseLookupForAttributes=False>([Net Sales]){~+}/ Sum<UseLookupForAttributes=False>([Target]){~+}":
                "Sum({Net Sales})/ Sum({Target})",
            "Count<Distinct=True,UseLookupForAttributes=False>(Order@ID){~+}<[%Filter 1%]>": "Count(Order (ID))",
            "[Returned Units]/ Count<UseLookupForAttributes=False>(Return){~+}": "{Returned Units}/ Count(Return)",
            "Count<UseLookupForAttributes=False>(Order@ID){~+}<[%Filter 1%];+>": "Count(Order (ID))",
            "Sum<UseLookupForAttributes=False>([Balance]){~+,![Status]+}": "Sum({Balance})",
        }
        for text, want in cases.items():
            self.assertEqual(am.connector_formula(text), want)

    def test_missing_formulas_are_filled_and_named(self):
        exp = _export()
        exp["model"]["factMetrics"][2]["function"] = "sum"
        exp["model"]["factMetrics"][2]["fact"]["expressions"][0]["expression"]["text"] = "RETURNED_UNITS"
        exp["model"]["metrics"] = [{"information": {"name": "Return Rate"},
                                    "expression": {"text": "[Returned Units]/ [Quantity]"}}]
        mets = [{"name": "returned units_1", "formula": ""}, {"name": "return rate", "formula": ""},
                {"name": "quantity", "formula": "sum(orders.{QUANTITY})"}, {"name": "unknown", "formula": ""}]
        filled = am.formulas_from_export(mets, exp)
        self.assertEqual(filled, ["returned units_1", "return rate"])
        self.assertEqual(mets[0]["formula"], "sum(RETURNS.RETURNED_UNITS)")   # a metric named like an attribute
        self.assertEqual(mets[1]["formula"], "{Returned Units}/ {Quantity}")
        self.assertEqual(mets[2]["formula"], "sum(orders.{QUANTITY})")         # the connector's own is kept
        self.assertEqual(am.metric_tables({"metrics": [dict(mets[0], type="double")]}).keys(), {"returns"})

    def test_scope_says_when_no_formula_is_shown(self):
        bare = re.sub(r"\nsum\([^)]*\)", "", SEMANTICS)

        class Runner(_Connector):
            def call(self, tool, args):
                if tool == "get_semantics":
                    return {"content": [{"type": "text", "text": bare}]}
                return super().call(tool, args)
        out = am.scope_model(Runner(), "retail", "Retail Model", profile=False)
        self.assertIn("--model-export", out["notes"]["no_formulas"])
        self.assertIsNone(out["anchor"])


class JoinPathTests(unittest.TestCase):
    def test_known_reach_isnt_asked_and_partial_reach_is_one_finding(self):
        scope, _ = _structured("export")
        runner = _Connector()
        out = am.check_join_paths(runner, "retail", scope)
        self.assertEqual([f["anchor"] for f in out["facts"]], ["order lines", "returned units"])  # no store anchor
        self.assertFalse(any('"return reason"' in q and 'COUNT("order lines")' in q for q in runner.calls))
        self.assertEqual(out["attributes"]["return reason"]["order lines"]["verdict"], "not_reached")
        ids = [f["id"] for f in out["findings"]]
        self.assertIn("J-partial-reach", ids)
        self.assertFalse(any(i.startswith("J-") and i not in ("J-partial-reach",) and not i.startswith("J-filter")
                             for i in ids))


class ProbeTests(unittest.TestCase):
    def _probe(self, **kw):
        scope, _ = _structured("export")
        runner = _Connector(kw.pop("ignored", ()))
        return am.probe_objects(runner, "retail", scope, period="order date", **kw), runner

    def test_each_attribute_is_checked_against_a_fact_it_reaches(self):
        out, runner = self._probe(attributes=["return reason", "channel"], metrics=[], calendar_filters=False)
        attrs = {a["name"]: a for a in out["attributes"]}
        self.assertEqual(attrs["return reason"]["anchor"], "returned units")
        self.assertEqual(attrs["return reason"]["issues"], [])
        self.assertNotIn("anchor", attrs["channel"])               # the model's anchor
        self.assertIn("filtered", attrs["return reason"].get("not_asked", {}))   # the order date doesn't reach returns
        self.assertFalse(any('"return reason"' in q and 'COUNT("order lines")' in q for q in runner.calls))

    def test_metrics_use_their_own_fact_and_only_reaching_groupings(self):
        out, runner = self._probe(attributes=[], calendar_filters=False)
        mets = {m["name"]: m for m in out["metrics"]}
        self.assertEqual(mets["returned units"]["grouped_by"]["period"], "return date")
        self.assertEqual(mets["returns counted"]["fact"], "returns")
        self.assertEqual(mets["return rate"].get("fact"), None)    # reads two facts: model-wide as before
        self.assertFalse(any("not the fact table" in i for m in mets.values() for i in m["issues"]))
        self.assertTrue(any("one row per 'store id'" in i for i in mets["floor area"]["issues"]))
        self.assertIsNone(mets["floor area"]["grouped_by"]["period"])
        for q in runner.calls:      # a query never mixes two tables' metrics
            names = re.findall(r'SUM\("([^"]+)"\)', q)
            self.assertLessEqual(len({TABLE.get(n, n) for n in names if n in TABLE}), 1, q)

    def test_an_ignored_calendar_filter_is_join_paths_finding(self):
        out, runner = self._probe(metrics=[], ignored={"year"})   # every attribute: the calendar ones too
        self.assertEqual([f["id"] for f in out["findings"]], ["J-filter-year"])
        self.assertEqual(out["findings"][0]["source"], "probe")
        self.assertEqual(out["calendar_filters"]["month"]["verdict"], "ok")
        self.assertEqual(out["calendar_filters"]["return date"]["anchor"], "returned units")
        self.assertTrue(any("finding J-filter-year" in w for w in out["warnings"]))

    def test_calendar_filters_follow_an_explicit_attribute_list(self):
        out, _ = self._probe(attributes=["month"], metrics=[])
        self.assertEqual(sorted(out["calendar_filters"]), ["month"])


class MetricFactKeyTests(unittest.TestCase):
    def test_counts_and_compounds_find_their_table(self):
        scope, _ = _structured("export")
        mets = {m["name"]: m for m in scope["metrics"]}
        attrs = {a["name"]: a for a in scope["attributes"]}
        self.assertEqual(am._metric_fact_key("returns counted", mets, attrs, scope), "returns")
        self.assertIsNone(am._metric_fact_key("return rate", mets, attrs, scope))
        self.assertEqual(am._metric_fact_key("floor area", mets, attrs, scope), "stores")


if __name__ == "__main__":
    unittest.main()
