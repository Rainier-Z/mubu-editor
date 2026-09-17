"""Sensitive configuration file permission regression tests.

All integration files live under pytest's temporary directory.  The Windows
tests inspect the security descriptor through Win32 APIs rather than relying
on localized account names or shell output.
"""

import ctypes
import os
import stat
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import mubu.client as client_module
import mubu.config as config


def _windows_file_acl(path):
    """Return (protected, {sid: (mask, ace_flags)}) for a Windows file."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    advapi32.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)
    )
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = (
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD
    )
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = (
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)
    )
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p

    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    dacl_security_information = 0x00000004 | 0x00000001
    status = advapi32.GetNamedSecurityInfoW(
        str(path), 1, dacl_security_information,
        ctypes.byref(owner), None, ctypes.byref(dacl), None,
        ctypes.byref(descriptor),
    )
    if status:
        raise OSError(status, "GetNamedSecurityInfoW failed")
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        class AclSizeInformation(ctypes.Structure):
            _fields_ = [
                ("AceCount", wintypes.DWORD),
                ("AclBytesInUse", wintypes.DWORD),
                ("AclBytesFree", wintypes.DWORD),
            ]

        acl_info = AclSizeInformation()
        if not advapi32.GetAclInformation(
            dacl, ctypes.byref(acl_info), ctypes.sizeof(acl_info), 2
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        entries = {}
        for index in range(acl_info.AceCount):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            address = ace.value
            ace_type = ctypes.c_ubyte.from_address(address).value
            ace_flags = ctypes.c_ubyte.from_address(address + 1).value
            if ace_type != 0:  # ACCESS_ALLOWED_ACE_TYPE
                entries[f"unexpected-ace-{index}"] = (ace_type, ace_flags)
                continue
            mask = wintypes.DWORD.from_address(address + 4).value
            sid_pointer = ctypes.c_void_p(address + 8)
            sid_text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                entries[sid_text.value] = (mask, ace_flags)
            finally:
                kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        return bool(control.value & 0x1000), entries, _sid_text(advapi32, kernel32, owner)
    finally:
        kernel32.LocalFree(descriptor)


def _sid_text(advapi32, kernel32, sid):
    sid_text = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return sid_text.value
    finally:
        kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))


def _assert_private_file(path):
    if os.name == "nt":
        protected, entries, owner_sid = _windows_file_acl(path)
        assert protected, "DACL must be protected from inherited ACEs"
        assert set(entries) == {"S-1-5-18", owner_sid}
        assert owner_sid != "S-1-5-18"
        assert set(entries.values()) == {(0x1F01FF, 0)}
    else:
        assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600


def test_sensitive_permission_function_restricts_a_temp_file(tmp_path):
    path = tmp_path / "secret.txt"
    path.write_text("temporary test marker", encoding="utf-8")

    config._secure_file_permissions(path)

    _assert_private_file(path)


def test_lock_failure_is_reported_before_entering_critical_section(tmp_path, monkeypatch):
    target = tmp_path / "target.json"
    entered = False

    def fail_closed(_path):
        raise PermissionError("test ACL failure")

    monkeypatch.setattr(config, "_secure_file_permissions", fail_closed, raising=False)
    with pytest.raises(PermissionError, match="test ACL failure"):
        with config._token_file_lock(target):
            entered = True

    assert not entered


def test_atomic_replacement_secures_temp_before_write_and_final_file(tmp_path, monkeypatch):
    destination = tmp_path / "secret.json"
    secured_paths = []
    secure = getattr(client_module, "_secure_file_permissions", None)
    assert secure is not None, "client must use the config permission function"

    def record_and_secure(path):
        secure(path)
        path = Path(path)
        assert path.exists()
        _assert_private_file(path)
        secured_paths.append(path)

    monkeypatch.setattr(client_module, "_secure_file_permissions", record_and_secure)
    client_module._atomic_replace_text(destination, '{"temporary": true}')

    assert len(secured_paths) >= 2
    assert secured_paths[0] != destination
    assert secured_paths[0].suffix == ".tmp"
    assert secured_paths[-1] == destination
    assert destination.read_text(encoding="utf-8") == '{"temporary": true}'


def test_atomic_replacement_closes_temp_descriptor_when_acl_setup_fails(tmp_path, monkeypatch):
    destination = tmp_path / "secret.json"
    descriptors = []
    mkstemp = client_module.tempfile.mkstemp

    def capture_descriptor(*args, **kwargs):
        descriptor, name = mkstemp(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor, name

    def fail_acl(_path):
        raise PermissionError("test ACL failure")

    monkeypatch.setattr(client_module.tempfile, "mkstemp", capture_descriptor)
    monkeypatch.setattr(client_module, "_secure_file_permissions", fail_acl)
    with pytest.raises(PermissionError, match="test ACL failure"):
        client_module._atomic_replace_text(destination, "test-only marker")

    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert not list(tmp_path.glob(".secret.json.*.tmp"))


def test_token_trash_lock_and_env_paths_are_secured_on_temporary_files(tmp_path, monkeypatch):
    token_path = tmp_path / "token.json"
    trash_path = tmp_path / "trash.json"
    env_path = tmp_path / "env.mubu"
    env_path.write_text("MUBU_PHONE=test-only-value\n", encoding="utf-8")

    monkeypatch.setattr(client_module, "TOKEN_FILE", token_path)
    monkeypatch.setattr(client_module, "TRASH_FILE", trash_path)
    client = client_module.MubuClient.__new__(client_module.MubuClient)
    client.token = "test-token-only"
    client.user_id = "test-user"
    client.username = "temporary test"
    client.member_id = "test-member"
    client.expires_at = 0

    client._save_token()
    client._save_trash({"test-only-id": {"type": "doc"}})
    client._load_env_file(env_path)

    for path in (token_path, trash_path, env_path,
                 token_path.with_name(token_path.name + ".lock"),
                 trash_path.with_name(trash_path.name + ".lock")):
        _assert_private_file(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod error path")
def test_posix_chmod_failure_is_reported(tmp_path, monkeypatch):
    path = tmp_path / "secret.txt"
    path.write_text("temporary test marker", encoding="utf-8")

    def fail_chmod(*_args, **_kwargs):
        raise PermissionError("test chmod failure")

    monkeypatch.setattr(config.os, "chmod", fail_chmod)
    with pytest.raises(PermissionError, match="test chmod failure"):
        config._secure_file_permissions(path)
