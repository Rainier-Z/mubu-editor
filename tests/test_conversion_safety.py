import re
import sys
import unittest
from html import unescape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import mubu.convert as convert
from mubu.convert import (
    _safe_filename,
    link_html,
    markdown_table_to_rows,
    normalize_node,
    rows_to_markdown_table,
)


class TestMarkdownTableRoundtrip(unittest.TestCase):
    def test_roundtrip_preserves_special_cell_content(self):
        rows = [
            ["header 1", "header 2", "header 3", "header 4"],
            ["  padded  ", r"back\slash", "left|right", "line 1\nline 2 &amp; &#124;"],
        ]

        markdown = rows_to_markdown_table(rows)

        self.assertEqual(markdown_table_to_rows(markdown), rows)
        self.assertEqual(len(markdown.splitlines()), 3)

    def test_rejects_rows_with_inconsistent_column_counts(self):
        markdown = "| A | B |\n| --- | --- |\n| only one |"

        with self.assertRaises(ValueError):
            markdown_table_to_rows(markdown)


class TestSafeFilename(unittest.TestCase):
    def test_converts_empty_dot_and_windows_unsafe_names(self):
        cases = {
            "": "untitled",
            "   ": "untitled",
            ".": "untitled",
            "..": "untitled",
            "CON": "_CON",
            "con.txt": "_con.txt",
            "LPT9.log": "_LPT9.log",
            "name. ": "name",
            "name...": "name",
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(_safe_filename(source), expected)


class TestUniqueFilename(unittest.TestCase):
    def test_disambiguates_cleaned_collisions_with_used_names_or_stable_ids(self):
        used_names = set()

        first = getattr(convert, "_unique_filename", None)
        self.assertIsNotNone(first, "_unique_filename helper is required")

        self.assertEqual(first("a/b", used_names=used_names), "a_b")
        self.assertEqual(first("a:b", used_names=used_names), "a_b_2")
        self.assertEqual(first("a/b", stable_id="doc:1"), "a_b--doc_1")
        self.assertEqual(first("a:b", stable_id="doc/2"), "a_b--doc_2")


class TestSafeLinks(unittest.TestCase):
    def test_rejects_unsafe_schemes_and_malformed_web_urls(self):
        for url in (
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "https:///missing-host",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    link_html("label", url)

    def test_emits_the_native_content_link_template_for_allowed_urls(self):
        cases = (
            "https://example.com/?a=1&b=2",
            "http://example.com",
            "mailto:person@example.com",
            "../notes/topic#part",
        )
        pattern = re.compile(
            r'<a class="content-link" data-id="([A-Za-z0-9]{10})" '
            r'target="_blank" spellcheck="false" rel="noreferrer" href="(.*?)">'
            r'<span class="content-link-text">(.*?)</span></a>'
        )

        for url in cases:
            with self.subTest(url=url):
                match = pattern.fullmatch(link_html("label <b>", url))
                self.assertIsNotNone(match)
                element_id, href, visible = match.groups()
                self.assertEqual(len(element_id), 10)
                self.assertEqual(unescape(href), url)
                self.assertEqual(visible, "label &lt;b&gt;")


class TestNormalizeNodeProtocolFields(unittest.TestCase):
    def test_preserves_protocol_and_future_fields_and_defaults_recursively(self):
        future_value = {"revision": 17}
        node = {
            "text": "root",
            "highlight": "",
            "color": "",
            "futureField": future_value,
            "children": [
                {
                    "text": "child",
                    "highlight": "yellow",
                    "color": 3,
                    "futureField": "preserve-me",
                    "children": [
                        {"text": "grandchild", "children": []},
                    ],
                },
            ],
        }

        normalize_node(node)

        self.assertEqual(node["highlight"], "")
        self.assertEqual(node["color"], "")
        self.assertIs(node["futureField"], future_value)
        child = node["children"][0]
        self.assertEqual(child["highlight"], "yellow")
        self.assertEqual(child["color"], 3)
        self.assertEqual(child["futureField"], "preserve-me")
        grandchild = child["children"][0]
        self.assertEqual(grandchild.get("highlight"), "")
        self.assertEqual(grandchild.get("color"), "")
