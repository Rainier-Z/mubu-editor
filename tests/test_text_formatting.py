import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from mubu.commands import _highlight, _mask
from mubu.convert import highlight_html, mask_html


class TestTextFormattingConverters(unittest.TestCase):
    def test_mask_escapes_text_and_wraps_it_in_the_native_mask_span(self):
        self.assertEqual(
            mask_html('A & <B> "C"'),
            '<span class="mask">A &amp; &lt;B&gt; &quot;C&quot;</span>',
        )

    def test_highlight_escapes_text_and_uses_the_confirmed_yellow_color(self):
        self.assertEqual(
            highlight_html("first\nsecond & <third>"),
            '<span class="highlight-yellow">first\nsecond &amp; &lt;third&gt;</span>',
        )

    def test_highlight_rejects_unverified_colors(self):
        for color in ("green", "orange"):
            with self.subTest(color=color), self.assertRaisesRegex(
                    ValueError, "不支持的幕布高亮颜色"):
                highlight_html("text", color=color)


class TestTextFormattingCommands(unittest.TestCase):
    def test_mask_rewrites_the_requested_node_with_native_html(self):
        calls = []
        client = SimpleNamespace(
            set_node_fields=lambda doc_id, path, fields: calls.append(
                (doc_id, path, fields)) or True)
        args = SimpleNamespace(doc_id="doc-1", path="0.1", text="Mask <me>")

        with redirect_stdout(StringIO()) as output:
            _mask(client, args)

        self.assertEqual(calls, [
            ("doc-1", ["nodes", 0, "children", 1], {
                "text": '<span class="mask">Mask &lt;me&gt;</span>',
            }),
        ])
        self.assertIn("已改写为挖空", output.getvalue())

    def test_highlight_rewrites_the_requested_node_with_native_html(self):
        calls = []
        client = SimpleNamespace(
            set_node_fields=lambda doc_id, path, fields: calls.append(
                (doc_id, path, fields)) or True)
        args = SimpleNamespace(
            doc_id="doc-1", path="nodes,0", text="Focus & learn", color="yellow")

        with redirect_stdout(StringIO()) as output:
            _highlight(client, args)

        self.assertEqual(calls, [
            ("doc-1", ["nodes", 0], {
                "text": '<span class="highlight-yellow">Focus &amp; learn</span>',
            }),
        ])
        self.assertIn("已改写为黄色高亮", output.getvalue())


if __name__ == "__main__":
    unittest.main()
