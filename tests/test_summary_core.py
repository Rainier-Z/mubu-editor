"""Offline contract tests for node summary changesets."""

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from mubu.client import MubuClient
from mubu.config import MubuError


class SummaryCoreTests(unittest.TestCase):
    def make_client(self, nodes):
        client = MubuClient.__new__(MubuClient)
        snapshot = {"baseVersion": 17, "definition": json.dumps({"nodes": nodes})}
        client._get_doc_raw = mock.Mock(return_value=copy.deepcopy(snapshot))
        client._write_events_verified = mock.Mock(return_value=copy.deepcopy(snapshot))
        client._gen_node_id = mock.Mock(return_value="summary-id")
        return client

    def test_create_summary_builds_one_consistent_update_for_all_members(self):
        client = self.make_client([
            {"id": "a", "text": "A", "children": []},
            {"id": "b", "text": "B", "summaryData": [
                {"id": "old", "text": "old summary"}
            ], "children": []},
        ])

        result = client.create_summary("D1", [["nodes", 1], ["nodes", 0]], "摘要")

        self.assertEqual(result, "summary-id")
        client._write_events_verified.assert_called_once()
        doc_id, events, expected, version = client._write_events_verified.call_args.args
        self.assertEqual((doc_id, version), ("D1", 17))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["name"], "update")
        entries = event["updated"]
        self.assertEqual([entry["path"] for entry in entries], [["nodes", 1], ["nodes", 0]])
        records = []
        for entry in entries:
            patch = entry["updated"]
            self.assertEqual(entry["original"]["id"], patch["id"])
            records.append(patch["summaryData"][-1])
        self.assertEqual(records[0], records[1])
        self.assertEqual(records[0], {
            "id": "summary-id", "createdAt": records[0]["createdAt"],
            "modified": records[0]["modified"], "text": "摘要",
            "memberIds": ["b", "a"], "children": [],
        })
        self.assertEqual(entries[0]["original"]["summaryData"], [
            {"id": "old", "text": "old summary"}
        ])
        self.assertNotIn("summaryData", entries[1]["original"])
        self.assertEqual(expected[0]["summaryData"][0]["id"], "summary-id")
        self.assertEqual(expected[1]["summaryData"][0]["id"], "old")
        self.assertEqual(expected[1]["summaryData"][1], records[0])

    def test_delete_summary_removes_only_requested_item_and_preserves_others(self):
        client = self.make_client([
            {"id": "a", "summaryData": [
                {"id": "keep-a"}, {"id": "remove"}
            ], "children": []},
            {"id": "b", "summaryData": [
                {"id": "remove", "text": "x"}, {"id": "keep-b"}
            ], "children": []},
        ])

        result = client.delete_summary("D1", [["nodes", 0], ["nodes", 1]], "remove")

        self.assertEqual(result, 2)
        _, events, expected, version = client._write_events_verified.call_args.args
        self.assertEqual(version, 17)
        entries = events[0]["updated"]
        self.assertEqual([entry["path"] for entry in entries], [["nodes", 0], ["nodes", 1]])
        self.assertEqual(entries[0]["original"]["summaryData"], [
            {"id": "keep-a"}, {"id": "remove"}
        ])
        self.assertEqual(entries[0]["updated"]["summaryData"], [{"id": "keep-a"}])
        self.assertEqual(entries[1]["updated"]["summaryData"], [{"id": "keep-b"}])
        self.assertEqual(expected[0]["summaryData"], [{"id": "keep-a"}])
        self.assertEqual(expected[1]["summaryData"], [{"id": "keep-b"}])

    def test_summary_paths_are_resolved_against_one_fresh_snapshot(self):
        client = self.make_client([{
            "id": "parent", "children": [{"id": "child", "children": []}]
        }])

        client.create_summary("D1", [["nodes", 0, "children", 0]])

        client._get_doc_raw.assert_called_once_with("D1")
        entry = client._write_events_verified.call_args.args[1][0]["updated"][0]
        self.assertEqual(entry["updated"]["id"], "child")

    def test_create_summary_rejects_duplicate_members_before_write(self):
        client = self.make_client([{"id": "a", "children": []}])

        with self.assertRaises(MubuError):
            client.create_summary("D1", [["nodes", 0], ["nodes", 0]])

        client._write_events_verified.assert_not_called()

    def test_delete_summary_requires_target_on_every_member_before_write(self):
        client = self.make_client([
            {"id": "a", "summaryData": [{"id": "summary"}], "children": []},
            {"id": "b", "summaryData": [], "children": []},
        ])

        with self.assertRaises(MubuError):
            client.delete_summary("D1", [["nodes", 0], ["nodes", 1]], "summary")

        client._write_events_verified.assert_not_called()

    def test_empty_summary_data_is_equivalent_to_service_omitting_the_field(self):
        self.assertTrue(MubuClient._nodes_semantically_equal(
            [{"id": "a"}],
            [{"id": "a", "summaryData": []}],
        ))

    def test_summary_record_omitted_empty_children_matches_expected_default(self):
        record = {
            "id": "summary", "createdAt": 101, "modified": 101,
            "text": "", "memberIds": ["a"],
        }
        self.assertTrue(MubuClient._nodes_semantically_equal(
            [{"id": "a", "summaryData": [copy.deepcopy(record)]}],
            [{"id": "a", "summaryData": [
                {**record, "children": []}
            ]}],
        ))

    def test_create_summary_uses_id_only_original_for_explicit_empty_summary_data(self):
        client = self.make_client([{"id": "a", "summaryData": [], "children": []}])

        client.create_summary("D1", [["nodes", 0]])

        entry = client._write_events_verified.call_args.args[1][0]["updated"][0]
        self.assertEqual(entry["original"], {"id": "a"})

    def test_create_summary_rejects_generated_id_colliding_with_any_snapshot_summary(self):
        client = self.make_client([
            {"id": "a", "summaryData": [{"id": "taken"}], "children": []},
            {"id": "b", "children": []},
        ])
        client._gen_node_id.return_value = "taken"

        with self.assertRaises(MubuError):
            client.create_summary("D1", [["nodes", 1]])

        client._write_events_verified.assert_not_called()

    def test_create_and_delete_reject_invalid_explicit_summary_data(self):
        for invalid in (None, {}, ["bad"], [None]):
            client = self.make_client([{
                "id": "a", "summaryData": invalid, "children": []
            }])
            with self.subTest(invalid=invalid):
                with self.assertRaises(MubuError):
                    client.create_summary("D1", [["nodes", 0]])
                client._write_events_verified.assert_not_called()

            client = self.make_client([{
                "id": "a", "summaryData": invalid, "children": []
            }])
            with self.subTest(operation="delete", invalid=invalid):
                with self.assertRaises(MubuError):
                    client.delete_summary("D1", [["nodes", 0]], "summary")
                client._write_events_verified.assert_not_called()

    def test_raw_update_payload_rejects_invalid_explicit_summary_data(self):
        client = MubuClient.__new__(MubuClient)
        client.member_id = "M1"
        client._request = mock.Mock()
        for invalid in (None, {}, ["bad"], [None]):
            event = {"name": "update", "updated": [{
                "updated": {"id": "a", "summaryData": invalid},
                "original": {"id": "a"}, "path": ["nodes", 0],
            }]}
            with self.subTest(invalid=invalid):
                with self.assertRaises(MubuError):
                    client._save_doc_unverified("D1", events=[event], version=4)
        client._request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
