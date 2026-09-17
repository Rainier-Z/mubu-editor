"""Lossless Markdown outline structure round-trip tests."""

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from mubu.commands import _save
from mubu.config import MubuError
from mubu.convert import export_markdown, markdown_to_doc


def _shape(node):
    return {
        "text": node.get("text", ""),
        "checked": node.get("checked"),
        "note": node.get("note"),
        "children": [_shape(child) for child in node.get("children", [])],
    }


class MarkdownRoundtripTests(unittest.TestCase):
    def test_root_note_after_bullet_belongs_to_document_root(self):
        markdown = "# Doc\n- A\n> root note"

        doc = markdown_to_doc(markdown)

        self.assertEqual(doc["nodes"][0]["note"], "root note")
        self.assertNotIn("note", doc["nodes"][0]["children"][0])
        self.assertEqual(export_markdown(doc), markdown)

    def test_multiple_headings_remain_independent_top_level_nodes(self):
        markdown = "# First\n- child\n# Second\n- another child"

        doc = markdown_to_doc(markdown)

        self.assertEqual([node["text"] for node in doc["nodes"]], ["First", "Second"])
        self.assertEqual(
            [node["children"][0]["text"] for node in doc["nodes"]],
            ["child", "another child"],
        )
        self.assertEqual(export_markdown(doc), markdown)

    def test_same_named_top_level_nodes_remain_distinct(self):
        markdown = "# Repeat\n- first\n# Repeat\n- second"

        doc = markdown_to_doc(markdown)

        self.assertEqual(len(doc["nodes"]), 2)
        self.assertEqual(doc["nodes"][0]["text"], doc["nodes"][1]["text"])
        self.assertEqual(doc["nodes"][0]["text"], "Repeat")
        self.assertEqual(export_markdown(doc), markdown)

    def test_nested_nodes_notes_and_checkboxes_round_trip(self):
        original = {
            "nodes": [
                {
                    "text": "Plan",
                    "note": "root note",
                    "children": [
                        {
                            "text": "Phase",
                            "checked": False,
                            "note": "phase note",
                            "children": [
                                {"text": "done", "checked": True},
                                {"text": "open", "checked": False},
                            ],
                        }
                    ],
                },
                {"text": "Second root", "children": []},
            ]
        }

        markdown = export_markdown(original)
        restored = markdown_to_doc(markdown)

        self.assertEqual(
            [_shape(node) for node in restored["nodes"]],
            [_shape(node) for node in original["nodes"]],
        )

    def test_text_prefixes_spaces_multiline_notes_and_root_check_round_trip(self):
        original = {
            "nodes": [
                {
                    "text": "[x] title",
                    "checked": False,
                    "note": "first line\nsecond line",
                    "children": [
                        {
                            "text": "[x] literal",
                            "note": "child note",
                            "children": [{"text": "  trailing "}],
                        }
                    ],
                }
            ]
        }

        restored = markdown_to_doc(export_markdown(original))

        self.assertEqual(_shape(restored["nodes"][0]), _shape(original["nodes"][0]))

    def test_plain_text_single_line_round_trips_as_one_root(self):
        text = "Plain text without Markdown structure"

        doc = markdown_to_doc(text)
        restored = markdown_to_doc(export_markdown(doc))

        self.assertEqual(len(doc["nodes"]), 1)
        self.assertEqual(doc["nodes"][0]["text"], text)
        self.assertEqual(_shape(restored["nodes"][0]), _shape(doc["nodes"][0]))

    def test_plain_text_multiple_lines_round_trips_without_flattening(self):
        text = "First plain line\nSecond plain line"

        doc = markdown_to_doc(text)
        restored = markdown_to_doc(export_markdown(doc))

        self.assertEqual(len(doc["nodes"]), 1)
        self.assertEqual(doc["nodes"][0]["text"], text)
        self.assertEqual(_shape(restored["nodes"][0]), _shape(doc["nodes"][0]))

    def test_empty_markdown_is_an_empty_document(self):
        doc = markdown_to_doc(" \n\t\n")

        self.assertEqual(doc["nodes"], [])
        self.assertEqual(export_markdown(doc), "")

    def test_save_receives_all_parsed_top_level_nodes(self):
        from mubu import commands

        with tempfile.TemporaryDirectory() as temp_dir:
            markdown_file = Path(temp_dir) / "outline.md"
            markdown_file.write_text("# First\n# Second", encoding="utf-8")
            with patch.object(commands, "_safe_local_path", return_value=markdown_file):
                class Client:
                    definition = None

                    def sync_doc_definition(self, doc_id, definition):
                        self.doc_id = doc_id
                        self.definition = definition

                client = Client()
                _save(
                    client,
                    SimpleNamespace(md="outline.md", file=None, content=None, doc_id="doc"),
                )

        self.assertEqual(client.doc_id, "doc")
        self.assertEqual(
            [node["text"] for node in client.definition["nodes"]], ["First", "Second"]
        )

    def test_create_receives_all_parsed_top_level_nodes(self):
        from mubu import commands

        with tempfile.TemporaryDirectory() as temp_dir:
            markdown_file = Path(temp_dir) / "outline.md"
            markdown_file.write_text("# First\n# Second", encoding="utf-8")
            with patch.object(commands, "_safe_local_path", return_value=markdown_file):
                class Client:
                    content = None

                    def create_doc(self, name, folder, content):
                        self.content = content
                        return "doc-id"

                client = Client()
                with redirect_stdout(StringIO()):
                    commands._create(
                        client,
                        SimpleNamespace(md="outline.md", name="Doc", folder="folder"),
                    )

        definition = json.loads(client.content)
        self.assertEqual(
            [node["text"] for node in definition["nodes"]], ["First", "Second"]
        )

    def test_skipped_list_depth_is_rejected_instead_of_flattened(self):
        with self.assertRaises(MubuError):
            markdown_to_doc("# Root\n    - orphan")


if __name__ == "__main__":
    unittest.main()
