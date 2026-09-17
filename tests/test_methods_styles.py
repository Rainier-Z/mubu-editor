"""离线测试：用户可复用的幕布颜色与标题样式方法。"""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from mubu import cli
from mubu.client import MubuClient
from mubu.config import MubuError
from mubu.convert import style_text
from mubu.methods.colors import COLORS, color_class, normalize_color
from mubu.methods.headings import (
    HEADING_STYLES,
    HeadingStyle,
    append_headings,
    set_headings,
    style_heading,
)


class TestColorMethods:
    def test_registry_contains_template_colors_and_default_black(self):
        assert dict(COLORS) == {
            "red": "red",
            "yellow": "yellow",
            "green": "green",
            "blue": "blue",
            "purple": "purple",
            "black": None,
        }

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            ("  ", None),
            (" RED ", "red"),
            ("Yellow", "yellow"),
            ("black", "black"),
        ],
    )
    def test_normalize_color(self, value, expected):
        assert normalize_color(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            ("red", "text-color-red"),
            (" purple ", "text-color-purple"),
            ("black", None),
        ],
    )
    def test_color_class(self, value, expected):
        assert color_class(value) == expected

    def test_unknown_or_non_string_colors_are_rejected(self):
        with pytest.raises(ValueError, match="不支持的幕布文本颜色"):
            normalize_color("orange")
        with pytest.raises(TypeError, match="字符串"):
            normalize_color(12)

    def test_generic_renderer_combines_basic_styles(self):
        assert style_text(
            "Use", bold=True, italic=True, underline=True, color="purple"
        ) == '<span class="bold italic underline text-color-purple">Use</span>'
        assert style_text("Black", bold=True, color="black") == (
            '<span class="bold">Black</span>'
        )
        assert style_text("Plain") == "Plain"

        with pytest.raises(ValueError, match="不支持的幕布文本颜色"):
            style_text("Unknown", color="orange")


class TestHeadingMethods:
    def test_heading_registry_matches_verified_template(self):
        assert dict(HEADING_STYLES) == {
            1: HeadingStyle(heading=1, bold=True, color="red"),
            2: HeadingStyle(heading=2, bold=True),
            3: HeadingStyle(heading=3, bold=True),
            4: HeadingStyle(heading=0, bold=True),
        }

    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            (1, '<span class="bold text-color-red">Title</span>'),
            (2, '<span class="bold">Title</span>'),
            (3, '<span class="bold">Title</span>'),
            (4, '<span class="bold">Title</span>'),
        ],
    )
    def test_style_heading_formats_each_semantic_level(self, level, expected):
        client = object.__new__(MubuClient)

        if level == 1:
            # Omitting level preserves the original one-level API behavior.
            assert client.style_heading("Title") == expected
        else:
            assert client.style_heading("Title", level=level) == expected

    @pytest.mark.parametrize("level", [0, 5, True, "1"])
    def test_invalid_heading_levels_fail_before_client_calls(self, level):
        client = mock.Mock()
        with pytest.raises(MubuError, match="标题级别"):
            style_heading(client, "Title", level=level)
        with pytest.raises(MubuError, match="标题级别"):
            append_headings(client, "doc", "Title", level=level)
        with pytest.raises(MubuError, match="标题级别"):
            set_headings(client, "doc", "Title", level=level)
        client.assert_not_called()

    def test_append_defaults_to_level_one_and_can_select_another_level(self):
        nodes = []
        client = _append_test_client(nodes)

        ids = client.append_headings("doc", "Default")
        assert ids == ["node000001"]
        assert nodes[-1]["heading"] == 1
        assert nodes[-1]["text"] == '<span class="bold text-color-red">Default</span>'

        ids = client.append_headings("doc", "Fourth", level=4)
        assert ids == ["node000002"]
        assert nodes[-1]["heading"] == 0
        assert nodes[-1]["text"] == '<span class="bold">Fourth</span>'

    def test_append_always_appends_even_when_called_with_same_heading(self):
        nodes = []
        client = _append_test_client(nodes)

        first = client.append_headings("doc", "Repeat")
        second = client.append_headings("doc", "Repeat")

        assert first == ["node000001"]
        assert second == ["node000002"]
        assert [node["text"] for node in nodes] == [
            '<span class="bold text-color-red">Repeat</span>',
            '<span class="bold text-color-red">Repeat</span>',
        ]
        assert client.save_doc.call_count == 2

    def test_set_skips_compliant_nodes_without_writing(self):
        compliant_node = {
            "id": "existing",
            "text": '<span class="bold text-color-red">Same</span>',
            "heading": 1,
            "modified": 123,
            "children": [],
        }
        client = object.__new__(MubuClient)
        client._get_doc_raw = mock.Mock(
            return_value={
                "definition": json.dumps({"nodes": [compliant_node]}),
                "baseVersion": 3,
            }
        )
        client.save_doc = mock.Mock()

        result = client.set_headings("doc", ["Same"])

        assert result == {"updated": 0, "created": []}
        client.save_doc.assert_not_called()


def _append_test_client(nodes):
    """用本地列表模拟服务端保存，验证追加的精确语义，不访问网络。"""
    client = object.__new__(MubuClient)
    ids = iter(["node000001", "node000002", "node000003"])
    client._gen_node_id = lambda: next(ids)
    client._get_doc_raw = lambda _doc_id: {
        "definition": json.dumps({"nodes": nodes}),
        "baseVersion": len(nodes),
    }

    def record_save(_doc_id, *, events, version):
        assert version == len(nodes)
        nodes.append(events[0]["created"][0]["node"])

    client.save_doc = mock.Mock(side_effect=record_save)
    return client


class TestHeadingCli:
    @pytest.mark.parametrize(
        ("command", "handler_name", "extra_args", "return_value", "expected_level"),
        [
            ("append", "append_headings", ["--level", "2"], [], 2),
            (
                "set-headings",
                "set_headings",
                ["--level", "3"],
                {"updated": 0, "created": []},
                3,
            ),
            ("append", "append_headings", [], [], 1),
        ],
    )
    def test_cli_passes_selected_or_default_level(
        self, monkeypatch, command, handler_name, extra_args, return_value, expected_level
    ):
        monkeypatch.setattr(
            sys,
            "argv",
            ["mubu_api.py", command, "doc-id", "Topic", *extra_args],
        )
        with mock.patch("mubu.cli.MubuClient") as client_type, mock.patch(
            f"mubu.commands.{handler_name}", return_value=return_value
        ) as handler:
            cli.main()

        handler.assert_called_once_with(
            client_type.return_value, "doc-id", ["Topic"], level=expected_level
        )
