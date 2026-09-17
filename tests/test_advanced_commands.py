import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from mubu.commands import (
    COMMANDS,
    _collapse,
    _emoji,
    _formula,
    _image,
    _link_nodes,
    _mention,
    _move_node,
    _node_mention,
    _summary_create,
    _summary_delete,
    _task,
)
from mubu.config import MubuError


class FakeClient:
    def __init__(self):
        self.calls = []

    def set_node_fields(self, doc_id, path, fields):
        self.calls.append(("set_node_fields", doc_id, path, fields))
        return True

    def create_summary(self, doc_id, paths, text=""):
        self.calls.append(("create_summary", doc_id, paths, text))
        return "summary-1"

    def delete_summary(self, doc_id, paths, summary_id):
        self.calls.append(("delete_summary", doc_id, paths, summary_id))
        return len(paths)

    def move_node(self, doc_id, path, index, parent=None):
        self.calls.append(("move_node", doc_id, path, index, parent))
        return True

    def attach_image(self, doc_id, path, file, width=None):
        self.calls.append(("attach_image", doc_id, path, file, width))
        return True

    def link_nodes(self, doc_id, from_path, to_path, side="right"):
        self.calls.append(("link_nodes", doc_id, from_path, to_path, side))
        return True


def args(**values):
    defaults = {
        "doc_id": "D1", "path": "0.1", "latex": "x^2", "block": False,
        "target_doc": "D2", "target_node": "N2", "name": "Target",
        "text": "Target node", "members": ["0", "0.1"],
        "summary_id": "S1", "value": "😄", "status": 1,
        "expand": False, "index": 2, "parent": None, "file": "photo.png",
        "width": None, "from_path": "0", "to_path": "0.1", "side": "right",
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


class AdvancedCommandTests(unittest.TestCase):
    def test_all_advanced_commands_are_registered(self):
        self.assertTrue({
            "formula", "mention", "node-mention", "summary-create",
            "summary-delete", "emoji", "task", "collapse", "move-node",
            "image", "link-nodes",
        } <= set(COMMANDS))

    def test_formula_uses_inline_converter_unless_block_requested(self):
        client = FakeClient()
        with patch("mubu.commands.formula_html", return_value="FORMULA") as converter:
            _formula(client, args())
        converter.assert_called_once_with("x^2", inline=True)
        self.assertEqual(client.calls[-1], (
            "set_node_fields", "D1", ["nodes", 0, "children", 1], {"text": "FORMULA"}))

        client = FakeClient()
        with patch("mubu.commands.formula_html", return_value="BLOCK") as converter:
            _formula(client, args(block=True))
        converter.assert_called_once_with("x^2", inline=False)
        self.assertEqual(client.calls[-1][3], {"text": "BLOCK"})

    def test_mentions_write_converter_output_to_requested_node(self):
        client = FakeClient()
        with patch("mubu.commands.mention_html", return_value="MENTION") as converter:
            _mention(client, args())
        converter.assert_called_once_with("D2", "Target")
        self.assertEqual(client.calls[-1][1:], (
            "D1", ["nodes", 0, "children", 1], {"text": "MENTION"}))

        client = FakeClient()
        with patch("mubu.commands.node_mention_html", return_value="NODE") as converter:
            _node_mention(client, args())
        converter.assert_called_once_with("D2", "N2", "Target node")
        self.assertEqual(client.calls[-1][3], {"text": "NODE"})

    def test_summary_commands_parse_each_member_path(self):
        client = FakeClient()
        with redirect_stdout(StringIO()):
            _summary_create(client, args())
        self.assertEqual(client.calls[-1], (
            "create_summary", "D1",
            [["nodes", 0], ["nodes", 0, "children", 1]], "Target node"))

        client = FakeClient()
        with redirect_stdout(StringIO()):
            _summary_delete(client, args())
        self.assertEqual(client.calls[-1], (
            "delete_summary", "D1",
            [["nodes", 0], ["nodes", 0, "children", 1]], "S1"))

    def test_field_commands_map_to_protocol_fields(self):
        client = FakeClient()
        with redirect_stdout(StringIO()):
            _emoji(client, args(value="🚀"))
            _task(client, args(status=2))
            _collapse(client, args(expand=False))
            _collapse(client, args(expand=True))
        self.assertEqual([call[3] for call in client.calls], [
            {"emoji": "🚀"}, {"taskStatus": 2},
            {"collapsed": True}, {"collapsed": False},
        ])

    def test_move_image_and_link_commands_map_paths_and_options(self):
        client = FakeClient()
        with redirect_stdout(StringIO()):
            _move_node(client, args(index=3, parent="1.0"))
            _image(client, args(file="photo.jpg", width=240))
            _link_nodes(client, args(from_path="0", to_path="1.2", side="left"))
        self.assertEqual(client.calls, [
            ("move_node", "D1", ["nodes", 0, "children", 1], 3,
             ["nodes", 1, "children", 0]),
            ("attach_image", "D1", ["nodes", 0, "children", 1], "photo.jpg", 240),
            ("link_nodes", "D1", ["nodes", 0], ["nodes", 1, "children", 2], "left"),
        ])

    def test_image_rejects_path_outside_current_workspace(self):
        client = FakeClient()
        with self.assertRaises(MubuError):
            _image(client, args(file="C:/outside-workspace/photo.png"))
        self.assertEqual(client.calls, [])

    def test_cli_registers_advanced_parser_arguments(self):
        from mubu.cli import main

        cases = [
            (["mubu", "formula", "D1", "--path", "0", "--latex", "x^2", "--block"],
             "formula", {"path": "0", "latex": "x^2", "block": True}),
            (["mubu", "mention", "D1", "--path", "0", "--target-doc", "D2", "--name", "N"],
             "mention", {"target_doc": "D2", "name": "N"}),
            (["mubu", "summary-create", "D1", "--member", "0", "--member", "0.1"],
             "summary-create", {"members": ["0", "0.1"]}),
            (["mubu", "task", "D1", "--path", "0", "--status", "2"],
             "task", {"status": 2}),
            (["mubu", "collapse", "D1", "--path", "0", "--expand"],
             "collapse", {"expand": True}),
            (["mubu", "move-node", "D1", "--path", "0", "3", "--parent", "1"],
             "move-node", {"index": 3, "parent": "1"}),
            (["mubu", "image", "D1", "--path", "0", "--file", "a.png", "--width", "80"],
             "image", {"width": 80}),
        ]
        for argv, command, expected in cases:
            with self.subTest(argv=argv):
                with patch("sys.argv", argv), patch("mubu.cli.MubuClient"), \
                        patch("mubu.cli.dispatch") as dispatch:
                    main()
                parsed = dispatch.call_args.args[1]
                self.assertEqual(parsed.command, command)
                for key, value in expected.items():
                    self.assertEqual(getattr(parsed, key), value)


if __name__ == "__main__":
    unittest.main()
