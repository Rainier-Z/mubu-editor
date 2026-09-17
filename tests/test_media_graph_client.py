import copy
import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from mubu import tos
from mubu.client import MubuClient
from mubu.config import MubuError


def png_header(width=320, height=240):
    signature = b"\x89PNG\r\n\x1a\n"
    data = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", 13) + data
    return signature + chunk + struct.pack(">I", zlib.crc32(data) & 0xFFFFFFFF)


class MediaGraphClientTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = mock.patch.object(MubuClient, "_load_env_file", lambda self, path=None: None)
        self.token_patch = mock.patch.object(MubuClient, "_load_token", lambda self: False)
        self.env_patch.start()
        self.token_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(self.token_patch.stop)

    def make_client(self, nodes):
        client = MubuClient(phone="test", password="test")
        client.member_id = "member-1"
        store = {"nodes": copy.deepcopy(nodes), "version": 7, "events": []}

        def get_doc_raw(_doc_id):
            return {
                "baseVersion": store["version"],
                "definition": json.dumps({"nodes": copy.deepcopy(store["nodes"])}),
            }

        def write_events(_doc_id, events, expected_nodes, version):
            self.assertEqual(version, store["version"])
            store["nodes"] = MubuClient._apply_node_events(store["nodes"], events)
            self.assertEqual(
                [MubuClient._node_semantics(node) for node in store["nodes"]],
                [MubuClient._node_semantics(node) for node in expected_nodes],
            )
            store["events"].append(copy.deepcopy(events))
            store["version"] += 1
            return get_doc_raw(_doc_id)

        client._get_doc_raw = get_doc_raw
        client._write_events_verified = write_events
        client._test_store = store
        return client

    def test_move_node_reorders_a_sibling_and_verifies_final_identity(self):
        client = self.make_client([
            {"id": "A", "text": "A", "children": []},
            {"id": "B", "text": "B", "children": []},
            {"id": "C", "text": "C", "children": []},
        ])

        self.assertTrue(client.move_node("D1", ["nodes", 0], 2))

        self.assertEqual(
            [node["id"] for node in client._test_store["nodes"]], ["B", "C", "A"])
        entry = client._test_store["events"][0][0]["changed"][0]
        self.assertEqual(entry["original"]["node"]["id"], "A")
        self.assertEqual(entry["original"]["path"], ["nodes", 0])
        self.assertEqual(entry["changed"]["path"], ["nodes", 2])

    def test_move_node_moves_a_nested_node_to_another_parent(self):
        client = self.make_client([
            {"id": "A", "text": "A", "children": [
                {"id": "A1", "text": "A1", "children": []},
            ]},
            {"id": "B", "text": "B", "children": []},
        ])

        self.assertTrue(client.move_node(
            "D1", ["nodes", 0, "children", 0], 0, ["nodes", 1]))

        self.assertEqual(client._test_store["nodes"][0]["children"], [])
        self.assertEqual(
            client._test_store["nodes"][1]["children"][0]["id"], "A1")
        entry = client._test_store["events"][0][0]["changed"][0]
        self.assertEqual(entry["original"]["parentId"], "A")
        self.assertEqual(entry["changed"]["parentId"], "B")

    def test_move_node_without_destination_parent_promotes_nested_node_to_root(self):
        client = self.make_client([
            {"id": "A", "text": "A", "children": [
                {"id": "A1", "text": "A1", "children": []},
            ]},
            {"id": "B", "text": "B", "children": []},
        ])

        self.assertTrue(client.move_node(
            "D1", ["nodes", 0, "children", 0], 2))

        self.assertEqual(
            [node["id"] for node in client._test_store["nodes"]], ["A", "B", "A1"])
        self.assertEqual(client._test_store["nodes"][0]["children"], [])
        entry = client._test_store["events"][0][0]["changed"][0]
        self.assertEqual(entry["original"]["parentId"], "A")
        self.assertIsNone(entry["changed"]["parentId"])
        self.assertEqual(entry["changed"]["path"], ["nodes", 2])

    def test_move_node_rejects_invalid_or_ambiguous_targets_without_writing(self):
        client = self.make_client([
            {"id": "A", "text": "A", "children": [
                {"id": "A1", "text": "A1", "children": []},
            ]},
        ])
        cases = [
            (["nodes", 0], -1, None),
            (["nodes", 0], 1, None),
            (["nodes", 9], 0, None),
            (["nodes", 0], 0, ["nodes", 0]),
        ]
        for path, index, parent in cases:
            with self.subTest(path=path, index=index, parent=parent):
                with self.assertRaises(MubuError):
                    client.move_node("D1", path, index, parent)
        self.assertEqual(client._test_store["events"], [])

    def test_attach_image_fetches_sts_uploads_to_tos_and_registers_image(self):
        client = self.make_client([{"id": "A", "text": "Picture", "children": []}])
        client.user_id = "user-1"
        sts_response = {
            "credentials": {
                "accessKeyId": "access-key",
                "secretAccessKey": "secret-key",
                "sessionToken": "session-token",
            }
        }
        request = mock.Mock(return_value=sts_response)
        client._request = request
        data = png_header(17, 23)
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "sample.png"
            path.write_bytes(data)

            with mock.patch.object(
                tos, "build_object_key", return_value="document_image/user-1/image-1.png"
            ) as build_key, mock.patch.object(
                tos, "put_object", return_value=(200, '"etag-1"', "")
            ) as put_object:
                self.assertTrue(client.attach_image(
                    "D1", ["nodes", 0], path.relative_to(Path.cwd()), width=11))

        request.assert_has_calls([
            mock.call("GET", "/tos/sts"),
            mock.call(
                "POST",
                "/document/sync_recently_used_img",
                json={"imageIdList": ["document_image/user-1/image-1.png"]},
            ),
        ])
        self.assertEqual(request.call_count, 2)
        build_key.assert_called_once_with("user-1", "png")
        put_object.assert_called_once_with(
            "document_image/user-1/image-1.png",
            data,
            sts_response["credentials"],
            "image/png",
        )
        updated = client._test_store["events"][0][0]["updated"][0]["updated"]
        self.assertEqual(updated["id"], "A")
        self.assertEqual(updated["images"], [{
            "id": updated["images"][0]["id"],
            "uri": "document_image/user-1/image-1.png",
            "ow": 17,
            "oh": 23,
            "w": 11,
        }])
        self.assertEqual(updated["imageLayouts"], [{"count": 1}])

    def test_attach_image_preserves_existing_images_and_rejects_bad_options(self):
        client = self.make_client([{
            "id": "A", "text": "Picture", "children": [],
            "images": [{"id": "old", "uri": "old.jpg", "ow": 1, "oh": 2, "w": 1}],
        }])
        client._request = mock.Mock()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "sample.png"
            path.write_bytes(png_header())
            for width in (0, -1, True, "400"):
                with self.subTest(width=width), self.assertRaises(MubuError):
                    client.attach_image(
                        "D1", ["nodes", 0], path.relative_to(Path.cwd()), width=width)
        self.assertEqual(client._test_store["events"], [])

    def test_attach_image_rejects_path_outside_current_directory(self):
        client = self.make_client([{"id": "A", "text": "Picture", "children": []}])
        client._request = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outside.png"
            path.write_bytes(png_header())
            with self.assertRaises(MubuError) as error:
                client.attach_image("D1", ["nodes", 0], path)
        self.assertIn("拒绝读取绝对路径", str(error.exception))
        client._request.assert_not_called()

    def test_attach_image_rejects_incomplete_tos_credentials(self):
        client = self.make_client([{"id": "A", "text": "Picture", "children": []}])
        client.user_id = "user-1"
        client._request = mock.Mock(return_value={"credentials": {}})
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "sample.png"
            path.write_bytes(png_header())
            with self.assertRaises(MubuError):
                client.attach_image(
                    "D1", ["nodes", 0], path.relative_to(Path.cwd()))
        self.assertEqual(client._test_store["events"], [])

    def test_attach_image_registration_failure_is_not_reported_as_success(self):
        client = self.make_client([{"id": "A", "text": "Picture", "children": []}])
        client.user_id = "user-1"
        calls = []

        def request(method, endpoint, **kwargs):
            calls.append((method, endpoint))
            if endpoint == "/tos/sts":
                return {"credentials": {
                    "accessKeyId": "access-key",
                    "secretAccessKey": "secret-key",
                    "sessionToken": "session-token",
                }}
            raise MubuError("注册失败")

        client._request = request
        data = png_header(17, 23)
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "sample.png"
            path.write_bytes(data)
            with mock.patch.object(
                tos, "put_object", return_value=(200, '"etag-1"', "")
            ):
                with self.assertRaises(MubuError, msg="registration failure must abort node write"):
                    client.attach_image(
                        "D1", ["nodes", 0], path.relative_to(Path.cwd()))
        self.assertEqual(client._test_store["events"], [])

    def test_link_nodes_appends_a_link_line_to_the_source_node(self):
        client = self.make_client([
            {"id": "A", "text": "from", "children": []},
            {"id": "B", "text": "to", "children": []},
        ])

        self.assertTrue(client.link_nodes("D1", ["nodes", 0], ["nodes", 1]))

        updated = client._test_store["events"][0][0]["updated"][0]["updated"]
        line = updated["linkLines"][0]
        self.assertEqual(updated["id"], "A")
        self.assertEqual(line["fromNodeId"], "A")
        self.assertEqual(line["toNodeId"], "B")
        self.assertEqual(line["fromAttachment"], {"side": "right", "t": 0.5})
        self.assertEqual(line["toAttachment"], {"side": "right", "t": 0.5})
        self.assertEqual(line["controlPoints"], [[0, 0], [0, 0]])

    def test_link_nodes_is_idempotent_and_rejects_invalid_side_or_paths(self):
        client = self.make_client([
            {"id": "A", "text": "from", "children": [], "linkLines": [{
                "id": "L", "fromNodeId": "A", "toNodeId": "B",
            }]},
            {"id": "B", "text": "to", "children": []},
        ])
        self.assertFalse(client.link_nodes("D1", ["nodes", 0], ["nodes", 1]))
        self.assertEqual(client._test_store["events"], [])
        for side in ("center", "", None, 1):
            with self.subTest(side=side), self.assertRaises(MubuError):
                client.link_nodes("D1", ["nodes", 0], ["nodes", 1], side=side)
        with self.assertRaises(MubuError):
            client.link_nodes("D1", ["nodes", 0], ["nodes", 0])


if __name__ == "__main__":
    unittest.main()
