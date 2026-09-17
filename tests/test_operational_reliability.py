"""Operational reliability regressions using local fakes and temporary files only."""

import errno
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from mubu import client as client_module
from mubu import config as config_module
from mubu.client import MubuClient
from mubu.config import MubuError


class OperationalReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def make_client(self):
        with mock.patch.object(MubuClient, "_load_env_file", lambda self, path=None: None), \
                mock.patch.object(MubuClient, "_load_token", lambda self: False):
            client = MubuClient(phone="test", password="test")
        client.token = "test-token"
        client.ensure_valid_token = lambda: None
        return client

    def test_export_tree_uses_one_case_insensitive_namespace_for_sanitized_names(self):
        client = self.make_client()
        listing = {
            "folders": [
                {"id": "f1", "name": "A?B"},
                {"id": "f2", "name": "a_b"},
                {"id": "f3", "name": ".."},
            ],
            "documents": [
                {"id": "d1", "name": "A/B"},
                {"id": "d2", "name": "A:B"},
                {"id": "d3", "name": "a_b"},
                {"id": "d4", "name": ".."},
            ],
        }
        client.get_list = lambda folder_id: listing if folder_id == "0" else {
            "folders": [], "documents": [],
        }
        client.get_doc = lambda doc_id: {"node": {"text": doc_id}}

        stats = client.export_tree("0", str(self.root))

        self.assertEqual(stats, {"docs": 4, "folders": 3, "errors": 0})
        outputs = [path.relative_to(self.root).as_posix().casefold()
                   for path in self.root.rglob("*")]
        self.assertEqual(len(outputs), len(set(outputs)))
        self.assertTrue((self.root / "A_B.md").read_text(encoding="utf-8"))
        self.assertTrue((self.root / "A_B_2.md").exists())
        self.assertTrue((self.root / "a_b_2.md").exists())
        self.assertFalse(any(".." in part for output in outputs for part in output.split("/")))

    def test_export_tree_never_overwrites_existing_output(self):
        client = self.make_client()
        existing = self.root / "Keep.md"
        existing.write_text("user data", encoding="utf-8")
        client.get_list = lambda _folder_id: {
            "folders": [], "documents": [{"id": "d1", "name": "Keep"}],
        }
        client.get_doc = lambda _doc_id: {"node": {"text": "new export"}}

        stats = client.export_tree("0", str(self.root))

        self.assertEqual(stats, {"docs": 1, "folders": 0, "errors": 0})
        self.assertEqual(existing.read_text(encoding="utf-8"), "user data")
        self.assertTrue((self.root / "Keep_2.md").exists())

    def test_export_tree_rejects_final_paths_outside_resolved_output_root(self):
        client = self.make_client()
        output = self.root / "output"
        output.mkdir()
        client.get_list = lambda _folder_id: {
            "folders": [{"id": "f1", "name": "would be safe normally"}],
            "documents": [{"id": "d1", "name": "would be safe normally"}],
        }
        client.get_doc = lambda _doc_id: {"node": {"text": "synthetic"}}

        with mock.patch.object(client_module, "_unique_filename", return_value="..\\escape",
                               create=True):
            stats = client.export_tree("0", str(output))

        self.assertEqual(stats, {"docs": 0, "folders": 0, "errors": 2})
        self.assertFalse((self.root / "escape").exists())
        self.assertFalse((self.root / "escape.md").exists())

    def test_post_read_operation_retries_but_folder_create_does_not(self):
        client = self.make_client()
        success = mock.Mock(status_code=200)
        success.json.return_value = {"code": 0, "data": {"folders": [], "docs": []}}
        calls = {"read": 0, "write": 0}

        def read_request(*_args, **_kwargs):
            calls["read"] += 1
            if calls["read"] == 1:
                raise requests.exceptions.Timeout("response timed out")
            return success

        client._session.request = mock.Mock(side_effect=read_request)
        with mock.patch.object(client_module.time, "sleep"):
            client._request("POST", "/list/get", json={"folderId": "0"})
        self.assertEqual(calls["read"], 2)
        client._session.request.reset_mock()

        def write_request(*_args, **_kwargs):
            calls["write"] += 1
            raise requests.exceptions.ConnectionError("connection lost after send")

        client._session.request.side_effect = write_request
        with mock.patch.object(client_module.time, "sleep"):
            with self.assertRaises(MubuError) as raised:
                client.create_folder("New", "0")

        self.assertEqual(calls["write"], 1)
        self.assertIn("结果未知", str(raised.exception))

    def test_create_operations_report_unknown_when_success_response_has_no_id(self):
        client = self.make_client()
        client._request = mock.Mock(return_value={})

        for operation in (
            lambda: client.create_folder("Folder"),
            lambda: client.create_doc("Document"),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(MubuError) as raised:
                    operation()
                self.assertIn("结果未知", str(raised.exception))
        self.assertEqual(client._request.call_count, 2)

    def test_uncertain_changeset_is_confirmed_by_post_write_readback(self):
        client = self.make_client()
        client.member_id = "test-member-id"
        store = {"nodes": [{
            "id": "N1", "text": "before", "children": [],
            "highlight": "", "color": "",
            "note": "unchanged note", "futureField": {"keep": True},
        }]}
        reads = {"count": 0}

        def get_doc_raw(_doc_id):
            reads["count"] += 1
            return {"baseVersion": 3,
                    "definition": json.dumps({"nodes": store["nodes"]})}

        def request(_method, endpoint, **kwargs):
            if endpoint == "/colla/events":
                event = kwargs["json"]["events"][0]
                entry = event["updated"][0]
                path = entry["path"]
                siblings = store["nodes"]
                for offset in range(2, len(path), 2):
                    siblings = siblings[path[offset - 1]].setdefault("children", [])
                siblings[path[-1]].update(entry["updated"])
                error = MubuError("请求结果未知")
                error.result_unknown = True
                raise error
            self.fail(f"unexpected endpoint {endpoint}")

        client._get_doc_raw = get_doc_raw
        client._request = request

        changed = client.update_node_text("D1", ["nodes", 0], "after")

        self.assertTrue(changed)
        self.assertEqual(store["nodes"][0]["text"], "after")
        self.assertEqual(store["nodes"][0]["children"], [])
        self.assertEqual(store["nodes"][0]["highlight"], "")
        self.assertEqual(store["nodes"][0]["color"], "")
        self.assertEqual(store["nodes"][0]["note"], "unchanged note")
        self.assertEqual(store["nodes"][0]["futureField"], {"keep": True})
        self.assertGreaterEqual(reads["count"], 2)

    def test_sensitive_requests_disable_redirects_and_report_redirect_response(self):
        client = self.make_client()
        redirect = mock.Mock(status_code=302, headers={"Location": "http://attacker.invalid/"})
        client._session.request = mock.Mock(return_value=redirect)

        with self.assertRaises(MubuError) as raised:
            client._request("POST", "/list/get", json={"folderId": "0"})

        self.assertIn("重定向", str(raised.exception))
        self.assertIs(client._session.request.call_args.kwargs["allow_redirects"], False)

    def test_windows_lock_uses_msvcrt_byte_range_lock_and_unlock(self):
        fake_msvcrt = SimpleNamespace(
            LK_NBLCK=1, LK_UNLCK=2,
            locking=mock.Mock(side_effect=[None, None]),
        )
        lock_target = self.root / "token.json"

        with mock.patch.object(config_module, "fcntl", None), \
                mock.patch.object(config_module, "msvcrt", fake_msvcrt, create=True):
            with config_module._token_file_lock(lock_target):
                self.assertTrue(lock_target.with_name("token.json.lock").exists())

        self.assertEqual(fake_msvcrt.locking.call_count, 2)
        self.assertEqual(fake_msvcrt.locking.call_args_list[0].args[1:], (fake_msvcrt.LK_NBLCK, 1))
        self.assertEqual(fake_msvcrt.locking.call_args_list[1].args[1:], (fake_msvcrt.LK_UNLCK, 1))

    def test_windows_lock_times_out_instead_of_waiting_forever(self):
        busy = OSError(errno.EACCES, "lock is held")
        fake_msvcrt = SimpleNamespace(
            LK_NBLCK=1, LK_UNLCK=2,
            locking=mock.Mock(side_effect=busy),
        )
        lock_target = self.root / "token.json"
        clock = iter((0.0, 0.0, 1.0))

        with mock.patch.object(config_module, "fcntl", None), \
                mock.patch.object(config_module, "msvcrt", fake_msvcrt, create=True), \
                mock.patch.object(config_module, "WINDOWS_LOCK_TIMEOUT_SECONDS", 0.5), \
                mock.patch.object(config_module.time, "monotonic", side_effect=clock), \
                mock.patch.object(config_module.time, "sleep"):
            with self.assertRaisesRegex(MubuError, "获取配置文件锁超时"):
                with config_module._token_file_lock(lock_target):
                    self.fail("lock acquisition must time out before entering the critical section")

        self.assertGreaterEqual(fake_msvcrt.locking.call_count, 1)

    def test_token_atomic_write_uses_unique_temp_file_not_fixed_tmp_name(self):
        client = self.make_client()
        token_file = self.root / "token.json"
        stale_temp = self.root / "token.json.tmp"
        stale_temp.write_text("leave this alone", encoding="utf-8")
        client.token = "synthetic-token"
        client.user_id = "synthetic-user"

        with mock.patch.object(client_module, "TOKEN_FILE", token_file), \
                mock.patch.object(client_module, "_token_file_lock", lambda *_: nullcontext()):
            client._save_token()

        self.assertEqual(stale_temp.read_text(encoding="utf-8"), "leave this alone")
        self.assertEqual(json.loads(token_file.read_text(encoding="utf-8"))["token"],
                         "synthetic-token")
        self.assertEqual(sorted(path.name for path in self.root.glob("token.json*.tmp")),
                         [stale_temp.name])

    def test_trash_read_modify_write_holds_one_lock_across_read_and_write(self):
        client = self.make_client()
        trash_file = self.root / "trash.json"
        active = {"locked": False, "read": False, "write": False}

        @contextmanager
        def tracking_lock(_path=None):
            self.assertFalse(active["locked"])
            active["locked"] = True
            try:
                yield
            finally:
                active["locked"] = False

        def checked_read():
            self.assertTrue(active["locked"])
            active["read"] = True
            return {}

        def checked_write(trash):
            self.assertTrue(active["locked"])
            active["write"] = True
            trash_file.write_text(json.dumps(trash), encoding="utf-8")

        with mock.patch.object(client_module, "TRASH_FILE", trash_file), \
                mock.patch.object(client_module, "_token_file_lock", tracking_lock), \
                mock.patch.object(client, "_read_trash_unlocked", side_effect=checked_read,
                                  create=True), \
                mock.patch.object(client, "_write_trash_unlocked", side_effect=checked_write,
                                  create=True):
            client.trash_item("D1", "doc", name="Doc")

        self.assertTrue(active["read"])
        self.assertTrue(active["write"])
        self.assertFalse(active["locked"])
        self.assertEqual(json.loads(trash_file.read_text(encoding="utf-8"))["D1"]["name"],
                         "Doc")

    def test_trash_atomic_write_preserves_a_fixed_name_temp_file(self):
        client = self.make_client()
        trash_file = self.root / "trash.json"
        stale_temp = self.root / "trash.json.tmp"
        stale_temp.write_text("stale temp marker", encoding="utf-8")

        with mock.patch.object(client_module, "TRASH_FILE", trash_file), \
                mock.patch.object(client_module, "_token_file_lock", lambda *_: nullcontext()):
            client._save_trash({"D1": {"id": "D1", "type": "doc"}})

        self.assertEqual(stale_temp.read_text(encoding="utf-8"), "stale temp marker")
        self.assertEqual(json.loads(trash_file.read_text(encoding="utf-8"))["D1"]["id"], "D1")


if __name__ == "__main__":
    unittest.main()
