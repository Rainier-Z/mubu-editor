"""Regression coverage for reliable Mubu core writes (network calls use local fakes)."""

import copy
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from mubu import client as client_module
from mubu.client import MubuClient
from mubu.commands import _save
from mubu.config import MubuError


class CoreWriteReliabilityTests(unittest.TestCase):
    def setUp(self):
        self._patchers = [
            mock.patch.object(MubuClient, "_load_env_file", lambda self, path=None: None),
            mock.patch.object(MubuClient, "_load_token", lambda self: False),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_client(self, nodes, *, apply_events=True, omit_zero_task_status=False,
                    add_insert_child_defaults=False):
        client = MubuClient(phone="test", password="test")
        client.member_id = "M1"
        store = {
            "nodes": copy.deepcopy(nodes), "version": 4, "events": [],
            "attempts": [], "write_occurred": False,
        }

        def strip_omitted_default_task_status(node):
            if node.get("taskStatus") == 0:
                node.pop("taskStatus")
            for child in node.get("children") or []:
                strip_omitted_default_task_status(child)

        def get_doc_raw(doc_id):
            readback_nodes = copy.deepcopy(store["nodes"])
            if omit_zero_task_status and store["write_occurred"]:
                for node in readback_nodes:
                    strip_omitted_default_task_status(node)
            return {
                "baseVersion": store["version"],
                "definition": json.dumps({"nodes": readback_nodes}),
            }

        def apply_event(event):
            name = event["name"]
            entries = event.get({
                "update": "updated", "create": "created", "delete": "deleted",
                "structureChanged": "changed",
            }[name], [])
            if name == "update":
                for entry in entries:
                    path = entry["path"]
                    target = store["nodes"]
                    for index in range(1, len(path) - 1, 2):
                        target = target[path[index]]["children"]
                    updated = copy.deepcopy(target[path[-1]])
                    updated.update(copy.deepcopy(entry["updated"]))
                    target[path[-1]] = updated
            elif name == "create":
                for entry in entries:
                    path = entry["path"]
                    target = store["nodes"]
                    for index in range(1, len(path) - 2, 2):
                        target = target[path[index]]["children"]
                    target.insert(entry["index"], copy.deepcopy(entry["node"]))
                    if add_insert_child_defaults and entry.get("parentId") is not None:
                        target[entry["index"]].update({
                            "note": "", "collapsed": False, "finish": False,
                            "color": "", "deadline": 0, "remindAt": 0,
                        })
            elif name == "delete":
                for entry in entries:
                    path = entry["path"]
                    target = store["nodes"]
                    for index in range(1, len(path) - 1, 2):
                        target = target[path[index]]["children"]
                    target.pop(path[-1])
            elif name == "structureChanged":
                def find_node(nodes, node_id):
                    for node in nodes:
                        if node.get("id") == node_id:
                            return node
                        found = find_node(node.get("children") or [], node_id)
                        if found is not None:
                            return found
                    return None

                for entry in entries:
                    original = entry["original"]
                    changed = entry["changed"]
                    source = store["nodes"]
                    for index in range(1, len(original["path"]) - 1, 2):
                        source = source[original["path"][index]]["children"]
                    node = source.pop(original["index"])
                    if changed["parentId"] is None:
                        destination = store["nodes"]
                    else:
                        parent = find_node(store["nodes"], changed["parentId"])
                        destination = parent.setdefault("children", [])
                    destination.insert(changed["index"], node)

        def request(*args, **kwargs):
            payload = kwargs.get("json", {})
            events = payload.get("events", [])
            if events:
                store["attempts"].append(copy.deepcopy(events))
                for event in events:
                    name = event.get("name")
                    entries = event.get({
                        "update": "updated", "create": "created",
                        "structureChanged": "changed", "delete": "deleted",
                    }.get(name, ""), [])
                    for entry in entries:
                        if name == "create":
                            nodes = [entry.get("node")]
                        elif name == "update":
                            nodes = [entry.get("updated"), entry.get("original")]
                        elif name == "structureChanged":
                            nodes = [
                                (entry.get("original") or {}).get("node"),
                                (entry.get("changed") or {}).get("node"),
                            ]
                        else:
                            nodes = []

                        def reject_internal_checked(node):
                            if not isinstance(node, dict):
                                return
                            if "checked" in node:
                                raise MubuError("fake server rejects internal checked")
                            for child in node.get("children") or []:
                                reject_internal_checked(child)
                        for node in nodes:
                            reject_internal_checked(node)
                        if name == "update" and "children" in (entry.get("updated") or {}):
                            raise MubuError("fake server rejects update.children")
                        if name in {"create", "update", "delete"}:
                            path = entry.get("path")
                            if not isinstance(path, list) or len(path) < 2 or path[0] != "nodes":
                                raise MubuError("fake server rejects invalid node path")
                if payload.get("version") != store["version"]:
                    raise MubuError(
                        f"stale version: expected {store['version']}, got {payload.get('version')}"
                    )
            store["events"].append(copy.deepcopy(events))
            if apply_events:
                for event in events:
                    apply_event(event)
                store["version"] += 1
                store["write_occurred"] = bool(events)
            return {}

        client._get_doc_raw = get_doc_raw
        client._request = request
        client._test_store = store
        return client

    def test_set_node_fields_rejects_identity_and_structure_fields(self):
        client = self.make_client([{"id": "A", "text": "before", "children": []}])
        for field, value in (("id", "B"), ("children", []),
                             ("parentId", "P"), ("path", ["nodes", 0]),
                             ("index", 1)):
            with self.subTest(field=field):
                with self.assertRaises(MubuError):
                    client.set_node_fields("D1", ["nodes", 0], {field: value})
        self.assertEqual(client._test_store["events"], [])

    def test_create_boundary_maps_checked_without_leaking_internal_field(self):
        client = self.make_client([])

        client.append_top_nodes("D1", [{
            "id": "A", "text": "done", "checked": True, "children": [{
                "id": "A1", "text": "open", "checked": False, "children": [],
            }],
        }])

        created = client._test_store["events"][0][0]["created"]
        payload_nodes = [created[0]["node"], created[0]["node"]["children"][0]]
        self.assertEqual([node["taskStatus"] for node in payload_nodes], [2, 1])
        for node in payload_nodes:
            self.assertNotIn("checked", node)
            self.assertIs(node["finish"], False)
            self.assertEqual(node["deadline"], 0)
            self.assertEqual(node["remindAt"], 0)

    def test_sync_markdown_status_cycle_maps_checked_and_clears_status(self):
        client = self.make_client([{
            "id": "A", "text": "Task", "taskStatus": 0,
            "finish": False, "deadline": 0, "remindAt": 0, "children": [],
        }])

        for wanted, expected_status in (
            ({"checked": False}, 1),
            ({"checked": True}, 2),
            ({"taskStatus": 1}, 1),
            ({"taskStatus": 0}, 0),
        ):
            client.sync_doc_definition("D1", {"nodes": [
                {"id": "md_1", "text": "Task", "children": [], **wanted},
            ]})
            event = client._test_store["events"][-1][0]
            entry = event["updated"][0]
            self.assertNotIn("checked", json.dumps(event))
            self.assertEqual(entry["updated"]["taskStatus"], expected_status)
            self.assertIs(entry["updated"]["finish"], False)
            self.assertEqual(entry["updated"]["deadline"], 0)
            self.assertEqual(entry["updated"]["remindAt"], 0)

    def test_sync_nested_mixed_add_delete_uses_path_events_and_partial_updates(self):
        client = self.make_client([
            {"id": "A", "text": "A", "children": [
                {"id": "A1", "text": "remove", "children": []},
                {"id": "A2", "text": "keep", "future": "yes", "children": []},
            ]},
            {"id": "B", "text": "B", "children": [
                {"id": "B1", "text": "remove", "children": []},
            ]},
        ])

        client.sync_doc_definition("D1", {"nodes": [
            {"id": "md_a", "text": "A", "children": [
                {"id": "md_a2", "text": "keep", "children": []},
                {"id": "md_a3", "text": "new", "children": []},
            ]},
            {"id": "md_b", "text": "B", "children": [
                {"id": "md_b2", "text": "new", "children": []},
            ]},
        ]})

        self.assertEqual([node["text"] for node in client._test_store["nodes"][0]["children"]],
                         ["keep", "new"])
        self.assertEqual([node["text"] for node in client._test_store["nodes"][1]["children"]],
                         ["new"])
        events = [event for batch in client._test_store["events"] for event in batch]
        self.assertTrue(any(event["name"] == "create" and
                            event["created"][0]["path"] == ["nodes", 0, "children", 1]
                            and event["created"][0]["parentId"] == "A"
                            for event in events))
        self.assertTrue(any(event["name"] == "delete" and
                            event["deleted"][0]["path"] == ["nodes", 0, "children", 1]
                            and event["deleted"][0]["parentId"] == "A"
                            for event in events))
        for event in events:
            if event["name"] == "update":
                self.assertTrue(all("children" not in entry["updated"]
                                    for entry in event["updated"]))

    def test_build_update_event_rejects_legacy_root_children_touch(self):
        client = self.make_client([])

        with self.assertRaises(MubuError):
            client.build_update_event({"nodes": [{"id": "n1", "text": "A"}]}, "D1")

    def test_save_doc_rejects_raw_internal_checked_before_network_even_without_verify(self):
        client = self.make_client([{"id": "A", "text": "before", "children": []}])
        event = {"name": "update", "updated": [{
            "updated": {"id": "A", "checked": True},
            "original": {"id": "A"}, "path": ["nodes", 0],
        }]}

        with self.assertRaises(MubuError):
            client._save_doc_unverified("D1", events=[event], version=4)
        self.assertEqual(client._test_store["attempts"], [])

    def test_new_node_rejects_boolean_task_status_before_network(self):
        client = self.make_client([])

        with self.assertRaises(MubuError):
            client.append_top_nodes("D1", [{
                "id": "A", "text": "bad", "taskStatus": True, "children": [],
            }])
        self.assertEqual(client._test_store["attempts"], [])

    def test_raw_task_status_zero_rejects_conflicting_finish_before_network(self):
        client = self.make_client([{"id": "A", "text": "before", "children": []}])
        event = {"name": "update", "updated": [{
            "updated": {"id": "A", "taskStatus": 0, "finish": True,
                         "deadline": 0, "remindAt": 0},
            "original": {"id": "A"}, "path": ["nodes", 0],
        }]}

        with self.assertRaises(MubuError):
            client._save_doc_unverified("D1", events=[event], version=4)
        self.assertEqual(client._test_store["attempts"], [])

    def test_existing_update_rejects_non_exact_task_status_even_when_equal(self):
        for existing_status in (True, None, "1", 3, -1):
            with self.subTest(existing_status=existing_status):
                client = self.make_client([{
                    "id": "A", "text": "before", "taskStatus": existing_status,
                    "children": [],
                }])
                with self.assertRaises(MubuError):
                    client.set_node_fields("D1", ["nodes", 0], {"taskStatus": 1})
                self.assertEqual(client._test_store["attempts"], [])

    def test_build_node_update_event_emits_only_collapsed_patch(self):
        client = self.make_client([])
        original = {
            "id": "A", "text": "Task", "collapsed": False, "note": "memo",
            "priority": 2, "highlight": "yellow", "color": "red",
            "futureField": {"nested": True}, "children": [],
        }
        updated = copy.deepcopy(original)
        updated["collapsed"] = True

        event = client.build_node_update_event(["nodes", 0], original, updated)

        self.assertEqual(event["updated"][0]["updated"], {"id": "A", "collapsed": True})
        self.assertEqual(event["updated"][0]["original"], original)

    def test_build_node_update_event_adds_text_companions_even_when_blank(self):
        client = self.make_client([])
        original = {
            "id": "A", "text": "before", "modified": 1,
            "highlight": "", "color": "", "note": "memo", "priority": 2,
            "futureField": {"nested": True}, "children": [],
        }
        updated = copy.deepcopy(original)
        updated.update({"text": "after", "modified": 1234})

        event = client.build_node_update_event(["nodes", 0], original, updated)

        self.assertEqual(event["updated"][0]["updated"], {
            "id": "A", "text": "after", "modified": 1234,
            "highlight": "", "color": "",
        })
        self.assertEqual(event["updated"][0]["original"], original)

    def test_task_status_transition_zero_to_one_uses_captured_payload(self):
        client = self.make_client([{
            "id": "A", "text": "Task", "taskStatus": 0, "finish": False,
            "deadline": 0, "remindAt": 0, "children": [],
        }])

        self.assertTrue(client.set_node_fields("D1", ["nodes", 0], {"taskStatus": 1}))

        entry = client._test_store["events"][0][0]["updated"][0]
        self.assertEqual(entry["updated"], {
            "id": "A", "taskStatus": 1, "finish": False,
            "deadline": 0, "remindAt": 0,
        })
        self.assertEqual(entry["original"], {
            "id": "A", "taskStatus": 0, "deadline": 0, "remindAt": 0,
        })

    def test_task_status_transition_one_to_two_uses_captured_payload(self):
        client = self.make_client([{
            "id": "A", "text": "Task", "taskStatus": 1, "finish": False,
            "deadline": 0, "remindAt": 0, "children": [],
        }])

        self.assertTrue(client.set_node_fields("D1", ["nodes", 0], {"taskStatus": 2}))

        entry = client._test_store["events"][0][0]["updated"][0]
        self.assertEqual(entry["updated"], {
            "id": "A", "taskStatus": 2, "finish": False,
            "deadline": 0, "remindAt": 0,
        })
        self.assertEqual(entry["original"], {
            "id": "A", "taskStatus": 1, "finish": False,
            "deadline": 0, "remindAt": 0,
        })

    def test_collapsed_and_emoji_writes_do_not_add_modified(self):
        """Emoji minimal payload reflects confirmed success; exact emoji payload was not captured."""
        for field, value, expected in (
            ("collapsed", True, {"id": "A", "collapsed": True}),
            ("emoji", "😄", {"id": "A", "emoji": "😄"}),
        ):
            with self.subTest(field=field):
                client = self.make_client([{
                    "id": "A", "text": "Task", "collapsed": False,
                    "emoji": "", "modified": 1, "children": [],
                }])
                client.set_node_fields("D1", ["nodes", 0], {field: value})
                event = client._test_store["events"][0][0]
                self.assertEqual(event["updated"][0]["updated"], expected)

    def test_task_status_supports_checked_task_lifecycle_transitions(self):
        client = self.make_client([{
            "id": "A", "text": "Task", "taskStatus": 2, "finish": False,
            "deadline": 0, "remindAt": 0, "children": [],
        }])

        for expected_status in (1, 0):
            self.assertTrue(client.set_node_fields(
                "D1", ["nodes", 0], {"taskStatus": expected_status}))
            entry = client._test_store["events"][-1][0]["updated"][0]
            self.assertEqual(entry["updated"]["taskStatus"], expected_status)
            self.assertIs(entry["updated"]["finish"], False)
            self.assertEqual(entry["updated"]["deadline"], 0)
            self.assertEqual(entry["updated"]["remindAt"], 0)

    def test_apply_partial_node_update_preserves_unmentioned_metadata(self):
        original = {
            "id": "A", "text": "before", "collapsed": False,
            "highlight": "yellow", "color": "#123456",
            "futureField": {"nested": [1, 2]}, "children": [],
        }
        event = {"name": "update", "updated": [{
            "updated": {"id": "A", "collapsed": True},
            "original": copy.deepcopy(original), "path": ["nodes", 0],
        }]}

        expected = MubuClient._apply_node_events([original], [event])

        self.assertEqual(expected[0]["collapsed"], True)
        self.assertEqual(expected[0]["highlight"], "yellow")
        self.assertEqual(expected[0]["color"], "#123456")
        self.assertEqual(expected[0]["futureField"], {"nested": [1, 2]})

    def test_sync_text_change_sends_partial_payload_and_preserves_unknown_fields(self):
        initial = [{
            "id": "A", "text": "before", "modified": 1,
            "highlight": "", "color": "", "note": "memo", "priority": 2,
            "futureField": {"nested": [1, {"keep": True}]}, "children": [],
        }]
        client = self.make_client(initial)

        client.sync_doc_definition("D1", {"nodes": [
            {"id": "node_1", "text": "after", "note": "memo", "children": []},
        ]})

        update_event = next(
            event for batch in client._test_store["events"] for event in batch
            if event["name"] == "update"
        )
        self.assertEqual(update_event["updated"][0]["updated"].keys(), {
            "id", "text", "modified", "highlight", "color",
        })
        saved = client._test_store["nodes"][0]
        self.assertEqual(saved["text"], "after")
        self.assertEqual(saved["highlight"], "")
        self.assertEqual(saved["color"], "")
        self.assertEqual(saved["note"], "memo")
        self.assertEqual(saved["priority"], 2)
        self.assertEqual(saved["futureField"], {"nested": [1, {"keep": True}]})

    def test_save_doc_rejects_missing_or_empty_events(self):
        client = self.make_client([])

        for events in (None, [], [None]):
            with self.subTest(events=events):
                with self.assertRaises(MubuError):
                    client.save_doc("D1", events=events, version=4)
        self.assertEqual(client._test_store["events"], [])

    def test_raw_nodes_treats_empty_definition_object_as_no_nodes(self):
        self.assertEqual(MubuClient._raw_nodes({"definition": "{}", "baseVersion": 4}, "D1"), [])
        self.assertEqual(MubuClient._raw_nodes({"definition": '{"nodes":null}'}, "D1"), [])

    def test_raw_nodes_distinguishes_non_array_nodes(self):
        with self.assertRaises(MubuError) as error:
            MubuClient._raw_nodes({"definition": '{"nodes":{}}'}, "D1")
        self.assertIn("nodes", str(error.exception))
        self.assertIn("非数组", str(error.exception))

    def test_semantics_accepts_only_omitted_default_fields(self):
        expected = [{
            "id": "A", "text": "Text", "note": "", "collapsed": False,
            "finish": False, "color": "red", "children": [], "priority": 0,
            "highlight": "",
        }]
        actual = [{
            "id": "A", "text": "Text", "note": "", "collapsed": False,
            "finish": False, "color": "red",
        }]

        self.assertTrue(MubuClient._nodes_semantically_equal(actual, expected))

    def test_semantics_remains_strict_for_nondefault_and_required_fields(self):
        expected_node = {
            "id": "A", "text": "Text", "note": "Note", "collapsed": True,
            "finish": True, "color": "red", "children": [], "priority": 3,
            "highlight": "yellow",
        }
        cases = [
            ("children", [{"id": "C", "text": "child"}]),
            ("priority", 4),
            ("highlight", "green"),
            ("id", "B"),
            ("text", "Other"),
            ("note", "Other note"),
            ("collapsed", False),
            ("finish", False),
            ("color", "blue"),
        ]
        required = ("id", "text", "note", "collapsed", "finish", "color")
        for key, changed_value in cases:
            with self.subTest(field=key, variation="different"):
                actual = copy.deepcopy(expected_node)
                actual[key] = changed_value
                self.assertFalse(MubuClient._nodes_semantically_equal([actual], [expected_node]))
            if key in required:
                with self.subTest(field=key, variation="missing"):
                    actual = copy.deepcopy(expected_node)
                    del actual[key]
                    self.assertFalse(MubuClient._nodes_semantically_equal([actual], [expected_node]))

        unexpected = copy.deepcopy(expected_node)
        unexpected.pop("priority")
        unexpected.pop("highlight")
        unexpected["serverFuture"] = {"nonDefault": True}
        normalized_expected = copy.deepcopy(expected_node)
        normalized_expected.pop("priority")
        normalized_expected.pop("highlight")
        self.assertFalse(MubuClient._nodes_semantically_equal([unexpected], [normalized_expected]))

    def test_semantics_requires_nondefault_task_status_and_rejects_unexpected_fields(self):
        expected = [{"id": "A", "text": "Text", "taskStatus": 1, "children": []}]
        missing_status = [{"id": "A", "text": "Text", "children": []}]
        default_status = [{"id": "A", "text": "Text", "taskStatus": 0, "children": []}]
        unexpected_nondefault = [{
            "id": "A", "text": "Text", "taskStatus": 1,
            "serverFuture": {"nonDefault": True}, "children": [],
        }]

        self.assertFalse(MubuClient._nodes_semantically_equal(missing_status, expected))
        self.assertFalse(MubuClient._nodes_semantically_equal(default_status, expected))
        self.assertFalse(MubuClient._nodes_semantically_equal(
            unexpected_nondefault, expected))

    def test_semantics_normalizes_only_proven_default_metadata(self):
        expected = [{"id": "A", "text": "Task", "children": []}]
        defaults = {
            "taskStatus": 0, "note": "", "collapsed": False, "finish": False,
            "color": "", "deadline": 0, "remindAt": 0,
        }
        actual_with_defaults = copy.deepcopy(expected)
        actual_with_defaults[0].update(defaults)

        self.assertTrue(MubuClient._nodes_semantically_equal(
            actual_with_defaults, expected))
        self.assertTrue(MubuClient._nodes_semantically_equal(
            expected, actual_with_defaults))

        nondefaults = {
            "note": "Reminder", "collapsed": True, "finish": True,
            "color": "red", "deadline": 123, "remindAt": 456,
        }
        for field, value in nondefaults.items():
            with self.subTest(field=field, comparison="actual_nondefault"):
                actual = copy.deepcopy(expected)
                actual[0][field] = value
                self.assertFalse(MubuClient._nodes_semantically_equal(actual, expected))
            with self.subTest(field=field, comparison="expected_nondefault"):
                wanted = copy.deepcopy(expected)
                wanted[0][field] = value
                self.assertFalse(MubuClient._nodes_semantically_equal(expected, wanted))

        unexpected = copy.deepcopy(expected)
        unexpected[0]["serverFuture"] = {"nonDefault": True}
        self.assertFalse(MubuClient._nodes_semantically_equal(unexpected, expected))

    def test_insert_child_verifies_server_added_default_metadata(self):
        client = self.make_client([{
            "id": "A", "text": "parent", "taskStatus": 0, "children": [],
        }], omit_zero_task_status=True, add_insert_child_defaults=True)

        created_ids = client.insert_child_nodes("D1", ["nodes", 0], ["child"])

        child = client._test_store["nodes"][0]["children"][0]
        self.assertEqual(len(created_ids), 1)
        self.assertEqual(child["text"], "child")
        self.assertEqual(child["note"], "")
        self.assertIs(child["collapsed"], False)
        self.assertIs(child["finish"], False)
        self.assertEqual(child["color"], "")
        self.assertEqual(child["deadline"], 0)
        self.assertEqual(child["remindAt"], 0)

    def test_append_three_nodes_with_subtree_verifies_when_server_omits_zero_task_status(self):
        client = self.make_client([], omit_zero_task_status=True)
        nodes = [
            {"id": "A", "text": "first", "children": [
                {"id": "A1", "text": "child", "children": []},
            ]},
            {"id": "B", "text": "second", "children": []},
            {"id": "C", "text": "third", "children": []},
        ]

        created_ids = client.append_top_nodes("D1", nodes)

        self.assertEqual(created_ids, ["A", "B", "C"])
        self.assertEqual([node["text"] for node in client._test_store["nodes"]],
                         ["first", "second", "third"])
        self.assertEqual(client._test_store["nodes"][0]["children"][0]["text"], "child")

    def test_update_text_verifies_when_server_omits_zero_task_status(self):
        client = self.make_client([{
            "id": "A", "text": "before", "taskStatus": 0,
            "highlight": "", "color": "", "children": [],
        }], omit_zero_task_status=True)

        self.assertTrue(client.update_node_text("D1", ["nodes", 0], "after"))

        self.assertEqual(client._test_store["nodes"][0]["text"], "after")
        self.assertEqual(client._test_store["nodes"][0]["taskStatus"], 0)

    def test_insert_child_verifies_when_server_omits_zero_task_status(self):
        client = self.make_client([{
            "id": "A", "text": "parent", "taskStatus": 0, "children": [],
        }], omit_zero_task_status=True)

        created_ids = client.insert_child_nodes("D1", ["nodes", 0], ["child"])

        self.assertEqual(len(created_ids), 1)
        self.assertEqual(client._test_store["nodes"][0]["children"][0]["text"], "child")
        self.assertEqual(client._test_store["nodes"][0]["children"][0]["taskStatus"], 0)

    def test_delete_top_verifies_when_server_omits_zero_task_status(self):
        client = self.make_client([
            {"id": "A", "text": "remove", "taskStatus": 0, "children": []},
            {"id": "B", "text": "keep", "taskStatus": 0, "children": []},
        ], omit_zero_task_status=True)

        self.assertEqual(client.delete_top_nodes("D1", [0]), 1)

        self.assertEqual([node["id"] for node in client._test_store["nodes"]], ["B"])

    def test_append_top_nodes_writes_and_reads_back_from_empty_definition(self):
        client = self.make_client([])
        get_raw = client._get_doc_raw

        def empty_definition_while_empty(doc_id):
            raw = get_raw(doc_id)
            if not client._test_store["nodes"]:
                raw["definition"] = "{}"
            return raw

        client._get_doc_raw = empty_definition_while_empty

        created = client.append_top_nodes("D1", [{"text": "first"}])

        self.assertEqual(len(created), 1)
        self.assertEqual(client._test_store["nodes"][0]["text"], "first")

    def test_create_doc_appends_valid_content_to_empty_definition_and_verifies_it(self):
        client = self.make_client([])
        get_raw = client._get_doc_raw
        save_request = client._request

        def request(*args, **kwargs):
            if "events" in kwargs.get("json", {}):
                return save_request(*args, **kwargs)
            return {"id": "D-created"}

        def empty_definition_while_empty(doc_id):
            raw = get_raw(doc_id)
            if not client._test_store["nodes"]:
                raw["definition"] = "{}"
            return raw

        client._request = request
        client._get_doc_raw = empty_definition_while_empty

        doc_id = client.create_doc("Doc", "F1", {"nodes": [{"text": "first", "children": []}]})

        self.assertEqual(doc_id, "D-created")
        self.assertEqual([node["text"] for node in client._test_store["nodes"]], ["first"])

    def test_save_cli_uses_verified_full_tree_sync_and_only_reports_after_success(self):
        client = mock.Mock()
        args = SimpleNamespace(doc_id="D1", md=None, file=None, content='{"nodes":[]}')
        client.sync_doc_definition.side_effect = MubuError("readback mismatch")
        output = io.StringIO()

        with redirect_stdout(output):
            with self.assertRaises(MubuError):
                _save(client, args)

        client.sync_doc_definition.assert_called_once_with("D1", {"nodes": []})
        self.assertNotIn("保存成功", output.getvalue())

    def test_sync_doc_definition_preserves_unknown_fields_with_same_parent_add_delete(self):
        initial = [
            {"id": "A", "text": "Alpha", "color": 3,
             "children": [
                 {"id": "A1", "text": "Child", "private": "keep", "children": []},
                 {"id": "A2", "text": "Drop child", "children": []},
             ]},
            {"id": "B", "text": "Beta", "priority": 2, "children": []},
            {"id": "C", "text": "Drop me", "children": []},
        ]
        client = self.make_client(initial)
        desired = {"nodes": [
            {"id": "md_1", "text": "Alpha updated", "children": [
                {"id": "md_2", "text": "Child", "children": []},
                {"id": "md_3", "text": "New child", "children": []},
            ]},
            {"id": "md_4", "text": "Beta", "children": []},
        ]}

        client.sync_doc_definition("D1", desired)

        nodes = client._test_store["nodes"]
        self.assertEqual([node["text"] for node in nodes], ["Alpha updated", "Beta"])
        self.assertEqual(nodes[0]["id"], "A")
        self.assertEqual(nodes[0]["color"], 3)
        self.assertEqual(nodes[1]["id"], "B")
        self.assertEqual(nodes[1]["priority"], 2)
        self.assertEqual(nodes[0]["children"][0]["id"], "A1")
        self.assertEqual(nodes[0]["children"][0]["private"], "keep")
        self.assertEqual([node["text"] for node in nodes[0]["children"]], ["Child", "New child"])
        all_ids = []
        def collect(node_list):
            for node in node_list:
                all_ids.append(node["id"])
                collect(node.get("children") or [])
        collect(nodes)
        self.assertNotIn("C", all_ids)

    def test_sync_moves_existing_sibling_from_after_target_to_target_order(self):
        client = self.make_client([
            {"id": "A", "text": "A", "children": []},
            {"id": "B", "text": "B", "children": []},
            {"id": "C", "text": "C", "children": []},
        ])

        client.sync_doc_definition("D1", {"nodes": [
            {"id": "md_1", "text": "B", "children": []},
            {"id": "md_2", "text": "A", "children": []},
            {"id": "md_3", "text": "C", "children": []},
        ]})

        self.assertEqual([node["id"] for node in client._test_store["nodes"]], ["B", "A", "C"])
        events = [event for batch in client._test_store["events"] for event in batch]
        self.assertEqual([event["name"] for event in events], ["structureChanged"])
        entry = events[0]["changed"][0]
        self.assertEqual(entry["original"]["index"], 1)
        self.assertEqual(entry["changed"]["index"], 0)

    def test_sync_moves_existing_nested_node_across_parents(self):
        client = self.make_client([
            {"id": "A", "text": "A", "children":[{"id": "C", "text": "Child", "children": []}]},
            {"id": "B", "text": "B", "children": []},
        ])

        client.sync_doc_definition("D1", {"nodes": [
            {"id": "md_1", "text": "A", "children": []},
            {"id": "md_2", "text": "B", "children": [
                {"id": "md_3", "text": "Child", "children": []},
            ]},
        ]})

        self.assertEqual(client._test_store["nodes"][0]["children"], [])
        self.assertEqual(client._test_store["nodes"][1]["children"][0]["id"], "C")
        events = [event for batch in client._test_store["events"] for event in batch]
        self.assertEqual([event["name"] for event in events], ["structureChanged"])
        entry = events[0]["changed"][0]
        self.assertEqual(entry["original"]["parentId"], "A")
        self.assertEqual(entry["changed"]["parentId"], "B")
        self.assertEqual(entry["original"]["node"], entry["changed"]["node"])

    def test_build_structure_event_copies_full_node_and_checks_direct_parent(self):
        nodes = [
            {"id": "A", "text": "A", "futureField": {"keep": True}, "children": []},
            {"id": "B", "text": "B", "children": []},
        ]
        client = self.make_client(nodes)
        original = {"parentId": None, "index": 0, "node": copy.deepcopy(nodes[0]),
                    "path": ["nodes", 0]}
        changed = {"parentId": None, "index": 1, "node": copy.deepcopy(nodes[0]),
                   "path": ["nodes", 1]}

        event = client.build_node_structure_event(original, changed, nodes)
        original["node"]["futureField"]["keep"] = False
        changed["node"]["text"] = "mutated"

        self.assertEqual(event["name"], "structureChanged")
        self.assertEqual(event["changed"][0]["original"]["node"], nodes[0])
        self.assertEqual(event["changed"][0]["changed"]["node"], nodes[0])
        self.assertEqual(event["changed"][0]["changed"]["parentId"], None)

    def test_build_structure_event_rejects_invalid_paths_parent_and_self_descendant(self):
        nodes = [{"id": "A", "text": "A", "children": [
            {"id": "B", "text": "B", "children": []},
        ]}]
        original = {"parentId": None, "index": 0, "node": copy.deepcopy(nodes[0]),
                    "path": ["nodes", 0]}
        invalid = [
            {"parentId": None, "index": 1, "node": copy.deepcopy(nodes[0]),
             "path": ["nodes", 0]},
            {"parentId": "B", "index": 0, "node": copy.deepcopy(nodes[0]),
             "path": ["nodes", 0]},
            {"parentId": "B", "index": 0, "node": copy.deepcopy(nodes[0]),
             "path": ["nodes", 0, "children", 0, "children", 0]},
        ]
        client = self.make_client(nodes)
        for changed in invalid:
            with self.subTest(changed=changed):
                with self.assertRaises(MubuError):
                    client.build_node_structure_event(original, changed, nodes)

    def test_structure_write_moves_source_before_or_after_target(self):
        cases = [
            (["A", "B", "C"], "A", 1, ["B", "A", "C"]),
            (["A", "B", "C"], "C", 0, ["C", "A", "B"]),
        ]
        for initial_ids, moving_id, target_index, expected_ids in cases:
            with self.subTest(moving_id=moving_id, target_index=target_index):
                nodes = [{"id": node_id, "text": node_id, "children": []}
                         for node_id in initial_ids]
                client = self.make_client(nodes)
                source_index = initial_ids.index(moving_id)
                source = {"parentId": None, "index": source_index,
                          "node": copy.deepcopy(nodes[source_index]),
                          "path": ["nodes", source_index]}
                destination = {"parentId": None, "index": target_index,
                               "node": copy.deepcopy(nodes[source_index]),
                               "path": ["nodes", target_index]}
                event = client.build_node_structure_event(source, destination, nodes)
                expected = MubuClient._apply_node_events(nodes, [event])
                client._write_events_verified("D1", [event], expected, 4)
                self.assertEqual([node["id"] for node in client._test_store["nodes"]], expected_ids)

    def test_sync_recomputes_paths_for_multiple_nested_moves(self):
        client = self.make_client([
            {"id": "A", "text": "A", "children": [
                {"id": "A1", "text": "A1", "children": []},
                {"id": "A2", "text": "A2", "children": []},
                {"id": "A3", "text": "A3", "children": []},
            ]},
            {"id": "B", "text": "B", "children": [
                {"id": "B1", "text": "B1", "children": []},
            ]},
        ])

        client.sync_doc_definition("D1", {"nodes": [
            {"id": "md_1", "text": "A", "children": [
                {"id": "md_7", "text": "A3", "children": []},
                {"id": "md_3", "text": "A2", "children": []},
                {"id": "md_6", "text": "A1", "children": []},
            ]},
            {"id": "md_4", "text": "B", "children": [
                {"id": "md_5", "text": "B1", "children": []},
            ]},
        ]})

        self.assertEqual([node["id"] for node in client._test_store["nodes"][0]["children"]],
                         ["A3", "A2", "A1"])
        events = [event for batch in client._test_store["events"] for event in batch]
        structure_events = [event for event in events if event["name"] == "structureChanged"]
        self.assertEqual(len(structure_events), 2)
        self.assertTrue(all(len(event["changed"]) == 1 for event in structure_events))
        second_source = structure_events[1]["changed"][0]["original"]
        self.assertEqual(second_source["node"]["id"], "A2")
        self.assertEqual(second_source["path"], ["nodes", 0, "children", 2])

    def test_sync_stops_after_structure_readback_failure(self):
        client = self.make_client([
            {"id": "A", "text": "A", "children": []},
            {"id": "B", "text": "B", "children": []},
            {"id": "C", "text": "C", "children": []},
        ], apply_events=False)

        with mock.patch.object(client_module.time, "sleep", lambda _seconds: None):
            with self.assertRaises(MubuError):
                client.sync_doc_definition("D1", {"nodes": [
                    {"id": "md_1", "text": "C", "children": []},
                    {"id": "md_2", "text": "A", "children": []},
                    {"id": "md_3", "text": "B", "children": []},
                ]})

        self.assertEqual(len(client._test_store["attempts"]), 1)

    def test_sync_stops_after_structure_version_conflict(self):
        client = self.make_client([
            {"id": "A", "text": "A", "children": []},
            {"id": "B", "text": "B", "children": []},
            {"id": "C", "text": "C", "children": []},
            {"id": "D", "text": "D", "children": []},
        ])
        request = client._request
        structure_requests = {"count": 0}

        def conflict_before_second_move(*args, **kwargs):
            payload = kwargs.get("json", {})
            events = payload.get("events", [])
            if events and events[0].get("name") == "structureChanged":
                structure_requests["count"] += 1
                if structure_requests["count"] == 2:
                    client._test_store["version"] += 1
            return request(*args, **kwargs)

        client._request = conflict_before_second_move

        with self.assertRaises(MubuError):
            client.sync_doc_definition("D1", {"nodes": [
                {"id": "md_1", "text": "C", "children": []},
                {"id": "md_2", "text": "D", "children": []},
                {"id": "md_3", "text": "A", "children": []},
                {"id": "md_4", "text": "B", "children": []},
            ]})

        accepted = [event for batch in client._test_store["events"] for event in batch]
        self.assertEqual([event["name"] for event in accepted], ["structureChanged"])
        self.assertEqual(structure_requests["count"], 2)

    def test_sync_matches_repeated_text_using_expressed_note_before_path(self):
        client = self.make_client([
            {"id": "A", "text": "Repeat", "note": "drop", "color": "red", "children": []},
            {"id": "B", "text": "Repeat", "note": "keep", "color": "blue", "children": []},
        ])

        client.sync_doc_definition("D1", {"nodes": [
            {"id": "md_1", "text": "Repeat", "note": "keep", "children": []},
        ]})

        self.assertEqual(len(client._test_store["nodes"]), 1)
        self.assertEqual(client._test_store["nodes"][0]["id"], "B")
        self.assertEqual(client._test_store["nodes"][0]["color"], "blue")
        self.assertEqual(client._test_store["nodes"][0]["note"], "keep")

    def test_sync_rejects_semantically_ambiguous_repeated_nodes_before_write(self):
        client = self.make_client([
            {"id": "A", "text": "Repeat", "note": "keep", "color": "red", "children": []},
            {"id": "B", "text": "Repeat", "note": "keep", "color": "blue", "children": []},
        ])

        with self.assertRaises(MubuError):
            client.sync_doc_definition("D1", {"nodes": [
                {"id": "md_1", "text": "Repeat", "note": "keep", "children": []},
            ]})

        self.assertEqual(client._test_store["events"], [])
        self.assertEqual(client._test_store["attempts"], [])

    def test_sync_update_preserves_highlight_color_and_nested_future_fields(self):
        initial = [{
            "id": "A", "text": "before", "highlight": "yellow", "color": "#123456",
            "futureField": {"nested": [1, {"keep": True}]}, "children": [],
        }]
        client = self.make_client(initial)

        client.sync_doc_definition("D1", {"nodes": [{"id": "node_1", "text": "after", "children": []}]})

        updated = client._test_store["nodes"][0]
        self.assertEqual(updated["id"], "A")
        self.assertEqual(updated["highlight"], "yellow")
        self.assertEqual(updated["color"], "#123456")
        self.assertEqual(updated["futureField"], {"nested": [1, {"keep": True}]})

    def test_sync_clears_a_previously_checked_markdown_task_when_checkbox_is_absent(self):
        client = self.make_client([{"id": "A", "text": "Task", "finish": True, "children": []}])

        client.sync_doc_definition("D1", {"nodes": [{"id": "node_1", "text": "Task", "children": []}]})

        self.assertIs(client._test_store["nodes"][0]["finish"], False)

    def test_verified_node_write_raises_when_readback_does_not_match(self):
        client = self.make_client([{"id": "A", "text": "before", "children": []}], apply_events=False)

        with mock.patch.object(client_module.time, "sleep", lambda _seconds: None):
            with self.assertRaises(MubuError) as error:
                client.update_node_text("D1", ["nodes", 0], "after")
        self.assertRegex(str(error.exception), "验证|读回|readback")

    def test_delete_top_nodes_binds_original_indices_to_ids_and_stops_on_version_change(self):
        client = self.make_client([
            {"id": "A", "text": "A", "children": []},
            {"id": "B", "text": "B", "children": []},
            {"id": "C", "text": "C", "children": []},
        ])
        request = client._request
        injected = {"done": False}

        def mutate_after_first_delete(*args, **kwargs):
            result = request(*args, **kwargs)
            payload = kwargs.get("json", {})
            if payload.get("events") and not injected["done"]:
                store = client._test_store
                store["nodes"].insert(0, {"id": "X", "text": "external", "children": []})
                store["version"] += 1
                injected["done"] = True
            return result

        client._request = mutate_after_first_delete
        with self.assertRaises(MubuError) as error:
            client.delete_top_nodes("D1", [0, 1])

        self.assertRegex(str(error.exception), "版本|变化|冲突|target|验证|读回")
        first_batch = client._test_store["events"][0]
        deleted = [entry["node"]["id"] for event in first_batch for entry in event.get("deleted", [])]
        self.assertEqual(deleted, ["B"])
        self.assertIn("X", [node["id"] for node in client._test_store["nodes"]])

    def test_delete_top_nodes_validates_all_indices_before_any_write(self):
        for indices in ([0, 9], [0, "1"], [0, 0], [True], [-1]):
            with self.subTest(indices=indices):
                client = self.make_client([
                    {"id": "A", "text": "A", "children": []},
                    {"id": "B", "text": "B", "children": []},
                ])
                with self.assertRaises(MubuError):
                    client.delete_top_nodes("D1", indices)
                self.assertEqual(client._test_store["events"], [])
                self.assertEqual(client._test_store["attempts"], [])

    def test_stale_version_is_atomically_rejected_by_write_server_fake(self):
        client = self.make_client([{"id": "A", "text": "before", "children": []}])
        store = client._test_store
        event = client.build_node_update_event(
            ["nodes", 0], store["nodes"][0], {**store["nodes"][0], "text": "after"})

        with self.assertRaises(MubuError):
            client._save_doc_unverified("D1", events=[event], version=3)

        self.assertEqual(store["nodes"][0]["text"], "before")
        self.assertEqual(store["version"], 4)
        self.assertEqual(store["events"], [])
        self.assertEqual(len(store["attempts"]), 1)

    def test_create_doc_validates_all_content_before_remote_create(self):
        client = self.make_client([])
        calls = []
        def request(*args, **kwargs):
            calls.append(kwargs)
            return {"id": "D1"}
        client._request = request
        client.append_top_nodes = lambda _doc_id, _nodes: []

        with self.assertRaises(MubuError):
            client.create_doc("Doc", "F1", '{"nodes":[{"text":"ok","children":[null]}]}')
        self.assertEqual(calls, [])

    def test_create_doc_rejects_non_json_node_metadata_before_remote_create(self):
        client = self.make_client([])
        calls = []
        client._request = lambda *args, **kwargs: calls.append(kwargs) or {"id": "D1"}

        with self.assertRaises(MubuError):
            client.create_doc("Doc", "F1", {"nodes": [{"text": "ok", "custom": {1, 2}}]})
        self.assertEqual(calls, [])

    def test_create_doc_reports_partial_append_progress_without_deleting_document(self):
        client = self.make_client([])
        client._request = lambda *args, **kwargs: {"id": "D-created"}
        append_calls = []

        def append_one(doc_id, nodes):
            append_calls.append((doc_id, copy.deepcopy(nodes)))
            if len(append_calls) == 1:
                return ["N1"]
            raise MubuError("verification failed")

        client.append_top_nodes = append_one
        delete = mock.Mock()
        client.delete_doc = delete

        with self.assertRaises(MubuError) as error:
            client.create_doc("Doc", "F1", json.dumps({"nodes": [{"text": "one"}, {"text": "two"}]}))

        self.assertIn("D-created", str(error.exception))
        self.assertTrue("1/2" in str(error.exception) or "1 个" in str(error.exception))
        self.assertEqual(len(append_calls), 2)
        delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
