import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _hermetic  # noqa: E402,F401  (scrub MSTR_*/proxy env, private secret store)

# Regression tests for the second build_mosaic.py audit (2026-10-05, round 2).
#
# Most tests drive a real bm.MSTR whose session is a RoutedSession: requests are routed by
# (VERB, path regex) to canned responses and recorded together with the changeset header in
# force, so whole commands run without a network. The end-to-end test runs the CLI in a
# subprocess against tests/fake_tenant.py.
import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "build-mosaic-model", "scripts")
sys.path.insert(0, SCRIPTS)

import build_mosaic as bm  # noqa: E402
from fake_tenant import PROJECT_ID, FakeTenant  # noqa: E402

BASE = "https://tenant.example.test/MicroStrategyLibrary"
DS = "D" * 32
A_ID, C_ID, U_ID = "A" * 32, "C" * 32, "E" * 32


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.text = "" if body is None else json.dumps(body)
        self.headers = headers or {}
        self.url = BASE

    def json(self):
        if self._body is None:
            raise ValueError("empty body")
        return self._body


class RoutedSession:
    """Stands in for MSTR.s. Routes (VERB, path regex) to a Resp or to a callable(call) ->
    Resp; unrouted calls get a 404. Records every call with the changeset header in force."""

    def __init__(self, routes):
        self.routes = list(routes.items())
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.calls = []

    def request(self, method, url, **kw):
        path = url[len(BASE):] if url.startswith(BASE) else url
        call = types.SimpleNamespace(
            method=method.upper(), path=path, kw=kw,
            changeset=(kw.get("headers") or {}).get("X-MSTR-MS-Changeset") or self.headers.get("X-MSTR-MS-Changeset"))
        self.calls.append(call)
        for (verb, pattern), resp in self.routes:
            if verb == call.method and re.fullmatch(pattern, path.split("?")[0]):
                return resp(call) if callable(resp) else resp
        return Resp(404, {"code": "ERR001", "message": f"unrouted {call.method} {path}"})

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def put(self, url, **kw):
        return self.request("PUT", url, **kw)

    def patch(self, url, **kw):
        return self.request("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)


def make_m(routes, *, login=True):
    """A real MSTR on a RoutedSession, signed in with a borrowed token (no sign-in calls)."""
    args = types.SimpleNamespace(base=BASE, project_id="P" * 32, user="", password="", login_mode=1,
                                 verbose=False, auth_token="TOK", identity_token="IDT",
                                 session_cookie="", ingress_cookie="", auth_method=None)
    m = bm.MSTR(args)
    m.s = RoutedSession(routes)
    if login:
        m.login(identity=True)
    return m


def changeset_routes():
    counter = {"n": 0}

    def new_cs(call):
        counter["n"] += 1
        return Resp(201, {"id": f"CS{counter['n']}"})
    return {("POST", r"/api/model/changesets"): new_cs,
            ("POST", r"/api/model/changesets/\w+/commit"): Resp(204),
            ("DELETE", r"/api/model/changesets/\w+"): Resp(204)}


def calls(m, method, pattern=".*"):
    return [c for c in m.s.calls if c.method == method and re.fullmatch(pattern, c.path.split("?")[0])]


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
        yield out


# ── #1 ACL ────────────────────────────────────────────────────────────────────

class AclMergeTests(unittest.TestCase):
    def test_deny_keeps_the_trustees_other_grants_and_its_subtype(self):
        current = {"G": {"granted": 255, "denied": 0, "subType": "user_group", "name": "Admins"}}
        merged = bm._merge_acl(current, bm._acl_changes([], ["G:delete"]))
        self.assertEqual(merged["G"], {"name": "Admins", "granted": 255 & ~16, "denied": 16, "subType": "user_group"})

    def test_grant_clears_the_same_bits_from_the_deny_set_and_leaves_others_alone(self):
        current = {"G": {"granted": 0, "denied": 255, "subType": "user"},
                   "OTHER": {"granted": 1, "denied": 0, "subType": "user"}}
        merged = bm._merge_acl(current, bm._acl_changes(["G:view"], []))
        self.assertEqual((merged["G"]["granted"], merged["G"]["denied"]), (197, 255 & ~197))
        self.assertEqual(merged["OTHER"], current["OTHER"])

    def test_replace_mode_sets_exactly_the_given_rights(self):
        current = {"G": {"granted": 255, "denied": 0, "subType": "user"}}
        merged = bm._merge_acl(current, bm._acl_changes(["G:read"], []), replace=True)
        self.assertEqual((merged["G"]["granted"], merged["G"]["denied"]), (4, 0))

    def test_granting_and_denying_the_same_right_stops_the_run(self):
        with self.assertRaises(SystemExit), quiet():
            bm._acl_changes(["G:read"], ["G:read"])

    def test_unknown_trustee_type_stops_the_run(self):
        with self.assertRaises(SystemExit), quiet():
            bm._parse_acl_entries(["G:read:role"], "grant")

    def test_apply_patches_the_merged_acl_and_looks_up_a_new_trustees_type(self):
        routes = {**changeset_routes(),
                  ("GET", r"/api/model/dataModels/M/objects/M/acl"): Resp(200, {"acl": {
                      "OWNER": {"granted": 255, "denied": 0, "subType": "user"}}}),
                  ("PATCH", r"/api/model/dataModels/M/objects/M/acl"): Resp(200, {}),
                  ("GET", r"/api/users/NEWGROUP"): Resp(404, {}),
                  ("GET", r"/api/usergroups/NEWGROUP"): Resp(200, {"id": "NEWGROUP"})}
        m = make_m(routes)
        with quiet():
            self.assertTrue(bm._apply_acl(m, "M", ["NEWGROUP:view"], model_id="M"))
        (patch,) = calls(m, "PATCH")
        acl = patch.kw["json"]["acl"]
        self.assertEqual(acl["OWNER"]["granted"], 255)
        self.assertEqual(acl["NEWGROUP"], {"granted": 197, "denied": 0, "subType": "user_group"})
        self.assertEqual(patch.changeset, "CS1")
        self.assertEqual(m.open_changesets, set())


# ── #2 security filters ───────────────────────────────────────────────────────

class SecurityFilterMemberTests(unittest.TestCase):
    def lookup(self, users, groups, names):
        routes = {("GET", r"/api/users"): Resp(200, users),
                  ("GET", r"/api/searches/results"): Resp(200, {"result": []}),
                  ("GET", r"/api/usergroups"): Resp(200, groups),
                  ("GET", r"/api/users/\w+"): Resp(404, {}),
                  ("GET", r"/api/usergroups/\w+"): Resp(404, {})}
        return bm._resolve_member_ids(make_m(routes), names)

    def test_a_user_and_a_group_with_the_same_name_are_ambiguous(self):
        with self.assertRaises(SystemExit), quiet():
            self.lookup([{"id": A_ID, "username": "fin", "fullName": "Finance"}],
                        [{"id": C_ID, "name": "Finance"}], ["Finance"])

    def test_a_literal_id_must_exist_as_a_user_or_group(self):
        with self.assertRaises(SystemExit), quiet():
            self.lookup([], [], [U_ID])

    def test_a_literal_id_is_used_as_given_when_principals_cannot_be_read(self):
        routes = {("GET", r"/api/users/\w+"): Resp(403, {"code": "ERR014"}),
                  ("GET", r"/api/usergroups/\w+"): Resp(403, {"code": "ERR014"})}
        with quiet():
            self.assertEqual(bm._resolve_member_ids(make_m(routes), [U_ID]), [U_ID])

    def test_an_acl_trustee_that_cannot_be_typed_needs_an_explicit_type(self):
        routes = {("GET", r"/api/users/\w+"): Resp(403, {}), ("GET", r"/api/usergroups/\w+"): Resp(403, {})}
        with self.assertRaises(SystemExit), quiet():
            bm._trustee_subtype(make_m(routes), U_ID)

    def test_a_unique_exact_group_match_resolves(self):
        ids = self.lookup([], [{"id": C_ID, "name": "Analysts"}, {"id": A_ID, "name": "Analysts EMEA"}],
                          ["Analysts"])
        self.assertEqual(ids, [C_ID])

    def test_member_patch_falls_back_to_the_spec_path_on_400(self):
        def members(call):
            path = call.kw["json"]["operationList"][0]["path"]
            return Resp(204) if path == "/members" else Resp(400, {"code": "ERR", "message": "Invalid path"})
        m = make_m({("PATCH", r"/api/dataModels/M/securityFilters/SF/members"): members})
        self.assertTrue(bm._assign_security_filter_members(m, "M", "SF", [U_ID]))
        sent = [c.kw["json"]["operationList"][0]["path"] for c in calls(m, "PATCH")]
        self.assertEqual(sent, ["/Members", "/members"])

    def test_unbound_members_are_a_failure_not_a_warning(self):
        routes = {**changeset_routes(),
                  ("POST", r"/api/model/dataModels/M/securityFilters"): Resp(201, {"information": {"objectId": "SF"}}),
                  ("PATCH", r"/api/dataModels/M/securityFilters/SF/members"): Resp(400, {"code": "ERR"})}
        m = make_m(routes)
        with quiet():
            ok = bm._create_security_filter(m, "M", {"name": "Region", "qualification": {"tree": {}},
                                                     "member_ids": [U_ID]})
        self.assertFalse(ok)
        self.assertEqual(len(calls(m, "POST", r"/api/model/changesets/\w+/commit")), 1)
        (post,) = calls(m, "POST", r"/api/model/dataModels/M/securityFilters")
        self.assertNotIn("changesetId", post.path)
        self.assertEqual(post.changeset, "CS1")

    def test_config_mapping_with_a_qualification_object_is_accepted(self):
        prepared = bm._prepare_security_filter(make_m({}), {"name": "SF", "members": [],
                                                            "qualification": {"tree": {"type": "x"}}})
        self.assertEqual(prepared["qualification"], {"tree": {"type": "x"}})
        with self.assertRaises(SystemExit), quiet():
            bm._prepare_security_filter(make_m({}), {"name": "SF"})


# ── #3 build: post-build failures keep the model id; no certify after failures ─

def build_routes(member_status):
    attr_n = {"n": 0}

    def new_attr(call):
        attr_n["n"] += 1
        return Resp(201, {"information": {"objectId": f"A{attr_n['n']}"}, "forms": [{"id": bm.FORM_ID}]})
    return {
        **changeset_routes(),
        ("GET", r"/api/datasources/\w+/catalog/namespaces"): Resp(200, {"namespaces": [{"name": "PUBLIC", "id": "NS1"}]}),
        ("GET", r"/api/datasources/\w+/catalog/namespaces/\w+/tables/.+"): Resp(200, {"columns": [
            {"name": "ORDER_ID", "dataType": {"type": "integer", "precision": 10, "scale": 0}},
            {"name": "REGION", "dataType": {"type": "utf8_char", "precision": 20, "scale": 0}},
            {"name": "AMOUNT", "dataType": {"type": "double", "precision": 15, "scale": 2}}]}),
        ("POST", r"/api/model/dataModels"): Resp(201, {"information": {"objectId": "MODEL1"}}),
        ("POST", r"/api/model/dataModels/MODEL1/tables"): Resp(201, {"information": {"objectId": "T1"}}),
        ("POST", r"/api/model/dataModels/MODEL1/attributes"): new_attr,
        ("PATCH", r"/api/model/dataModels/MODEL1/attributes/\w+"): Resp(200, {}),
        ("POST", r"/api/model/dataModels/MODEL1/factMetrics"): Resp(201, {"information": {"objectId": "F1"}}),
        ("GET", r"/api/model/dataModels/MODEL1/attributes/\w+"): Resp(200, {"relationships": []}),
        ("PUT", r"/api/model/dataModels/MODEL1/attributes/\w+/relationships"): Resp(200, {}),
        ("POST", r"/api/model/dataModels/MODEL1/securityFilters"): Resp(201, {"information": {"objectId": "SF1"}}),
        ("PATCH", r"/api/dataModels/MODEL1/securityFilters/SF1/members"):
            Resp(member_status, {"code": "ERR", "message": "rejected"} if member_status >= 400 else None),
        ("GET", r"/api/users/\w+"): Resp(200, {"id": U_ID}),
        ("PUT", r"/api/objects/MODEL1/certify"): Resp(200, {}),
    }


def build_args(**overrides):
    base = dict(name="Orders", source=[f"{DS}:PUBLIC:ORDERS"], instance=None, schema=None, tables=[],
                dest_folder="F" * 32, data_serve_mode="connect_live", attr_cols=[], metric_cols=[], skip_cols=[],
                skip_relationships=False, dictionary=None, erd=[], conformance_map=None, fk_map=None,
                security_filter=[f"Region SF={A_ID}=East|{U_ID}"], grant=[], deny=[], translate=[],
                certify=True, publish=False)
    base.update(overrides)
    return types.SimpleNamespace(**base)


class BuildPostStepTests(unittest.TestCase):
    def run_build(self, member_status):
        m = make_m(build_routes(member_status))
        code = None
        with quiet() as out:
            try:
                bm.cmd_build(m, build_args())
            except SystemExit as e:
                code = e.code
        return m, code, json.loads(out.getvalue())

    def test_unbound_security_filter_reports_the_model_and_skips_certify(self):
        m, code, summary = self.run_build(400)
        self.assertEqual(code, 1)
        self.assertEqual(summary["model_id"], "MODEL1")
        self.assertGreaterEqual(summary["failures"]["post_build"], 1)
        self.assertEqual(summary["post_build"]["security filter Region SF"], "failed")
        self.assertTrue(summary["post_build"]["certify"].startswith("skipped"))
        self.assertEqual(calls(m, "PUT", r"/api/objects/MODEL1/certify"), [])
        self.assertEqual(m.open_changesets, set())

    def test_clean_build_certifies_last(self):
        m, code, summary = self.run_build(204)
        self.assertIsNone(code)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["post_build"]["certify"], "ok")
        self.assertEqual(m.s.calls[-1].path.split("?")[0], "/api/objects/MODEL1/certify")

    def test_a_bad_post_build_spec_stops_before_anything_is_created(self):
        m = make_m(build_routes(204))
        with self.assertRaises(SystemExit), quiet():
            bm.cmd_build(m, build_args(grant=[f"{U_ID}:not-a-right"]))
        self.assertEqual(calls(m, "POST", r"/api/model/changesets"), [])
        self.assertEqual(calls(m, "POST", r"/api/model/dataModels"), [])

    def test_config_grants_and_translations_map_to_the_cli_syntax(self):
        self.assertEqual(bm._config_acl_entry({"trustee": "T", "rights": "view", "type": "user_group"}, "c"),
                         "T:view:user_group")
        self.assertEqual(bm._config_acl_entry({"trustee": "T", "rights": ["browse", "read"]}, "c"), "T:browse,read")
        self.assertEqual(bm._config_translation_entry({"object": "O", "locale": "1031", "text": "Umsatz"}, "c"),
                         "O:1031=Umsatz")
        with self.assertRaises(SystemExit), quiet():
            bm._config_acl_entry({"trustee": "T"}, "c")

    def test_translations_default_to_the_model_roots_subtype(self):
        self.assertEqual(list(bm._parse_translation_entries(["O:1033=Sales"])), [("O", "report_emma_cube")])
        self.assertEqual(list(bm._parse_translation_entries(["O:data_model:1033=Sales"])),
                         [("O", "report_emma_cube")])


# ── #4 build-from-schema-objects ─────────────────────────────────────────────

class BuildFromSchemaObjectsTests(unittest.TestCase):
    def test_model_is_created_inside_a_changeset_and_published_through_the_verified_flow(self):
        routes = {
            **changeset_routes(),
            ("GET", r"/api/model/attributes/\w+"): Resp(200, {"information": {"name": "Region"}, "relationships": []}),
            ("GET", r"/api/model/tables/\w+"): Resp(200, {"physicalTable": {
                "databaseInstance": {"objectId": DS}, "namespace": "PUBLIC", "tableName": "ORDERS"}}),
            ("GET", r"/api/datasources/\w+/catalog/namespaces"): Resp(200, {"namespaces": [{"name": "PUBLIC", "id": "NS1"}]}),
            ("GET", r"/api/datasources/\w+/catalog/namespaces/\w+/tables/.+"): Resp(200, {"columns": [
                {"name": "REGION", "dataType": {"type": "utf8_char", "precision": 20, "scale": 0}}]}),
            ("POST", r"/api/model/dataModels"): Resp(201, {"information": {"objectId": "M2"}}),
            ("POST", r"/api/model/dataModels/M2/tables"): Resp(201, {"information": {"objectId": "T1"}}),
            ("POST", r"/api/model/dataModels/M2/attributes"): Resp(201, {"information": {"objectId": "A1"}}),
            ("GET", r"/api/model/dataModels/M2/tables"): Resp(200, {"tables": [{"information": {"objectId": "T1"}}]}),
            ("POST", r"/api/dataModels/M2/instances"): Resp(204, None, {"X-MSTR-DataModelInstanceId": "I1"}),
            ("POST", r"/api/dataModels/M2/publish"): Resp(204),
            ("GET", r"/api/dataModels/M2/publishStatus"): Resp(200, {"status": 1, "tables": [
                {"id": "T1", "status": "completed"}]}),
            ("DELETE", r"/api/dataModels/M2/instances/I1"): Resp(204),
        }
        m = make_m(routes)
        args = types.SimpleNamespace(name="From Classic", attribute_ids="CA" + "0" * 30, fact_ids="", metric_ids="",
                                     instance_id="", schema="", data_serve_mode="connect_live", publish=True,
                                     dest_folder="F" * 32, review_file="", use_batch=False)
        with mock.patch.object(bm.sot, "extract_table_ids_from_attribute", return_value={"CT1"}), \
             mock.patch.object(bm.sot, "extract_table_ids_from_fact", return_value=set()), \
             mock.patch.object(bm.sot, "translate_attribute",
                               return_value=({"information": {"name": "Region"}}, [])), \
             quiet() as out:
            bm.cmd_build_from_schema_objects(m, args)
        review = json.loads(out.getvalue())
        self.assertTrue(review["ok"])
        self.assertTrue(review["published"])
        (create,) = calls(m, "POST", r"/api/model/dataModels")
        self.assertEqual(create.changeset, "CS1")
        self.assertFalse([c for c in m.s.calls if c.path.startswith("/api/cubes")])
        self.assertEqual(len(calls(m, "POST", r"/api/model/batch")), 0)
        self.assertEqual(len(calls(m, "DELETE", r"/api/dataModels/M2/instances/I1")), 1)


# ── #5 relationship merge ────────────────────────────────────────────────────

def rel(parent, child, table="T", kind="one_to_many"):
    return {"parent": {"objectId": parent}, "child": {"objectId": child},
            "relationshipTable": {"objectId": table}, "relationshipType": kind}


class RelationshipMergeTests(unittest.TestCase):
    def merge(self, existing_body, new_rels):
        m = make_m({("GET", r"/api/model/dataModels/M/attributes/C"): Resp(200, existing_body),
                    ("PUT", r"/api/model/dataModels/M/attributes/C/relationships"): Resp(200, {})})
        return m, bm.put_relationships_merged(m, "M", "C", new_rels, "CS")

    def test_the_specs_wrapped_shape_is_read_not_mistaken_for_none(self):
        m, (ok, added, total, _) = self.merge({"relationships": {"relationships": [rel("P0", "C")]}}, [rel("P1", "C")])
        self.assertEqual((ok, added, total), (True, 1, 2))
        (put,) = calls(m, "PUT")
        self.assertEqual(put.kw["headers"], {"X-MSTR-MS-Changeset": "CS"})
        self.assertNotIn("changesetId", put.path)

    def test_an_unknown_shape_refuses_the_destructive_put(self):
        m, (ok, *_rest) = self.merge({"relationships": "P0->C"}, [rel("P1", "C")])
        self.assertFalse(ok)
        self.assertEqual(calls(m, "PUT"), [])

    def test_a_reverse_edge_self_reference_or_type_change_is_refused(self):
        for new in ([rel("P", "C")], [rel("C", "C")], [rel("C", "P", kind="many_to_many")]):
            existing = {"relationships": [rel("C", "P")]}
            m, (ok, *_rest, err) = self.merge(existing, new)
            self.assertFalse(ok, new)
            self.assertTrue(err)
            self.assertEqual(calls(m, "PUT"), [])


# ── #6 batch ─────────────────────────────────────────────────────────────────

class BatchMappingTests(unittest.TestCase):
    def test_partial_results_are_matched_by_name_not_position(self):
        ops = [{"op": "create", "path": "/attributes", "value": {"information": {"name": n}}} for n in "ABC"]
        body = {"results": [{"status": 201, "response": {"information": {"objectId": "X3", "name": "C"}}}]}
        m = make_m({("POST", r"/api/model/batch"): Resp(207, body)})
        with quiet():
            passed, failed = bm.batch_call(m, "M", "CS", ops, atomic=False)
        self.assertEqual([r["_index"] for r in passed], [2])

    def test_per_op_creation_is_the_default(self):
        ops = [{"op": "create", "path": "/attributes", "value": {"information": {"name": "A"}}}]
        m = make_m({("POST", r"/api/model/dataModels/M/attributes"): Resp(201, {"information": {"objectId": "X"}})})
        passed, failed = bm.create_objects(m, "M", "CS", ops)
        self.assertEqual(([p["_index"] for p in passed], failed), ([0], []))
        self.assertEqual(calls(m, "POST", r"/api/model/batch"), [])


# ── #7 kill-sessions ─────────────────────────────────────────────────────────

def stamp_minus_5h(seconds_ago):
    """The monitor's local-time format at UTC-5 for a moment `seconds_ago` in the past."""
    t = time.time() - seconds_ago - 5 * 3600
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000-0500"


class KillSessionsTests(unittest.TestCase):
    def routes(self, user_status=200):
        conns = [{"id": "fresh", "username": "alee", "dateLastJobSubmitted": stamp_minus_5h(60)},
                 {"id": "stale", "username": "alee", "dateLastJobSubmitted": stamp_minus_5h(2 * 3600)}]
        return {("GET", r"/api/sessions/userInfo"): Resp(200, {"id": "U1", "fullName": "Ann Lee"}),
                ("GET", r"/api/users/U1"): Resp(user_status, {"username": "alee"}),
                ("GET", r"/api/monitors/iServer/nodes"): Resp(200, {"nodes": [{"name": "n1"}]}),
                ("GET", r"/api/monitors/userConnections"): Resp(200, {"userConnections": conns, "totalFiltered": 2}),
                ("DELETE", r"/api/monitors/userConnections/\w+"): Resp(204)}

    def test_the_utc_offset_is_kept_when_measuring_idle_time(self):
        m = make_m(self.routes())
        with quiet():
            bm.cmd_kill_sessions(m, types.SimpleNamespace(project="", idle_minutes=5.0, yes=True))
        self.assertEqual([c.path for c in calls(m, "DELETE")], ["/api/monitors/userConnections/stale"])

    def test_without_the_login_name_it_will_not_disconnect_anything(self):
        m = make_m(self.routes(user_status=403))
        with self.assertRaises(SystemExit), quiet():
            bm.cmd_kill_sessions(m, types.SimpleNamespace(project="", idle_minutes=5.0, yes=True))
        self.assertEqual(calls(m, "DELETE"), [])


# ── #8 lock release ──────────────────────────────────────────────────────────

LOCK_ERROR = {"errors": [{"code": "8004cc41", "additionalProperties": {
    "existingLock": {"ownerId": "U1", "comment": "<LOCKID>ABC123</LOCKID>"}}}]}


class LockReleaseTests(unittest.TestCase):
    def routes(self, owner="U1"):
        return {("GET", r"/api/sessions/userInfo"): Resp(200, {"id": "U1"}),
                ("GET", r"/api/model/schema/lock"): Resp(200, {"ownerId": owner, "ownerName": "someone",
                                                               "comment": "<LOCKID>ABC123</LOCKID>"}),
                ("DELETE", r"/api/model/changesets/ABC123"): Resp(500, {"code": "8004cb15"}),
                ("DELETE", r"/api/model/schema/lock"): Resp(204)}

    def test_own_lock_falls_back_to_deleting_the_schema_lock(self):
        m = make_m(self.routes())
        with quiet():
            self.assertEqual(bm._release_own_lock(m, LOCK_ERROR), "ABC123")
        self.assertEqual(len(calls(m, "DELETE", r"/api/model/schema/lock")), 1)

    def test_someone_elses_lock_is_left_alone(self):
        m = make_m(self.routes(owner="U2"))
        with quiet():
            self.assertIsNone(bm._release_own_lock(m, LOCK_ERROR))
        self.assertEqual(calls(m, "DELETE"), [])

    def test_this_runs_own_changeset_is_never_released(self):
        m = make_m(self.routes())
        m.open_changesets.add("ABC123")
        with quiet():
            self.assertIsNone(bm._release_own_lock(m, LOCK_ERROR))
        self.assertEqual(calls(m, "DELETE"), [])

    def test_patch_fact_metrics_no_longer_releases_locks_by_default(self):
        seen = {}

        def fake_open_cs(m, **kw):
            seen.update(kw)
            raise SystemExit(2)
        store = {"factMetrics": [{"information": {"objectId": "F1", "name": "Units"}}]}
        m = make_m({("GET", r"/api/model/dataModels/M/factMetrics"): Resp(200, store),
                    ("GET", r"/api/model/dataModels/M/factMetrics/F1"): Resp(200, {"function": "sum"})})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"metrics": [{"name": "Units", "function": "avg"}]}, f)
        try:
            with mock.patch.object(bm, "open_cs", fake_open_cs), self.assertRaises(SystemExit), quiet():
                bm.cmd_patch_fact_metrics(m, types.SimpleNamespace(model_id="M", spec=f.name, dry_run=False))
        finally:
            os.unlink(f.name)
        self.assertFalse(seen.get("release_self_locks"))


# ── #9 publish / refresh ─────────────────────────────────────────────────────

def publish_routes(status_resp):
    return {("GET", r"/api/model/dataModels/M/tables"): Resp(200, {"tables": [{"information": {"objectId": "T1"}}]}),
            ("POST", r"/api/dataModels/M/instances"): Resp(204, None, {"X-MSTR-DataModelInstanceId": "I1"}),
            ("POST", r"/api/dataModels/M/publish"): Resp(204),
            ("GET", r"/api/dataModels/M/publishStatus"): status_resp,
            ("DELETE", r"/api/dataModels/M/instances/I1"): Resp(204)}


class PublishTests(unittest.TestCase):
    def test_a_status_that_never_lists_tables_is_decided_by_the_model_answering(self):
        # observed on a live tenant: publishStatus says status 1 with no tables long after the load finished
        state = {"probes": 0}

        def probe(call):
            state["probes"] += 1
            if state["probes"] < 3:          # before the trigger, and once while loading
                return Resp(500, {"iServerCode": -2147072488, "message": "Intelligent Cube is not published"})
            return Resp(200, {"result": {"data": {}}})
        routes = publish_routes(Resp(200, {"status": 1, "tables": []}))
        routes[("POST", r"/api/v2/cubes/M/instances")] = probe
        m = make_m(routes)
        with mock.patch.object(bm.time, "time", side_effect=[i * 20.0 for i in range(1000)]), \
             mock.patch.object(bm.time, "sleep"), quiet():
            bm._mosaic_publish_verified(m, "M", poll_seconds=600, poll_interval=0.01)
        self.assertEqual(state["probes"], 3)
        self.assertEqual(len(calls(m, "DELETE")), 1)     # finished: instance deleted

    def test_a_model_too_large_to_answer_whole_is_published(self):
        # observed live: a model with three fact tables, loaded, refuses the whole-model probe for its row limit
        state = {"probes": 0}

        def probe(call):
            state["probes"] += 1
            if state["probes"] < 2:
                return Resp(500, {"iServerCode": -2147072488, "message": "Intelligent Cube is not published"})
            return Resp(500, {"iServerCode": -2147180907, "message": "(The number of intermediate rows 50001324 exceeds the maximum number of intermediate rows 50000000"}) \
                if state["probes"] == 2 else Resp(500, {"message": "(A cartesian join is detected and execution is aborted"})
        routes = publish_routes(Resp(200, {"status": 1, "tables": []}))
        routes[("POST", r"/api/v2/cubes/M/instances")] = probe
        m = make_m(routes)
        with mock.patch.object(bm.time, "time", side_effect=[i * 20.0 for i in range(1000)]), \
             mock.patch.object(bm.time, "sleep"), quiet():
            bm._mosaic_publish_verified(m, "M", poll_seconds=600, poll_interval=0.01)
        self.assertEqual(state["probes"], 2)

    def test_tables_said_completed_do_not_finish_a_first_publish_until_the_model_answers(self):
        # observed live (a cross-store model): every table said completed within seconds; the model loaded for minutes
        state = {"probes": 0}

        def probe(call):
            state["probes"] += 1
            if state["probes"] < 4:
                return Resp(500, {"iServerCode": -2147072488, "message": "Intelligent Cube is not published"})
            return Resp(200, {"result": {"data": {}}})
        routes = publish_routes(Resp(200, {"status": 1, "tables": [{"status": "completed"}]}))
        routes[("POST", r"/api/v2/cubes/M/instances")] = probe
        m = make_m(routes)
        with mock.patch.object(bm.time, "time", side_effect=[i * 20.0 for i in range(1000)]), \
             mock.patch.object(bm.time, "sleep"), quiet():
            bm._mosaic_publish_verified(m, "M", poll_seconds=600, poll_interval=0.01)
        self.assertEqual(state["probes"], 4)
        self.assertEqual(len(calls(m, "DELETE")), 1)

    def test_tables_completed_but_a_silent_model_is_published_once_more(self):
        # observed live: tables said completed, the model never loaded; a second publish loaded it in seconds
        state = {"probes": 0, "publishes": 0}

        def probe(call):
            state["probes"] += 1
            return Resp(200, {"result": {}}) if state["publishes"] >= 2 else \
                Resp(500, {"iServerCode": -2147072488, "message": "Intelligent Cube is not published"})

        def publish(call):
            state["publishes"] += 1
            return Resp(204)
        routes = publish_routes(Resp(200, {"status": 1, "tables": [{"status": "completed"}]}))
        routes[("POST", r"/api/v2/cubes/M/instances")] = probe
        routes[("POST", r"/api/dataModels/M/publish")] = publish
        m = make_m(routes)
        with mock.patch.object(bm.time, "time", side_effect=[i * 20.0 for i in range(2000)]), \
             mock.patch.object(bm.time, "sleep"), quiet():
            bm._mosaic_publish_verified(m, "M", poll_seconds=1200, poll_interval=0.01)
        self.assertEqual(state["publishes"], 2)

    def test_a_model_that_already_answers_still_waits_for_its_tables(self):
        routes = publish_routes(Resp(200, {"status": 1, "tables": [{"status": "completed"}]}))
        routes[("POST", r"/api/v2/cubes/M/instances")] = Resp(200, {"result": {}})
        m = make_m(routes)
        with quiet():
            bm._mosaic_publish_verified(m, "M", poll_seconds=30, poll_interval=0.01)
        self.assertEqual(len(calls(m, "POST", r"/api/v2/cubes/M/instances")), 1)    # only the check before the trigger

    def test_schema_comparison_error_fails_fast(self):
        m = make_m(publish_routes(Resp(200, {"status": 1, "tables": [{"status": "schema_comparison_error"}]})))
        started = time.monotonic()
        with self.assertRaises(SystemExit), quiet():
            bm._mosaic_publish_verified(m, "M", poll_seconds=30, poll_interval=0.01)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(calls(m, "DELETE")), 1)     # finished (failed): instance deleted

    def test_a_network_error_while_polling_keeps_the_instance(self):
        def drop(call):
            raise requests.ConnectionError("connection reset")
        m = make_m(publish_routes(drop))
        with self.assertRaises(requests.ConnectionError), quiet():
            bm._mosaic_publish_verified(m, "M", poll_seconds=30, poll_interval=0)
        self.assertEqual(calls(m, "DELETE"), [])

    def test_refresh_type_reaches_the_publish_body(self):
        m = make_m(publish_routes(Resp(200, {"status": 1, "tables": [{"status": "completed"}]})))
        with quiet():
            bm._mosaic_publish_verified(m, "M", poll_interval=0, refresh_policy=bm._refresh_policy("incremental"))
        (publish,) = calls(m, "POST", r"/api/dataModels/M/publish")
        self.assertEqual(publish.kw["json"], {"tables": [{"id": "T1", "refreshPolicy": "upsert"}]})

    def test_tables_are_listed_page_by_page(self):
        def pages(call):
            offset = call.kw["params"]["offset"]
            n = 1000 if offset == 0 else 500
            return Resp(200, {"tables": [{"information": {"objectId": f"T{offset + i}"}} for i in range(n)]})
        m = make_m({("GET", r"/api/model/dataModels/M/tables"): pages})
        items, failed = bm._list_all(m, "/api/model/dataModels/M/tables", "tables")
        self.assertEqual((len(items), failed, len(m.s.calls)), (1500, None, 2))

    def test_the_reported_total_wins_over_a_capped_page_size(self):
        def pages(call):
            offset = call.kw["params"]["offset"]
            n = min(500, 1200 - offset)            # the server caps pages at 500 rows
            return Resp(200, {"total": 1200,
                              "tables": [{"information": {"objectId": f"T{offset + i}"}} for i in range(n)]})
        m = make_m({("GET", r"/api/model/dataModels/M/tables"): pages})
        items, _ = bm._list_all(m, "/api/model/dataModels/M/tables", "tables")
        self.assertEqual((len(items), len(m.s.calls)), (1200, 3))

    def test_a_server_that_ignores_offset_does_not_loop(self):
        page = Resp(200, {"tables": [{"information": {"objectId": f"T{i}"}} for i in range(1000)]})
        m = make_m({("GET", r"/api/model/dataModels/M/tables"): page})
        items, _ = bm._list_all(m, "/api/model/dataModels/M/tables", "tables")
        self.assertEqual((len(items), len(m.s.calls)), (1000, 2))


# ── #10 classify ─────────────────────────────────────────────────────────────

class ClassifyTests(unittest.TestCase):
    def classify(self, model_read_status, ext_type):
        m = make_m({("GET", r"/api/objects/X"): Resp(200, {"subtype": 779, "extType": ext_type, "name": "n"}),
                    ("GET", r"/api/model/dataModels/X"): Resp(model_read_status, {})})
        info = bm.classify_object_surface(m, "X")
        (probe,) = calls(m, "GET", r"/api/model/dataModels/X")
        self.assertIsNone(probe.changeset)        # optional in the spec: read without a changeset
        return info["surface"]

    def test_the_model_read_decides_and_ext_type_is_the_fallback(self):
        self.assertEqual(self.classify(200, 3), "mosaic_data_model")
        self.assertEqual(self.classify(404, 448), "data_import_cube")
        self.assertEqual(self.classify(400, 448), "mosaic_data_model")
        self.assertEqual(self.classify(400, 3), "data_import_cube")


# ── #11 ERD parsing ──────────────────────────────────────────────────────────

DDL = """
-- header comment with a REFERENCES word in it
CREATE TABLE IF NOT EXISTS orders (
  order_id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  amount NUMERIC(10, 2)
);
CREATE TABLE sales.line_items (
  line_id INT,
  order_id INT REFERENCES public.orders (order_id),
  CONSTRAINT fk_sku FOREIGN KEY (sku) REFERENCES products(sku),
  FOREIGN KEY (a, b) REFERENCES pairs(x, y)
)
CREATE TABLE "Returns" ("Return ID" INT, "Order ID" INT REFERENCES "Orders"("Order ID"))
ALTER TABLE ONLY payments ADD CONSTRAINT fk_ord FOREIGN KEY (order_id) REFERENCES orders (order_id);
"""


class ErdTests(unittest.TestCase):
    def parse(self, text, suffix):
        with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8") as f:
            f.write(text)
        try:
            return bm.load_erd(f.name)
        finally:
            os.unlink(f.name)

    def test_common_ddl_dialects(self):
        got = {(r["child"], r["parent"]) for r in self.parse(DDL, ".sql")}
        self.assertEqual(got, {("orders.customer_id", "customers.id"),
                               ("line_items.order_id", "orders.order_id"),
                               ("line_items.sku", "products.sku"),
                               ("Returns.Order ID", "Orders.Order ID"),
                               ("payments.order_id", "orders.order_id")})

    def test_dbml_and_mermaid_still_parse(self):
        rels = self.parse('Ref: posts.user_id > users.id\nCUSTOMERS ||--o{ ORDERS : customer_id\n', ".txt")
        self.assertEqual({(r["child"], r["parent"]) for r in rels},
                         {("posts.user_id", "users.id"), ("ORDERS.customer_id", "CUSTOMERS.customer_id")})

    def test_long_tokens_parse_in_linear_time(self):
        started = time.monotonic()
        self.assertEqual(self.parse("a" * 200_000 + "\n" + "Ref" * 20_000 + "\n", ".txt"), [])
        self.assertLess(time.monotonic() - started, 2.0)


# ── #12 / #14 patch-model-object, unverified commands ────────────────────────

def pmo_args(**kw):
    base = dict(yes=True, json='{"information": {"name": "x"}}', json_file=None, model_id="M", object_id="O",
                method=None, schema_edit=False, show_expression_as=None, fields=None, show_advanced_properties=False,
                before_out=None, include_before=False, text_limit=8000, kind="attribute")
    base.update(kw)
    return types.SimpleNamespace(**base)


class PatchModelObjectTests(unittest.TestCase):
    def test_a_verb_the_spec_lacks_stops_before_sign_in(self):
        m = make_m({}, login=False)
        with self.assertRaises(SystemExit), quiet():
            bm.cmd_patch_model_object(m, pmo_args(kind="attribute", method="PUT"))
        self.assertEqual(m.s.calls, [])

    def test_put_only_kinds_default_to_put(self):
        routes = {**changeset_routes(),
                  ("PUT", r"/api/model/dataModels/M/securityFilters/O"): Resp(200, {}),
                  ("GET", r"/api/model/dataModels/M/securityFilters/O"): Resp(200, {})}
        m = make_m(routes)
        with quiet():
            bm.cmd_patch_model_object(m, pmo_args(kind="security_filter"))
        self.assertEqual(len(calls(m, "PUT")), 1)

    def test_classic_schema_objects_get_a_schema_edit_changeset(self):
        routes = {**changeset_routes(),
                  ("PATCH", r"/api/model/attributes/O"): Resp(200, {}),
                  ("GET", r"/api/model/attributes/O"): Resp(200, {})}
        m = make_m(routes)
        with quiet():
            bm.cmd_patch_model_object(m, pmo_args(kind="project_attribute", model_id=None))
        (opened,) = calls(m, "POST", r"/api/model/changesets")
        self.assertIn("schemaEdit=true", opened.path)

    def test_unverified_commands_need_explicit_consent(self):
        m = make_m({}, login=False)
        for fn, extra in ((bm.cmd_create_transformation, {"member": ["A=-1"]}),
                          (bm.cmd_attach_transformation, {"source_metric": "S", "transformation": "T"})):
            with self.subTest(fn.__name__), self.assertRaises(SystemExit), quiet():
                fn(m, types.SimpleNamespace(model_id="M", name="n", unverified_ok=False, **extra))
        self.assertEqual(m.s.calls, [])


# ── #15 api-call secrets / #16 base URL and flags / #18 misc ─────────────────

class ApiCallTests(unittest.TestCase):
    def test_tokens_in_the_body_are_redacted_and_the_out_file_is_private(self):
        m = make_m({("POST", r"/api/v2/auth/identityToken"): Resp(201, {"identityToken": "SECRET-VALUE", "ttl": 60})},
                   login=False)
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "resp.json")
            args = types.SimpleNamespace(no_auth=True, with_identity_token=False, method="POST",
                                         path="/api/v2/auth/identityToken", param=[], header=[], json=None,
                                         json_file=None, file=[], form=[], out=out_path, text_limit=8000,
                                         yes=False, show_secrets=False)
            with quiet() as out:
                bm.cmd_api_call(m, args)
            printed = json.loads(out.getvalue())
            self.assertEqual(printed["body"], {"identityToken": "<redacted>", "ttl": 60})
            with open(out_path, encoding="utf-8") as f:
                self.assertIn("SECRET-VALUE", f.read())
            self.assertEqual(stat.S_IMODE(os.stat(out_path).st_mode), 0o600)


class BaseUrlAndFlagTests(unittest.TestCase):
    def args(self, base):
        return types.SimpleNamespace(base=base, project_id="P", user="", password="", login_mode=1, verbose=False)

    def test_borrowed_tokens_are_never_sent_over_plain_http(self):
        with self.assertRaises(SystemExit), quiet():
            bm.MSTR(self.args("http://tenant.example.test/MicroStrategyLibrary"))
        bm.MSTR(self.args("http://127.0.0.1:8080/MicroStrategyLibrary"))
        with mock.patch.dict(os.environ, {"MSTR_ALLOW_HTTP": "1"}):
            bm.MSTR(self.args("http://lab.example.test/MicroStrategyLibrary"))

    def test_abbreviated_secret_flags_are_rejected(self):
        with self.assertRaises(SystemExit), quiet():
            bm.build_parser().parse_args(["--pass", "x", "auth-probe"])

    def test_commit_read_timeout_is_not_discarded_mid_commit(self):
        def slow(call):
            raise requests.ReadTimeout("read timed out")
        m = make_m({("POST", r"/api/model/changesets/CS9/commit"): slow})
        m.open_changesets.add("CS9")
        with self.assertRaises(SystemExit), quiet():
            bm.commit_cs(m, "CS9")
        self.assertEqual(calls(m, "DELETE"), [])
        self.assertEqual(m.open_changesets, set())

    def test_delete_model_refuses_what_is_not_a_mosaic_model(self):
        m = make_m({("GET", r"/api/objects/X"): Resp(200, {"subtype": 776, "name": "Cube"})}, login=False)
        with self.assertRaises(SystemExit), quiet():
            bm.cmd_delete_model(m, types.SimpleNamespace(yes=True, model_id="X", any_type=False))
        self.assertEqual(calls(m, "DELETE"), [])

    def test_wire_relationships_exits_non_zero_when_a_put_fails(self):
        form = {"forms": [{"expressions": [{"tables": [{"objectId": "T"}]}]}]}
        routes = {**changeset_routes(),
                  ("GET", r"/api/model/dataModels/M/attributes"): Resp(200, {"attributes": [
                      {"information": {"objectId": "P", "name": "Parent"}},
                      {"information": {"objectId": "C", "name": "Child"}}]}),
                  ("GET", r"/api/model/dataModels/M/tables"): Resp(200, {"tables": [
                      {"information": {"objectId": "T", "name": "facts"}}]}),
                  ("GET", r"/api/model/dataModels/M/attributes/\w+"): Resp(200, form),
                  ("PUT", r"/api/model/dataModels/M/attributes/C/relationships"): Resp(400, {"code": "8004ccc7"})}
        m = make_m(routes)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"relationships": [{"parent_attribute": "Parent", "child_attribute": "Child",
                                          "relationship_table": "facts"}]}, f)
        try:
            with self.assertRaises(SystemExit) as cm, quiet():
                bm.cmd_wire_relationships(m, types.SimpleNamespace(model_id="M", hints=f.name, dry_run=False,
                                                                   replace=False))
        finally:
            os.unlink(f.name)
        self.assertEqual(cm.exception.code, 1)


class RelationshipTableInferenceTests(unittest.TestCase):
    """A hint without relationship_table on a model with several tables: the one table both attributes read."""

    def test_names_of_the_connector_resolve_to_attributes(self):
        self.assertEqual(bm.attribute_name_candidates("Zero Balance__code"), ["zero balance__code", "zero balance"])
        self.assertEqual(bm.attribute_name_candidates("delinquency bucket (id)"),
                         ["delinquency bucket (id)", "delinquency bucket"])
        self.assertEqual(bm.attribute_name_candidates("region"), ["region"])

    def test_infer_table(self):
        tables = {"facts": "T", "map": "T2", "other": "T3"}
        self.assertEqual(bm.infer_relationship_table({}, {"T2"}, {"T", "T2"}, tables), ("T2", ""))
        self.assertIsNone(bm.infer_relationship_table({}, {"T3"}, {"T"}, tables)[0])
        both = bm.infer_relationship_table({"attributeLookupTable": {"objectId": "T2"}}, {"T", "T2"}, {"T", "T2"}, tables)
        self.assertEqual(both, ("T2", ""))
        none, why = bm.infer_relationship_table({}, {"T", "T2"}, {"T", "T2"}, tables)
        self.assertIsNone(none)
        self.assertIn("['facts', 'map']", why)

    def test_wire_infers_the_shared_table_on_a_multi_table_model(self):
        def attribute(call):
            reads = {"P": ["T2"], "C": ["T", "T2"]}[call.path.split("?")[0].rsplit("/", 1)[1]]
            return Resp(200, {"forms": [{"expressions": [{"tables": [{"objectId": t}]} for t in reads]}]})
        routes = {**changeset_routes(),
                  ("GET", r"/api/model/dataModels/M/attributes"): Resp(200, {"attributes": [
                      {"information": {"objectId": "P", "name": "Plus Flag"}},
                      {"information": {"objectId": "C", "name": "Status"}}]}),
                  ("GET", r"/api/model/dataModels/M/tables"): Resp(200, {"tables": [
                      {"information": {"objectId": "T", "name": "facts.csv"}},
                      {"information": {"objectId": "T2", "name": "status_map.csv"}}]}),
                  ("GET", r"/api/model/dataModels/M/attributes/\w+"): attribute}
        m = make_m(routes)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"relationships": [{"parent_attribute": "plus flag", "child_attribute": "Status (id)"}]}, f)
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                bm.cmd_wire_relationships(m, types.SimpleNamespace(model_id="M", hints=f.name, dry_run=True,
                                                                   replace=False))
        finally:
            os.unlink(f.name)
        self.assertIn("1 to write, 0 skipped", err.getvalue())
        self.assertIn("via status_map.csv (inferred)", err.getvalue())


# ── end to end: the CLI against the fake tenant ──────────────────────────────

class CliCleanupTests(unittest.TestCase):
    def test_a_failed_write_discards_its_changeset_once_and_logs_out(self):
        with FakeTenant() as t:
            @t.route("POST", r"/api/model/dataModels/\w+/securityFilters")
            def reject(call, match):
                return 400, {"code": "ERR007", "message": "bad qualification"}, {}

            env = dict(os.environ, MSTR_BASE=t.base, MSTR_USER="alice", MSTR_PASSWORD="correct horse",
                       MSTR_PROJECT_ID=PROJECT_ID, MSTR_AUTH_METHOD="password")
            r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "build_mosaic.py"), "add-security-filter",
                                "--model-id", "M" * 32, "--spec", f"Region={A_ID}=East"],
                               env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertEqual(t.count("DELETE", r"/api/model/changesets/\w+"), 1)
            self.assertEqual([c["state"] for c in t.state.changesets.values()], ["discarded"])
            self.assertEqual(t.count("POST", r"/api/auth/logout"), 1)
            self.assertEqual(t.open_sessions, 0)
            self.assertNotIn("correct horse", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
