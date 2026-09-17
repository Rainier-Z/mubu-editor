import json
import re
import sys
import unittest
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from mubu.convert import formula_html, mention_html, node_mention_html, style_text


class RichTextConverterTests(unittest.TestCase):
    def test_style_text_escapes_markup_and_quotes_inside_the_generated_span(self):
        self.assertEqual(
            style_text('A & <B> "C" </span>', bold=True),
            '<span class="bold">A &amp; &lt;B&gt; &quot;C&quot; '
            '&lt;/span&gt;</span>',
        )

    def test_formula_uses_native_span_without_markdown_wrappers(self):
        self.assertEqual(
            formula_html("x^2", inline=True),
            '<span class="formula" data-raw="x%5E2" '
            'contenteditable="false">\u200b\u200b\u200b</span>',
        )

    def test_formula_block_uses_the_same_native_span_shape(self):
        self.assertEqual(
            formula_html("a + b", inline=False),
            '<span class="formula" data-raw="a%20%2B%20b" '
            'contenteditable="false">\u200b\u200b\u200b</span>',
        )

    def test_formula_url_encodes_arbitrary_latex_without_raw_attribute_injection(self):
        markup = formula_html('x" & <y>')
        self.assertNotIn('data-raw="x"', markup)
        self.assertIn('data-raw="x%22%20%26%20%3Cy%3E"', markup)
        self.assertEqual(markup.count("\u200b"), 3)

    def test_mention_contains_matching_random_id_and_encoded_protocol_payload(self):
        markup = mention_html("doc/42", '显示 & <名> "quoted"')
        match = re.fullmatch(
            r'<a class="mention mm-iconfont" target="_blank" rel="noreferrer" '
            r'spellcheck="false" contenteditable="false" '
            r'href="(.*?)" id="mention-([A-Za-z0-9_-]+)" '
            r'data-mention="(.*?)" data-type="1" data-token="(.*?)">(.*?)</a>',
            markup,
        )
        self.assertIsNotNone(match)
        href, element_id, encoded_payload, token, visible = match.groups()
        self.assertEqual(href, "https://mubu.com/docdoc/42")
        self.assertEqual(token, "doc/42")
        self.assertEqual(visible, "显示 &amp; &lt;名&gt; &quot;quoted&quot;")
        payload = json.loads(unquote(encoded_payload))
        self.assertEqual(payload, {
            "type": 2,
            "id": element_id,
            "mentionType": 1,
            "mentionNotify": False,
            "token": "doc/42",
            "link": "https://mubu.com/docdoc/42",
            "textEn": "",
            "text": '显示 & <名> "quoted"',
            "docId": "doc/42",
        })

    def test_mention_ids_are_unpredictable_safe_and_not_reused(self):
        first = mention_html("doc-1", "One")
        second = mention_html("doc-1", "One")
        first_id = re.search(r'id="mention-([A-Za-z0-9_-]+)"', first).group(1)
        second_id = re.search(r'id="mention-([A-Za-z0-9_-]+)"', second).group(1)
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(len(first_id), 10)
        self.assertRegex(first_id, r"^[A-Za-z0-9]{10}$")

    def test_node_mention_contains_escaped_display_text_and_encoded_payload(self):
        markup = node_mention_html("doc-7", "node/9", '节点 & <文本> "x"')
        match = re.fullmatch(
            r'<span class="node-mention" id="([A-Za-z0-9_-]+)" '
            r'spellcheck="false" contenteditable="false" data-doc="(.*?)" '
            r'data-node="(.*?)" data-text="(.*?)"><span>(.*?)</span></span>',
            markup,
        )
        self.assertIsNotNone(match)
        element_id, doc_id, node_id, encoded_text, visible = match.groups()
        self.assertRegex(element_id, r"^[A-Za-z0-9]{10}$")
        self.assertEqual(doc_id, "doc-7")
        self.assertEqual(node_id, "node/9")
        self.assertEqual(visible, "节点 &amp; &lt;文本&gt; &quot;x&quot;")
        self.assertEqual(
            json.loads(unquote(encoded_text)),
            [{"type": 1, "text": '节点 & <文本> "x"'}],
        )


if __name__ == "__main__":
    unittest.main()
