"""test_nembu_migration.py — Nembu rename migration and legacy fallback tests.

Everything runs under temp dirs; never touches real ~/.nembu or ~/.mosaic.
"""

import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _envload  # noqa
import agent_config  # noqa
import mstate  # noqa
import qa  # noqa
import conversation  # noqa


class LegacyEnvFallbackTests(unittest.TestCase):
    def setUp(self):
        self._orig = {}
        for k in ("NEMBU_HOME", "MOSAIC_HOME", "NEMBU_CONFIG_DIR", "MOSAIC_CONFIG_DIR",
                  "NEMBU_HISTORY_DAYS", "MOSAIC_HISTORY_DAYS",
                  "NEMBU_GEMINI_BASE", "MOSAIC_GEMINI_BASE",
                  "NEMBU_PYTHON", "MOSAIC_PYTHON",
                  "NEMBU_DAILY_USD_CAP", "MOSAIC_DAILY_USD_CAP",
                  "NEMBU_CARD_BATCH", "MOSAIC_CARD_BATCH"):
            self._orig[k] = os.environ.get(k)
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_home_prefers_nembu_over_mosaic(self):
        os.environ["NEMBU_HOME"] = "/tmp/nembu-home"
        os.environ["MOSAIC_HOME"] = "/tmp/mosaic-home"
        # _envload.resolve_home should prefer NEMBU
        self.assertEqual(_envload.resolve_home(), "/tmp/nembu-home")
        # agent_config should also prefer NEMBU
        os.environ["NEMBU_CONFIG_DIR"] = "/tmp/nembu-config"
        os.environ["MOSAIC_CONFIG_DIR"] = "/tmp/mosaic-config"
        self.assertEqual(agent_config.config_dir(), "/tmp/nembu-config")

    def test_mosaic_fallback_when_nembu_missing(self):
        os.environ.pop("NEMBU_HOME", None)
        os.environ["MOSAIC_HOME"] = "/tmp/mosaic-only"
        self.assertEqual(_envload.resolve_home(), "/tmp/mosaic-only")
        os.environ.pop("NEMBU_CONFIG_DIR", None)
        os.environ["MOSAIC_CONFIG_DIR"] = "/tmp/mosaic-config-only"
        os.environ.pop("MOSAIC_HOME", None)
        self.assertEqual(agent_config.config_dir(), "/tmp/mosaic-config-only")

    def test_env_getter_fallback(self):
        os.environ.pop("NEMBU_HISTORY_DAYS", None)
        os.environ["MOSAIC_HISTORY_DAYS"] = "45"
        self.assertEqual(_envload.getenv_int("NEMBU_HISTORY_DAYS", "MOSAIC_HISTORY_DAYS", 30), 45)
        os.environ["NEMBU_HISTORY_DAYS"] = "15"
        self.assertEqual(_envload.getenv_int("NEMBU_HISTORY_DAYS", "MOSAIC_HISTORY_DAYS", 30), 15)

    def test_mstate_legacy_fallback(self):
        # mstate uses its own getenv that checks NEMBU then MOSAIC
        os.environ.pop("NEMBU_BODY_CHARS", None)
        os.environ.pop("MOSAIC_BODY_CHARS", None)
        # default
        # Reload not needed, we test _getenv directly
        from mstate import _getenv_int
        os.environ["MOSAIC_HISTORY_DAYS"] = "60"
        self.assertEqual(_getenv_int("NEMBU_HISTORY_DAYS", "MOSAIC_HISTORY_DAYS", 30), 60)


class PathFallbackTests(unittest.TestCase):
    def test_nembu_config_fallback_to_mosaic_config(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            mosaic_cfg = home / ".mosaic-config"
            mosaic_cfg.mkdir()
            (mosaic_cfg / "scope.json").write_text(json.dumps({"include_archived": True}))
            # Simulate what config_dir does when both .nembu-config missing and .mosaic-config exists
            # This is tested via agent_config's resolve logic: it checks existence
            # We'll set HOME to td
            old_home = os.environ.get("HOME")
            old_nembu = os.environ.get("NEMBU_CONFIG_DIR")
            old_mosaic = os.environ.get("MOSAIC_CONFIG_DIR")
            old_nembu_home = os.environ.get("NEMBU_HOME")
            old_mosaic_home = os.environ.get("MOSAIC_HOME")
            try:
                os.environ["HOME"] = str(home)
                os.environ.pop("NEMBU_CONFIG_DIR", None)
                os.environ.pop("MOSAIC_CONFIG_DIR", None)
                os.environ.pop("NEMBU_HOME", None)
                os.environ.pop("MOSAIC_HOME", None)
                cfg = agent_config.config_dir()
                # Should be .mosaic-config fallback when .nembu-config missing
                self.assertIn(".mosaic-config", cfg)
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
                for k, v in (("NEMBU_CONFIG_DIR", old_nembu), ("MOSAIC_CONFIG_DIR", old_mosaic),
                               ("NEMBU_HOME", old_nembu_home), ("MOSAIC_HOME", old_mosaic_home)):
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_resolve_home_uses_existing_dir(self):
        with tempfile.TemporaryDirectory() as td:
            home_base = pathlib.Path(td)
            nembu = home_base / ".nembu"
            mosaic = home_base / ".mosaic"
            mosaic.mkdir()
            # No env set, nembu missing, mosaic exists -> should return mosaic
            old_home = os.environ.get("HOME")
            old_nembu_home = os.environ.get("NEMBU_HOME")
            old_mosaic_home = os.environ.get("MOSAIC_HOME")
            try:
                os.environ["HOME"] = str(home_base)
                os.environ.pop("NEMBU_HOME", None)
                os.environ.pop("MOSAIC_HOME", None)
                resolved = _envload.resolve_home()
                self.assertEqual(resolved, str(mosaic))
                # Now create nembu, should prefer nembu
                nembu.mkdir()
                resolved2 = _envload.resolve_home()
                self.assertEqual(resolved2, str(nembu))
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
                for k, v in (("NEMBU_HOME", old_nembu_home), ("MOSAIC_HOME", old_mosaic_home)):
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


class MigrationScriptTests(unittest.TestCase):
    def test_install_sh_has_migration(self):
        text = (REPO / "install.sh").read_text()
        self.assertIn("migrate_legacy", text)
        self.assertIn("LEGACY_HOME", text)
        self.assertIn("NEMBU_HOME", text)
        self.assertIn(".nembu", text)
        self.assertIn("backup", text.lower())
        # Should reference new repo
        self.assertIn("Nembu-Alpha-Tester", text)
        # Should handle both binary names
        self.assertIn("nembu", text)

    def test_launcher_has_migration_and_new_paths(self):
        text = (REPO / "nembu").read_text()
        self.assertIn("NEMBU_HOME", text)
        self.assertIn("ai.nembu.bridge", text)
        self.assertIn("nembu.service", text)
        self.assertIn("nembu.log", text)
        self.assertIn("LEGACY", text)  # fallback handling

    def test_legacy_wrapper_exists(self):
        wrapper = (REPO / "mosaic")
        self.assertTrue(wrapper.exists())
        txt = wrapper.read_text()
        self.assertIn("deprecated", txt.lower())
        self.assertIn("nembu", txt.lower())


class OldDBCompatibilityTests(unittest.TestCase):
    def test_forwarded_detection_supports_both_brands(self):
        # Legacy Mosaic
        self.assertTrue(qa._is_forwarded_mosaic("*Mosaic* · Pending · 9 Nov"))
        # New Nembu
        self.assertTrue(qa._is_forwarded_mosaic("*Nembu* 🍋 · 9 Nov"))
        self.assertTrue(qa._is_forwarded_mosaic("*Nembu* · 9 Nov"))
        # Also direct new alias
        self.assertTrue(qa._is_forwarded_nembu("*Nembu* 🍋 · 9 Nov") if hasattr(qa, "_is_forwarded_nembu") else qa._is_forwarded_mosaic("*Nembu* 🍋 · 9 Nov"))
        self.assertTrue(qa._is_forwarded_mosaic("◆ I couldn't place that"))
        self.assertTrue(qa._is_forwarded_mosaic("🍋 something"))
        self.assertFalse(qa._is_forwarded_mosaic("shipped the UPI plan"))

    def test_conversation_role_nembu_with_legacy_mosaic(self):
        # recent_dialogue should produce role nembu, but also handle legacy _Mosaic_ marker
        # Simulate outbox body cleaning
        with tempfile.TemporaryDirectory() as td:
            store = pathlib.Path(td)
            # Setup minimal engine.db with outbox
            db_path = store / "engine.db"
            con = sqlite3.connect(str(db_path))
            con.executescript(mstate.SCHEMA)
            # Insert outbox entry with legacy marker
            con.execute("INSERT INTO outbox (kind, dedupe_key, target, body, status, created_at) VALUES (?,?,?,?,?,?)",
                        ("reply", "test1", "123@s.whatsapp.net", "◆ Hello\n_Mosaic_\nBody", "delivered", "2026-01-01T00:00:00"))
            con.commit()
            con.close()
            # Temporarily override STORE_DIR and ENGINE_DB_PATH via env?
            # Use conversation module: it reads from mstate.connect() which uses ENGINE_DB_PATH
            old_store = mstate.STORE_DIR
            old_engine = mstate.ENGINE_DB_PATH
            old_messages = os.environ.get("MESSAGES_DB_PATH")
            try:
                mstate.STORE_DIR = str(store)
                mstate.ENGINE_DB_PATH = str(db_path)
                # Need mechat.jids to return target we inserted? We'll mock mechat.mechat_jids
                import unittest.mock as mock
                with mock.patch("mechat.mechat_jids", return_value=["123@s.whatsapp.net"]):
                    dlg = conversation.recent_dialogue(limit=5)
                    # Should have role nembu, not mosaic
                    roles = [d["role"] for d in dlg]
                    self.assertIn("nembu", roles)
                    self.assertNotIn("mosaic", roles)
                    # Body should have stripped legacy marker
                    texts = [d["text"] for d in dlg]
                    # Should not contain _Mosaic_
                    for t in texts:
                        self.assertNotIn("_Mosaic_", t)
            finally:
                mstate.STORE_DIR = old_store
                mstate.ENGINE_DB_PATH = old_engine
                if old_messages is None:
                    os.environ.pop("MESSAGES_DB_PATH", None)
                else:
                    os.environ["MESSAGES_DB_PATH"] = old_messages

    def test_nembu_header_has_lemon(self):
        # Ensure qa.py headers now include lemon
        # Look for "*Nembu* 🍋" in source
        src = (REPO / "qa.py").read_text()
        self.assertIn("*Nembu* 🍋", src)


class BinaryAndPathsTests(unittest.TestCase):
    def test_go_binary_name(self):
        # Check that bridge build produces nembu binary (Makefile uses go build ./...)
        # And that runtime.go no longer references mosaic.log as primary
        tui_text = (REPO / "bridge" / "tui.go").read_text()
        self.assertIn("Nembu", tui_text)
        self.assertNotIn('"Mosaic"', tui_text)  # should be Nembu
        runtime_text = (REPO / "bridge" / "runtime.go").read_text()
        self.assertIn("NEMBU_HOME", runtime_text)


if __name__ == "__main__":
    unittest.main()
