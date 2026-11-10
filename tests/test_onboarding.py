"""test_onboarding.py — first-run journey tests with no real account.

Covers the installer/launcher contract, key-probe failure without a live key,
the archived-scope preference, the Me-chat welcome gate, and the .env loader.
Everything runs under a temp directory; nothing touches ~/.nembu or ~/.mosaic.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _envload  # noqa: E402
import agent_config  # noqa: E402
import mechat  # noqa: E402


class EnvLoadTests(unittest.TestCase):
    def test_load_basic_quotes_comments_and_precedence(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, ".env")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write('# comment\n\nA=1\nB="two words"\nC=three # trailing\n')
            os.environ.pop("A", None)
            os.environ.pop("B", None)
            os.environ.pop("C", None)
            self.assertTrue(_envload.load(p))
            self.assertEqual(os.environ["A"], "1")
            self.assertEqual(os.environ["B"], "two words")
            self.assertEqual(os.environ["C"], "three")
            # Existing environment wins unless override=True.
            os.environ["A"] = "existing"
            _envload.load(p)
            self.assertEqual(os.environ["A"], "existing")
            _envload.load(p, override=True)
            self.assertEqual(os.environ["A"], "1")
            os.environ.pop("A", None)

    def test_load_missing_file(self):
        self.assertFalse(_envload.load("/nonexistent/nembu.env"))


class ScopeChoiceTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.cfg = pathlib.Path(self._td.name) / "config"
        self._old_env = {
            k: os.environ.get(k) for k in ("NEMBU_CONFIG_DIR", "MOSAIC_CONFIG_DIR", "QA_INCLUDE_ARCHIVED")
        }
        os.environ["NEMBU_CONFIG_DIR"] = str(self.cfg)
        os.environ.pop("MOSAIC_CONFIG_DIR", None)
        os.environ.pop("QA_INCLUDE_ARCHIVED", None)

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._td.cleanup()

    def test_default_is_active_only(self):
        self.assertFalse(agent_config.include_archived())

    def test_onboarding_choice_is_read(self):
        self.cfg.mkdir(parents=True)
        (self.cfg / "scope.json").write_text(
            json.dumps({"include_archived": True}), encoding="utf-8")
        self.assertTrue(agent_config.include_archived())

    def test_env_override_wins_both_ways(self):
        self.cfg.mkdir(parents=True)
        (self.cfg / "scope.json").write_text(
            json.dumps({"include_archived": True}), encoding="utf-8")
        os.environ["QA_INCLUDE_ARCHIVED"] = "0"
        self.assertFalse(agent_config.include_archived())
        os.environ["QA_INCLUDE_ARCHIVED"] = "1"
        (self.cfg / "scope.json").write_text(
            json.dumps({"include_archived": False}), encoding="utf-8")
        self.assertTrue(agent_config.include_archived())


class WelcomeGateTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._old_store = mechat.STORE_DIR
        self._old_file = mechat.MECHAT_FILE
        mechat.STORE_DIR = pathlib.Path(self._td.name)
        mechat.MECHAT_FILE = mechat.STORE_DIR / "mechat.json"

    def tearDown(self):
        mechat.STORE_DIR = self._old_store
        mechat.MECHAT_FILE = self._old_file
        self._td.cleanup()

    def test_welcome_text_uses_synced_counts(self):
        (mechat.STORE_DIR / "sync_state.json").write_text(
            json.dumps({"chats": 42, "messages": 1234}), encoding="utf-8")
        text = mechat._welcome_text()
        self.assertIn("42 chats", text)
        self.assertIn("1,234 messages", text)
        self.assertIn("last 30 days", text)

    def test_no_me_chat_never_sends(self):
        with mock.patch.object(mechat, "get_mechat_jid", return_value=""):
            self.assertEqual(mechat.welcome_once(), "no-mechat")

    def test_welcome_is_sent_once(self):
        mechat.persist("me@s.whatsapp.net", "verified", "")
        with mock.patch.object(mechat, "get_mechat_jid",
                               return_value="me@s.whatsapp.net"), \
                mock.patch.object(mechat, "_send_via_bridge", return_value=True):
            self.assertEqual(mechat.welcome_once(), "welcomed")
            self.assertTrue(mechat.load_persisted()["welcomed"])
            self.assertEqual(mechat.welcome_once(), "already-welcomed")

    def test_failed_send_does_not_mark_welcomed(self):
        mechat.persist("me@s.whatsapp.net", "verified", "")
        with mock.patch.object(mechat, "get_mechat_jid",
                               return_value="me@s.whatsapp.net"), \
                mock.patch.object(mechat, "_send_via_bridge", return_value=False):
            self.assertEqual(mechat.welcome_once(), "send-failed")
            self.assertFalse(mechat.load_persisted()["welcomed"])


class EarlyMessageTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._old_store = mechat.STORE_DIR
        self._old_file = mechat.MECHAT_FILE
        mechat.STORE_DIR = pathlib.Path(self._td.name)
        mechat.MECHAT_FILE = mechat.STORE_DIR / "mechat.json"
        mechat.persist("me@s.whatsapp.net", "verified", "")

    def tearDown(self):
        mechat.STORE_DIR = self._old_store
        mechat.MECHAT_FILE = self._old_file
        self._td.cleanup()

    def test_early_text_is_short_and_actionable(self):
        text = mechat._early_welcome_text()
        self.assertIn("Me chat", text)
        self.assertIn("syncing", text)
        self.assertIn("esc", text)
        self.assertLess(len(text), 260)

    def test_howto_text_lists_every_example(self):
        text = mechat._howto_text()
        for example in (
            "what did I miss today?",
            "what's pending with <person>?",
            "what did I get done this week?",
            "top 10",
            "mute <chat> 7d",
            "watch <chat>",
            "send",
        ):
            self.assertIn(example, text)

    def test_early_welcome_is_sent_once(self):
        with mock.patch.object(mechat, "get_mechat_jid",
                               return_value="me@s.whatsapp.net"), \
                mock.patch.object(mechat, "_send_via_bridge", return_value=True) as send:
            self.assertEqual(mechat.early_welcome_once(), "welcomed-early")
            self.assertTrue(mechat.load_persisted()["welcomed_early"])
            self.assertEqual(mechat.early_welcome_once(), "already-welcomed")
        self.assertEqual(send.call_count, 1)

    def test_legacy_welcomed_blocks_upgrade_early_welcome(self):
        mechat.persist("me@s.whatsapp.net", "verified", "", welcomed=True)
        with mock.patch.object(mechat, "get_mechat_jid",
                               return_value="me@s.whatsapp.net"), \
                mock.patch.object(mechat, "_send_via_bridge", return_value=True) as send:
            self.assertEqual(mechat.early_welcome_once(), "already-welcomed")
        send.assert_not_called()

    def test_early_welcome_no_mechat_and_failure(self):
        with mock.patch.object(mechat, "get_mechat_jid", return_value=""):
            self.assertEqual(mechat.early_welcome_once(), "no-mechat")
        with mock.patch.object(mechat, "get_mechat_jid",
                               return_value="me@s.whatsapp.net"), \
                mock.patch.object(mechat, "_send_via_bridge", return_value=False):
            self.assertEqual(mechat.early_welcome_once(), "send-failed")
        self.assertFalse(mechat.load_persisted()["welcomed_early"])

    def test_howto_is_sent_once(self):
        with mock.patch.object(mechat, "get_mechat_jid",
                               return_value="me@s.whatsapp.net"), \
                mock.patch.object(mechat, "_send_via_bridge", return_value=True) as send:
            self.assertEqual(mechat.howto_once(), "sent")
            self.assertTrue(mechat.load_persisted()["howto_sent"])
            self.assertEqual(mechat.howto_once(), "already-sent")
        self.assertEqual(send.call_count, 1)

    def test_howto_no_mechat_and_failure(self):
        with mock.patch.object(mechat, "get_mechat_jid", return_value=""):
            self.assertEqual(mechat.howto_once(), "no-mechat")
        with mock.patch.object(mechat, "get_mechat_jid",
                               return_value="me@s.whatsapp.net"), \
                mock.patch.object(mechat, "_send_via_bridge", return_value=False):
            self.assertEqual(mechat.howto_once(), "send-failed")
        self.assertFalse(mechat.load_persisted()["howto_sent"])


class _ProbeHandler(BaseHTTPRequestHandler):
    status = 400

    def do_POST(self):  # noqa: N802
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b'{"error":{"message":"fake"}}')

    def log_message(self, *args):
        pass


def _run(cmd, env, cwd=REPO, timeout=90):
    return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                          text=True, timeout=timeout)


class PreflightProbeTests(unittest.TestCase):
    def test_failed_probe_uses_local_server_and_fails_cleanly(self):
        server = HTTPServer(("127.0.0.1", 0), _ProbeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as home:
                env = os.environ.copy()
                env["NEMBU_HOME"] = home
                env["GEMINI_API_KEY"] = "AIza" + "x" * 30
                env["GEMINI_MODEL"] = "fake-model"
                env["NEMBU_GEMINI_BASE"] = (
                    f"http://127.0.0.1:{server.server_port}/v1beta/models")
                out = _run([sys.executable, "scripts/preflight.py", "--probe"], env)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("PREFLIGHT FAILED", out.stdout)
        self.assertIn("Gemini", out.stdout)

    def test_fast_preflight_passes_without_a_key(self):
        if (REPO / ".env").exists():
            self.skipTest("repo .env present; cannot simulate a keyless machine")
        with tempfile.TemporaryDirectory() as home:
            env = os.environ.copy()
            env["NEMBU_HOME"] = home
            env.pop("GEMINI_API_KEY", None)
            out = _run([sys.executable, "scripts/preflight.py"], env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("PREFLIGHT OK", out.stdout)


def _fake_bin(td):
    d = pathlib.Path(td) / "fakebin"
    d.mkdir()
    uname = d / "uname"
    uname.write_text("#!/bin/sh\necho Linux\n")
    uname.chmod(0o755)
    systemctl = d / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)
    return d


def _launcher_env(home, fakebin):
    env = os.environ.copy()
    env["HOME"] = home
    env["NEMBU_HOME"] = os.path.join(home, ".nembu")
    env["MOSAIC_HOME"] = os.path.join(home, ".mosaic")
    env["PATH"] = str(fakebin) + ":" + env.get("PATH", "")
    for k in ("STORE_DIR", "NEMBU_PYTHON", "MOSAIC_PYTHON", "PY_SCRIPTS_DIR"):
        env.pop(k, None)
    return env


def _install_layout(home):
    root = pathlib.Path(home) / ".nembu"
    (root / "app" / "bin").mkdir(parents=True)
    (root / "app" / "install.sh").write_text("#!/bin/sh\nexit 0\n")
    (root / "python" / "bin").mkdir(parents=True)
    py = root / "python" / "bin" / "python3"
    py.write_text("#!/bin/sh\nexit 0\n")
    py.chmod(0o755)
    bridge = root / "app" / "bin" / "nembu"
    bridge.write_text("#!/bin/sh\nexit 0\n")
    bridge.chmod(0o755)
    # legacy symlink
    (root / "app" / "bin" / "mosaic").write_text("#!/bin/sh\nexit 0\n")
    (root / "app" / "bin" / "mosaic").chmod(0o755)
    (root / "store").mkdir(parents=True)
    (root / ".env").write_text('GEMINI_API_KEY=""\n')
    return root


class LauncherContractTests(unittest.TestCase):
    def test_scripts_parse(self):
        for script in ("install.sh", "nembu", "mosaic"):
            out = subprocess.run(["bash", "-n", str(REPO / script)],
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)

    def test_unknown_option_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as home:
            env = os.environ.copy()
            env["HOME"] = home
            env["NEMBU_HOME"] = os.path.join(home, ".mosaic")
            out = _run(["bash", str(REPO / "install.sh"), "--bogus"], env)
        self.assertEqual(out.returncode, 2)
        self.assertIn("Unknown option", out.stderr)

    def test_unknown_command(self):
        with tempfile.TemporaryDirectory() as home:
            env = _launcher_env(home, _fake_bin(home))
            out = _run(["bash", str(REPO / "nembu"), "bogus"], env)
        self.assertEqual(out.returncode, 1)
        self.assertIn("unknown command", out.stderr)

    def test_start_requires_install(self):
        with tempfile.TemporaryDirectory() as home:
            env = _launcher_env(home, _fake_bin(home))
            out = _run(["bash", str(REPO / "nembu"), "start"], env)
        self.assertEqual(out.returncode, 1)
        self.assertIn("not installed", out.stderr)

    def test_linux_start_stop_restart(self):
        with tempfile.TemporaryDirectory() as home:
            _install_layout(home)
            env = _launcher_env(home, _fake_bin(home))
            launcher = ["bash", str(REPO / "nembu")]
            for cmd in ("start", "stop", "restart"):
                out = _run(launcher + [cmd], env)
                self.assertEqual(out.returncode, 0, f"{cmd}: {out.stdout}{out.stderr}")
            unit = pathlib.Path(home) / ".config/systemd/user/nembu.service"
            self.assertTrue(unit.exists(), "systemd unit was not written")
            text = unit.read_text()
            self.assertIn("ExecStart=%h/.nembu/app/bin/nembu", text)
            self.assertIn("Restart=always", text)

    def test_uninstall_keeps_store_and_env(self):
        with tempfile.TemporaryDirectory() as home:
            root = _install_layout(home)
            env = _launcher_env(home, _fake_bin(home))
            out = _run(["bash", str(REPO / "nembu"), "uninstall"], env)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertFalse((root / "app").exists())
            self.assertFalse((root / "python").exists())
            self.assertFalse((root / "bin" / "nembu").exists())
            self.assertTrue((root / "store").exists(), "store must survive uninstall")
            self.assertTrue((root / ".env").exists(), ".env must survive uninstall")
            self.assertIn("Nembu removed", out.stdout)


if __name__ == "__main__":
    unittest.main()
