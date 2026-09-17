"""Regression tests for Mubu configuration source and endpoint safety."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import mubu.config as config


class ConfigSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def resolve_paths(self, workspace, environ):
        return config._resolve_config_paths(workspace, self.home, environ)

    def test_explicit_file_path_wins_when_missing(self):
        workspace = self.root / "workspace"
        legacy_paths = (
            ("MUBU_TOKEN_FILE", Path(".mubu_token"), 1),
            ("MUBU_TRASH_FILE", Path(".workbuddy") / ".mubu_trash.json", 2),
            ("MUBU_ENV_FILE", Path(".workbuddy") / ".env.mubu", 3),
        )
        config_dir = self.root / "config"

        for env_name, legacy_path, resolved_index in legacy_paths:
            with self.subTest(env_name=env_name):
                legacy = self.home / legacy_path
                legacy.parent.mkdir(parents=True, exist_ok=True)
                legacy.touch()
                explicit = self.root / "chosen" / env_name
                paths = self.resolve_paths(workspace, {
                    "MUBU_CONFIG_DIR": str(config_dir),
                    env_name: str(explicit),
                })

                self.assertEqual(paths[resolved_index], explicit)

    def test_explicit_config_dir_keeps_all_defaults_under_that_root(self):
        (self.home / ".mubu_token").touch()
        legacy_dir = self.home / ".workbuddy"
        legacy_dir.mkdir()
        (legacy_dir / ".env.mubu").touch()
        (legacy_dir / ".mubu_trash.json").touch()
        explicit_root = self.root / "new-config"

        config_dir, token_file, trash_file, env_file = self.resolve_paths(
            self.root / "workspace", {"MUBU_CONFIG_DIR": str(explicit_root)}
        )

        self.assertEqual(config_dir, explicit_root)
        self.assertEqual(token_file, explicit_root / ".mubu_token")
        self.assertEqual(trash_file, explicit_root / ".mubu_trash.json")
        self.assertEqual(env_file, explicit_root / ".env.mubu")

    def test_project_config_source_does_not_fall_back_per_file_to_legacy(self):
        workspace = self.root / "workspace"
        project_config = workspace / "config"
        project_config.mkdir(parents=True)
        # Any known project config file selects the project source as a group.
        (project_config / ".env.mubu").touch()
        (self.home / ".mubu_token").touch()
        legacy_dir = self.home / ".workbuddy"
        legacy_dir.mkdir()
        (legacy_dir / ".mubu_trash.json").touch()

        config_dir, token_file, trash_file, env_file = self.resolve_paths(workspace, {})

        self.assertEqual(config_dir, project_config)
        self.assertEqual(token_file, project_config / ".mubu_token")
        self.assertEqual(trash_file, project_config / ".mubu_trash.json")
        self.assertEqual(env_file, project_config / ".env.mubu")

    def test_legacy_files_select_the_legacy_source_as_a_group(self):
        workspace = self.root / "workspace"
        (workspace / "config").mkdir(parents=True)
        legacy_dir = self.home / ".workbuddy"
        legacy_dir.mkdir()
        (self.home / ".mubu_token").touch()
        (legacy_dir / ".env.mubu").touch()
        (legacy_dir / ".mubu_trash.json").touch()

        config_dir, token_file, trash_file, env_file = self.resolve_paths(workspace, {})

        self.assertEqual(config_dir, legacy_dir)
        self.assertEqual(token_file, self.home / ".mubu_token")
        self.assertEqual(trash_file, legacy_dir / ".mubu_trash.json")
        self.assertEqual(env_file, legacy_dir / ".env.mubu")

    def test_base_url_rejects_unsafe_schemes_authority_and_suffixes(self):
        unsafe_urls = (
            "http://api2.mubu.com/v3/api",
            "https://user@api2.mubu.com/v3/api",
            "https://api2.mubu.com:8443/v3/api",
            "https://api2.mubu.com:invalid/v3/api",
            "https://api2.mubu.com/v3/api?next=https://other.example",
            "https://api2.mubu.com/v3/api#fragment",
        )
        for url in unsafe_urls:
            with self.subTest(url=url), patch.dict(os.environ, {"MUBU_BASE_URL": url}):
                with self.assertLogs(config.logger, level="WARNING") as captured:
                    self.assertEqual(config._resolve_base_url(), config.DEFAULT_BASE_URL)
                self.assertNotIn("user@", " ".join(captured.output))

    def test_base_url_accepts_allowed_https_hosts_and_default_port(self):
        allowed_urls = (
            "https://api2.mubu.com/v3/api/",
            "https://api.mubu.com/v3/api",
            "https://mubu.com:443/v3/api",
        )
        for url in allowed_urls:
            with self.subTest(url=url), patch.dict(os.environ, {"MUBU_BASE_URL": url}):
                self.assertEqual(config._resolve_base_url(), url.rstrip("/"))


if __name__ == "__main__":
    unittest.main()
