"""Regression tests for task checkboxes and nested native-table paths."""

import json
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from mubu.commands import _export_table, _parse_node_path, _table
from mubu.config import MubuError
from mubu.convert import export_markdown, markdown_to_doc, rows_to_table_html


class TaskCheckboxTests(unittest.TestCase):
    def test_only_task_status_renders_protocol_checkbox_for_real_nodes(self):
        markdown = export_markdown({
            "nodes": [{
                "text": "ordinary table node",
                "finish": False,
                "taskStatus": 0,
                "children": [
                    {"text": "open task", "taskStatus": 1},
                    {"text": "done task", "taskStatus": 2},
                    {"text": "finish only", "finish": False},
                ],
            }],
        })

        self.assertEqual(
            markdown,
            "# ordinary table node\n- [ ] open task\n- [x] done task\n- finish only",
        )

    def test_legacy_checked_is_fallback_only_when_task_status_is_missing(self):
        markdown = export_markdown({
            "nodes": [{
                "text": "root",
                "children": [
                    {"text": "legacy open", "checked": False},
                    {"text": "protocol ordinary", "checked": True, "taskStatus": 0},
                ],
            }],
        })

        self.assertEqual(
            markdown,
            "# root\n- [ ] legacy open\n- protocol ordinary",
        )

    def test_markdown_checkboxes_create_task_status_and_round_trip(self):
        markdown = "# [ ] Root task\n- [x] Done child\n- [ ] Open child"

        doc = markdown_to_doc(markdown)
        root = doc["nodes"][0]

        self.assertEqual(root["taskStatus"], 1)
        self.assertEqual(root["children"][0]["taskStatus"], 2)
        self.assertEqual(root["children"][1]["taskStatus"], 1)
        self.assertEqual(export_markdown(doc), markdown)


class NodePathTests(unittest.TestCase):
    def test_parser_supports_dotted_paths_and_legacy_structural_paths(self):
        self.assertEqual(_parse_node_path("5"), ["nodes", 5])
        self.assertEqual(
            _parse_node_path("0.1"),
            ["nodes", 0, "children", 1],
        )
        self.assertEqual(
            _parse_node_path("nodes,0,children,1"),
            ["nodes", 0, "children", 1],
        )

    def test_parser_rejects_negative_empty_and_non_integer_segments(self):
        for spec in ("-1", "0..1", ".1", "0.", "0.a", "nodes,-1", "nodes,0,,children,1"):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    _parse_node_path(spec)


class TableCommandTests(unittest.TestCase):
    @staticmethod
    def args(**overrides):
        values = {
            "doc_id": "D1",
            "md": None,
            "json": None,
            "replace": None,
            "no_header": False,
            "parent_path": None,
            "format": "md",
            "index": None,
            "path": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_parent_path_inserts_table_as_child(self):
        client = SimpleNamespace(append_top_nodes=lambda *args: self.fail("must insert child"))
        client.insert_child_nodes = unittest.mock.Mock(return_value=["TABLE1"])
        output = StringIO()

        with patch("sys.stdin", StringIO(json.dumps([["Name"], ["Ada"]]))), redirect_stdout(output):
            _table(client, self.args(parent_path="0.1"))

        path, nodes = client.insert_child_nodes.call_args.args[1:]
        self.assertEqual(path, ["nodes", 0, "children", 1])
        self.assertEqual(len(nodes), 1)
        self.assertIn("<table", nodes[0])

    def test_parent_path_and_replace_cannot_be_combined(self):
        client = SimpleNamespace()
        with patch("sys.stdin", StringIO(json.dumps([["Name"], ["Ada"]]))):
            with self.assertRaises(MubuError):
                _table(client, self.args(parent_path="0", replace=2))

    def test_default_table_insert_remains_top_level(self):
        client = SimpleNamespace(
            append_top_nodes=unittest.mock.Mock(return_value=["TABLE1"]),
            insert_child_nodes=lambda *args: self.fail("must append at root"),
        )
        output = StringIO()

        with patch("sys.stdin", StringIO(json.dumps([["Name"], ["Ada"]]))), redirect_stdout(output):
            _table(client, self.args())

        self.assertEqual(client.append_top_nodes.call_count, 1)

    def test_export_table_resolves_nested_path(self):
        html = rows_to_table_html([["Name"], ["Ada"]])
        client = SimpleNamespace(get_doc=lambda _doc_id: {
            "nodes": [{
                "text": "Heading",
                "children": [{
                    "text": "section",
                    "children": [{"text": html}],
                }],
            }],
        })
        output = StringIO()

        with redirect_stdout(output):
            _export_table(client, self.args(path="0.0.0"))

        self.assertIn("| Name |", output.getvalue())
        self.assertIn("| Ada |", output.getvalue())

    def test_csv_export_neutralizes_spreadsheet_formula_prefixes(self):
        html = rows_to_table_html([["Name"], ["=HYPERLINK(\"https://evil.invalid\")"]])
        client = SimpleNamespace(get_doc=lambda _doc_id: {
            "nodes": [{"text": html, "children": []}],
        })
        output = StringIO()

        with redirect_stdout(output):
            _export_table(client, self.args(index=0, format="csv"))

        self.assertIn("'=HYPERLINK", output.getvalue())

    def test_export_table_requires_exactly_one_location(self):
        client = SimpleNamespace(get_doc=lambda _doc_id: {"nodes": []})
        with self.assertRaises(MubuError):
            _export_table(client, self.args())
        with self.assertRaises(MubuError):
            _export_table(client, self.args(index=0, path="0"))

    def test_cli_accepts_parent_path_and_export_path(self):
        from mubu.cli import main

        for argv, expected_command, expected_attribute, expected_value in (
            (["mubu", "table", "D1", "--parent-path", "0.1"], "table", "parent_path", "0.1"),
            (["mubu", "export-table", "D1", "--path", "0.1"], "export-table", "path", "0.1"),
            (["mubu", "export-table", "D1", "--index", "3"], "export-table", "index", 3),
        ):
            with self.subTest(argv=argv):
                with patch("sys.argv", argv), patch("mubu.cli.MubuClient"), patch("mubu.cli.dispatch") as dispatch:
                    main()
                args = dispatch.call_args.args[1]
                self.assertEqual(args.command, expected_command)
                self.assertEqual(getattr(args, expected_attribute), expected_value)

    def test_cli_rejects_both_export_locations(self):
        from mubu.cli import main

        with patch("sys.argv", ["mubu", "export-table", "D1", "--index", "0", "--path", "0.1"]), \
                patch("sys.stderr", StringIO()):
            with self.assertRaises(SystemExit):
                main()


if __name__ == "__main__":
    unittest.main()
