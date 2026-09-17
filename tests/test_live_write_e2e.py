import copy
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mubu.config import MubuError
from scripts.validation import live_write_e2e


class FakeClient:
    def __init__(self, *, create_result="doc-1", fail_at=None,
                 cleanup_error=False, bad_task_defaults=False,
                 summary_error=None):
        self.create_result = create_result
        self.fail_at = fail_at
        self.cleanup_error = cleanup_error
        self.bad_task_defaults = bad_task_defaults
        self.summary_error = summary_error
        self.created = []
        self.purged = []
        self.calls = []
        self.api_calls = []
        self.sync_snapshots = []
        self.nodes = []
        self._ids = iter(("A", "N", "G", "B", "C", "I", "D"))

    def _maybe_fail(self, operation):
        self.calls.append(operation)
        if self.fail_at == operation:
            raise RuntimeError(operation)

    def _record(self, method, **kwargs):
        self.api_calls.append({"method": method, **copy.deepcopy(kwargs)})

    def create_doc(self, name, folder_id, content):
        self._record("create_doc", name=name, folder_id=folder_id, content=content)
        self._maybe_fail("create")
        self.created.append((name, folder_id, content))
        if self.create_result is None:
            return None
        self.nodes = []
        return self.create_result

    def get_doc(self, doc_id):
        self._record("get_doc", doc_id=doc_id)
        self._maybe_fail("read")
        result = {"name": self.created[0][0], "nodes": copy.deepcopy(self.nodes)}
        if (self.bad_task_defaults and result["nodes"]
                and result["nodes"][0].get("taskStatus") in (1, 2)):
            result["nodes"][0]["finish"] = True
        return result

    def append_top_nodes(self, doc_id, nodes):
        self._record("append_top_nodes", doc_id=doc_id, nodes=nodes)
        self._maybe_fail("append")
        result = []

        def assign_ids(node):
            node.setdefault("id", next(self._ids))
            for child in node.get("children", []) or []:
                assign_ids(child)

        for source in nodes:
            node = copy.deepcopy(source)
            assign_ids(node)
            self.nodes.append(node)
            result.append(node["id"])
        return result

    def _node_at_path(self, path):
        siblings = self.nodes
        for offset in range(0, len(path), 2):
            if path[offset] not in {"nodes", "children"}:
                raise AssertionError("unexpected node path")
            node = siblings[path[offset + 1]]
            if offset + 2 == len(path):
                return node
            siblings = node.setdefault("children", [])
        raise AssertionError("path did not select a node")

    def _siblings_for_path(self, path):
        siblings = self.nodes
        for offset in range(0, len(path) - 2, 2):
            node = siblings[path[offset + 1]]
            siblings = node.setdefault("children", [])
        return siblings, path[-1]

    def update_node_text(self, doc_id, path, text):
        self._record("update_node_text", doc_id=doc_id, path=path, text=text)
        self._maybe_fail("update")
        self._node_at_path(path)["text"] = text
        return True

    def insert_child_nodes(self, doc_id, parent_path, texts):
        self._record("insert_child_nodes", doc_id=doc_id,
                     parent_path=parent_path, texts=texts)
        self._maybe_fail("insert")
        parent = self._node_at_path(parent_path)
        result = []
        for text in ([texts] if isinstance(texts, str) else texts):
            node = {"id": next(self._ids), "text": text, "children": [],
                    "taskStatus": 0}
            parent.setdefault("children", []).append(node)
            result.append(node["id"])
        return result

    def set_node_fields(self, doc_id, path, fields):
        self._record("set_node_fields", doc_id=doc_id, path=path, fields=fields)
        self._maybe_fail("fields")
        node = self._node_at_path(path)
        for key, value in fields.items():
            node[key] = value
        return True

    def sync_doc_definition(self, doc_id, definition):
        self._record("sync_doc_definition", doc_id=doc_id, definition=definition)
        self._maybe_fail("sync")
        current_ids = self._flatten_ids(self.nodes)
        target_ids = self._flatten_ids(definition["nodes"])
        if set(current_ids) != set(target_ids):
            raise AssertionError("sync must preserve stable node IDs")
        self.sync_snapshots.append(copy.deepcopy(definition))
        self.nodes = copy.deepcopy(definition["nodes"])
        return {"updated": 1}

    @staticmethod
    def _flatten_ids(nodes):
        result = []
        for node in nodes:
            result.append(node.get("id"))
            result.extend(FakeClient._flatten_ids(node.get("children") or []))
        return result

    def delete_node(self, doc_id, path):
        self._record("delete_node", doc_id=doc_id, path=path)
        self._maybe_fail("delete_nested")
        target, index = self._siblings_for_path(path)
        target.pop(index)
        return True

    def delete_top_nodes(self, doc_id, indices):
        self._record("delete_top_nodes", doc_id=doc_id, indices=indices)
        self._maybe_fail("delete")
        for index in sorted((indices if isinstance(indices, list) else [indices]), reverse=True):
            self.nodes.pop(index)
        return len(indices if isinstance(indices, list) else [indices])

    def create_summary(self, doc_id, node_paths, text=""):
        self._record("create_summary", doc_id=doc_id, node_paths=node_paths, text=text)
        self._maybe_fail("summary_create")
        if self.summary_error is not None:
            raise MubuError(
                self.summary_error,
                body="SECRET_TOKEN=do-not-print; Authorization: Bearer secret",
            )
        members = [self._node_at_path(path) for path in node_paths]
        member_ids = [node["id"] for node in members]
        summary = {"id": "summary-1", "createdAt": 1, "modified": 1,
                   "text": text, "memberIds": member_ids, "children": []}
        for node in members:
            node.setdefault("summaryData", []).append(copy.deepcopy(summary))
        return summary["id"]

    def delete_summary(self, doc_id, node_paths, summary_id):
        self._record("delete_summary", doc_id=doc_id, node_paths=node_paths,
                     summary_id=summary_id)
        self._maybe_fail("summary_delete")
        for path in node_paths:
            node = self._node_at_path(path)
            node["summaryData"] = [item for item in node.get("summaryData", [])
                                    if item.get("id") != summary_id]
        return len(node_paths)

    def move_node(self, doc_id, path, to_index, to_parent_path=None):
        self._record("move_node", doc_id=doc_id, path=path, to_index=to_index,
                     to_parent_path=to_parent_path)
        self._maybe_fail("move_node")
        source, source_index = self._siblings_for_path(path)
        moving = source.pop(source_index)
        if to_parent_path is None:
            target = self.nodes
        else:
            target = self._node_at_path(to_parent_path).setdefault("children", [])
        target.insert(to_index, moving)
        return True

    def attach_image(self, doc_id, path, file, width=None):
        self._record("attach_image", doc_id=doc_id, path=path, file=file, width=width)
        self._maybe_fail("image")
        node = self._node_at_path(path)
        node["images"] = [{"id": "image-1", "uri": "document_image/fake.jpg",
                           "ow": 17, "oh": 23, "w": width or 400}]
        node["imageLayouts"] = [{"count": 1}]
        return True

    def link_nodes(self, doc_id, from_path, to_path, side="right"):
        self._record("link_nodes", doc_id=doc_id, from_path=from_path,
                     to_path=to_path, side=side)
        self._maybe_fail("link_nodes")
        source = self._node_at_path(from_path)
        target = self._node_at_path(to_path)
        source["linkLines"] = [{"id": "line-1", "fromNodeId": source["id"],
                                "toNodeId": target["id"], "fromAttachment": {"side": side, "t": 0.5},
                                "toAttachment": {"side": side, "t": 0.5},
                                "controlPoints": [[0, 0], [0, 0]]}]
        return True

    def purge_item(self, item_id, item_type=None):
        self._record("purge_item", item_id=item_id, item_type=item_type)
        self._maybe_fail("purge")
        if self.cleanup_error:
            raise RuntimeError("cleanup")
        self.purged.append((item_id, item_type))


class LiveWriteValidationTests(unittest.TestCase):
    def test_summary_failure_emits_bounded_member_summary_diagnostic_only(self):
        client = FakeClient(summary_error="summary service rejected request")
        output = []

        self.assertFalse(live_write_e2e.run_live_write_validation(
            client=client, confirmed=True, emit=output.append))

        diagnostic_lines = [
            line for line in output if "summary readback diagnostic" in line
        ]
        self.assertEqual(len(diagnostic_lines), 1)
        diagnostic = diagnostic_lines[0].split("summaryData=", 1)[1]
        payload = json.loads(diagnostic)
        self.assertEqual(
            payload,
            {
                "members": [
                    {"path": ["nodes", 0, "children", 1], "summaryData": []},
                    {"path": ["nodes", 0, "children", 2], "summaryData": []},
                ]
            },
        )
        self.assertNotIn(live_write_e2e.TOP_A, diagnostic)
        self.assertNotIn(live_write_e2e.INSERTED_CHILD, diagnostic)
        self.assertNotIn("SECRET_TOKEN", diagnostic)
        self.assertNotIn("Authorization", diagnostic)

        large_client = FakeClient()
        large_client.created = [("test-name", "0", "")]
        large_client.nodes = [{
            "id": "A",
            "children": [
                {"id": "N"},
                {"id": "I", "summaryData": [{"text": "x" * 4000}]},
                {"id": "D", "summaryData": [{"text": "y" * 4000}]},
            ],
        }]
        large_output = []
        live_write_e2e._emit_summary_failure_diagnostic(
            "test-name", large_client, "D1",
            [["nodes", 0, "children", 1], ["nodes", 0, "children", 2]],
            large_output.append,
        )
        large_diagnostic = large_output[0].split("summaryData=", 1)[1]
        self.assertLessEqual(
            len(large_diagnostic), live_write_e2e.SUMMARY_DIAGNOSTIC_LIMIT)
        self.assertEqual(
            json.loads(large_diagnostic)["members"][0]["summaryData"],
            "<truncated>",
        )

    def test_step_failure_includes_only_safe_mubu_message(self):
        output = []
        error = MubuError(
            "summary write rejected (doc_id=D1)",
            status_code=400,
            body="SECRET_TOKEN=do-not-print; Authorization: Bearer secret",
        )

        with self.assertRaises(live_write_e2e._StepFailed):
            live_write_e2e._run_step(
                "test-name", None, "D1", "summary write", lambda: (_ for _ in ()).throw(error),
                lambda: None, output.append)

        self.assertEqual(
            output,
            ["[FAIL] name=test-name summary write (MubuError: summary write rejected (doc_id=D1))"],
        )
        self.assertNotIn("SECRET_TOKEN", output[0])
        self.assertNotIn("Authorization", output[0])

        output.clear()
        with self.assertRaises(live_write_e2e._StepFailed):
            live_write_e2e._run_step(
                "test-name", None, "D1", "ordinary failure",
                lambda: (_ for _ in ()).throw(RuntimeError("ordinary detail")),
                lambda: None, output.append)
        self.assertEqual(output, ["[FAIL] name=test-name ordinary failure (RuntimeError)"])

        output.clear()
        long_message = "x" * 301
        with self.assertRaises(live_write_e2e._StepFailed):
            live_write_e2e._run_step(
                "test-name", None, "D1", "bounded failure",
                lambda: (_ for _ in ()).throw(MubuError(long_message)),
                lambda: None, output.append)
        self.assertEqual(
            output,
            ["[FAIL] name=test-name bounded failure (MubuError: "
             + ("x" * 300) + ")"],
        )

    def test_requires_confirmation_before_client_or_document_creation(self):
        constructed = []

        def factory():
            constructed.append(True)
            return FakeClient()

        output = []
        self.assertFalse(live_write_e2e.run_live_write_validation(
            confirmed=False, client_factory=factory, emit=output.append))
        self.assertEqual(constructed, [])
        self.assertEqual(output, ["[FAIL] explicit confirmation is required; no client or document was created"])

    def test_full_acceptance_cleans_up_returned_id_and_logs_one_name(self):
        client = FakeClient()
        output = []
        self.assertTrue(live_write_e2e.run_live_write_validation(
            client=client, confirmed=True, emit=output.append))
        self.assertEqual(client.purged, [("doc-1", "doc")])
        name = client.created[0][0]
        self.assertTrue(name.startswith(live_write_e2e.DOC_NAME_PREFIX))
        self.assertTrue(output)
        self.assertTrue(all(f"name={name}" in line for line in output))
        self.assertIn("same-parent order ABC", " ".join(output))
        self.assertIn("same-parent order CAB", " ".join(output))
        self.assertIn("taskStatus", " ".join(output))
        self.assertIn("emoji", " ".join(output))
        self.assertIn("mask", " ".join(output))
        self.assertIn("highlight", " ".join(output))
        self.assertIn("formula", " ".join(output))
        self.assertIn("mention", " ".join(output))
        self.assertIn("summary", " ".join(output))
        self.assertIn("collapse", " ".join(output))
        self.assertIn("image", " ".join(output))
        self.assertIn("link", " ".join(output))
        # The live gate runs in a Windows PowerShell console that may use
        # CP936.  Its progress labels must never crash after a successful write.
        for line in output:
            line.encode("cp936")
        self.assertIn("sync", client.calls)

        writes = [call for call in client.api_calls if call["method"] in {
            "create_doc", "append_top_nodes", "sync_doc_definition",
            "update_node_text", "insert_child_nodes", "set_node_fields",
            "create_summary", "delete_summary", "move_node", "attach_image",
            "link_nodes", "delete_node", "delete_top_nodes", "purge_item",
        }]
        self.assertEqual(
            [call["method"] for call in writes],
            ["create_doc", "append_top_nodes", "move_node", "move_node",
             "move_node", "move_node",
             "update_node_text", "insert_child_nodes", "create_summary",
             "delete_summary", "set_node_fields", "set_node_fields",
             "sync_doc_definition", "set_node_fields", "set_node_fields",
             "set_node_fields", "set_node_fields", "set_node_fields",
             "set_node_fields", "set_node_fields", "set_node_fields",
             "delete_node", "move_node", "attach_image", "link_nodes",
             "delete_top_nodes", "purge_item"],
        )
        doc_id = "doc-1"
        self.assertEqual(writes[0]["folder_id"], "0")
        self.assertEqual(writes[0]["content"], "")
        self.assertEqual(writes[1]["doc_id"], doc_id)
        self.assertEqual(writes[2]["path"], ["nodes", 2])
        self.assertEqual(writes[2]["to_index"], 0)
        self.assertEqual(writes[3]["path"], ["nodes", 0])
        self.assertEqual(writes[3]["to_index"], 2)
        self.assertEqual(writes[4]["path"], ["nodes", 0, "children", 0])
        self.assertEqual(writes[4]["to_index"], 1)
        self.assertIsNone(writes[4]["to_parent_path"])
        self.assertEqual(writes[5]["path"], ["nodes", 1])
        self.assertEqual(writes[5]["to_index"], 0)
        self.assertEqual(writes[5]["to_parent_path"], ["nodes", 0])
        self.assertEqual(writes[6]["path"], ["nodes", 0])
        self.assertEqual(writes[7]["parent_path"], ["nodes", 0])
        field_writes = [call for call in writes if call["method"] == "set_node_fields"]
        self.assertEqual([call["fields"] for call in field_writes], [
            {"taskStatus": 1}, {"taskStatus": 2}, {"emoji": live_write_e2e.EMOJI},
            {"collapsed": True}, {"collapsed": False}, {"text": live_write_e2e.FORMULA_HTML},
            {"text": live_write_e2e.MENTION_HTML}, {"text": live_write_e2e.NODE_MENTION_HTML},
            {"text": live_write_e2e.MASK_HTML}, {"text": live_write_e2e.HIGHLIGHT_HTML},
        ])
        self.assertEqual(writes[-3]["method"], "link_nodes")
        self.assertEqual(writes[-2]["indices"], [1])

        self.assertEqual(len(client.sync_snapshots), 1)
        stable_ids = client._flatten_ids(client.sync_snapshots[0]["nodes"])
        self.assertEqual(sorted(stable_ids), ["A", "B", "C", "D", "G", "I", "N"])
        self.assertEqual(client.sync_snapshots[0]["nodes"][0]["text"], live_write_e2e.TOP_A_UPDATED)

        new_methods = [call["method"] for call in client.api_calls if call["method"] in {
            "create_summary", "delete_summary", "move_node", "attach_image", "link_nodes",
        }]
        self.assertEqual(new_methods, [
            "move_node", "move_node", "move_node", "move_node",
            "create_summary", "delete_summary",
            "move_node", "attach_image", "link_nodes",
        ])
        image_calls = [call for call in client.api_calls if call["method"] == "attach_image"]
        self.assertEqual(len(image_calls), 1)
        self.assertFalse(Path(image_calls[0]["file"]).exists())

        summaries = [call for call in client.api_calls
                     if call["method"] in {"create_summary", "delete_summary"}]
        self.assertEqual(summaries[0]["node_paths"], [
            ["nodes", 0, "children", 1],
            ["nodes", 0, "children", 2],
        ])
        self.assertEqual(summaries[0]["text"], "")
        self.assertEqual(summaries[1]["node_paths"], summaries[0]["node_paths"])
        self.assertEqual(summaries[1]["summary_id"], "summary-1")

    def test_full_acceptance_promotes_nested_node_to_root_and_restores_it(self):
        client = FakeClient()
        output = []

        self.assertTrue(live_write_e2e.run_live_write_validation(
            client=client, confirmed=True, emit=output.append))

        nested_root_moves = [
            call for call in client.api_calls
            if call["method"] == "move_node"
            and len(call["path"]) > 2
            and call["to_parent_path"] is None
        ]
        self.assertEqual(len(nested_root_moves), 1)
        self.assertEqual(nested_root_moves[0]["path"],
                         ["nodes", 0, "children", 0])
        self.assertEqual(nested_root_moves[0]["to_index"], 1)

        restored_moves = [
            call for call in client.api_calls
            if call["method"] == "move_node"
            and call["to_parent_path"] == ["nodes", 0]
        ]
        self.assertEqual(len(restored_moves), 1)
        self.assertEqual(restored_moves[0]["to_index"], 0)
        self.assertIn("nested node to root", " ".join(output))

    def test_task_status_readback_requires_default_task_fields(self):
        client = FakeClient()
        output = []
        self.assertTrue(live_write_e2e.run_live_write_validation(
            client=client, confirmed=True, emit=output.append))
        status_calls = [call for call in client.api_calls
                        if call["method"] == "set_node_fields"
                        and "taskStatus" in call["fields"]]
        self.assertEqual([call["fields"] for call in status_calls],
                         [{"taskStatus": 1}, {"taskStatus": 2}])

    def test_task_status_rejects_non_default_task_fields(self):
        client = FakeClient(bad_task_defaults=True)
        output = []
        self.assertFalse(live_write_e2e.run_live_write_validation(
            client=client, confirmed=True, emit=output.append))
        self.assertEqual(client.purged, [("doc-1", "doc")])
        self.assertIn("taskStatus 0", " ".join(output))

    def test_nested_delete_requires_formal_path_and_readback_absence(self):
        client = FakeClient()
        output = []
        self.assertTrue(live_write_e2e.run_live_write_validation(
            client=client, confirmed=True, emit=output.append))
        nested = [call for call in client.api_calls
                  if call["method"] == "delete_node"]
        self.assertEqual(len(nested), 1)
        self.assertEqual(nested[0]["path"], ["nodes", 0, "children", 2])
        self.assertIn("nested", " ".join(output))

    def test_sync_is_orchestration_with_stable_ids_and_nested_structure(self):
        client = FakeClient()
        output = []
        self.assertTrue(live_write_e2e.run_live_write_validation(
            client=client, confirmed=True, emit=output.append))
        self.assertEqual(len(client.sync_snapshots), 1)
        for snapshot in client.sync_snapshots:
            for node in snapshot["nodes"]:
                self.assertIsInstance(node.get("id"), str)
                self.assertIn("children", node)

    def test_mid_sequence_failure_still_purges_returned_id(self):
        client = FakeClient(fail_at="insert")
        output = []
        self.assertFalse(live_write_e2e.run_live_write_validation(
            client=client, confirmed=True, emit=output.append))
        self.assertEqual(client.purged, [("doc-1", "doc")])
        self.assertIn("[FAIL]", " ".join(output))

    def test_image_temp_file_is_removed_when_image_write_fails(self):
        client = FakeClient(fail_at="image")
        output = []
        self.assertFalse(live_write_e2e.run_live_write_validation(
            client=client, confirmed=True, emit=output.append))
        image_calls = [call for call in client.api_calls
                       if call["method"] == "attach_image"]
        self.assertEqual(len(image_calls), 1)
        self.assertFalse(Path(image_calls[0]["file"]).exists())
        self.assertEqual(client.purged, [("doc-1", "doc")])

    def test_unknown_create_result_reports_name_without_purging_by_name(self):
        client = FakeClient(create_result=None)
        output = []
        self.assertFalse(live_write_e2e.run_live_write_validation(
            client=client, confirmed=True, emit=output.append))
        name = client.created[0][0]
        self.assertIn(name, " ".join(output))
        self.assertEqual(client.purged, [])
        self.assertNotIn("purge", client.calls)

    def test_cleanup_failure_is_failure(self):
        client = FakeClient(cleanup_error=True)
        output = []
        self.assertFalse(live_write_e2e.run_live_write_validation(
            client=client, confirmed=True, emit=output.append))
        self.assertIn("doc_id=doc-1", " ".join(output))

    def test_main_yes_is_only_entry_guard(self):
        constructed = []

        def factory():
            constructed.append(True)
            return FakeClient()

        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(live_write_e2e.main([], client_factory=factory), 2)
        self.assertEqual(constructed, [])


if __name__ == "__main__":
    unittest.main()
