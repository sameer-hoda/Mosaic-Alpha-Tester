"""test_chat_exclusions.py — operator chat exclusions (AI agent chats).

Covers the Instinct-style exclusion: exact JIDs from config/exclude.txt or
NEMBU_EXCLUDE_CHATS, and case-insensitive name substrings. The excluded chat
must be dropped by the shared Q&A helper.
"""

import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="nembu-exclude-test-")
os.environ["NEMBU_HOME"] = _TMP
os.environ["MOSAIC_HOME"] = _TMP
os.environ["NEMBU_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["MOSAIC_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["STORE_DIR"] = os.path.join(_TMP, "store")
os.environ["MESSAGES_DB_PATH"] = os.path.join(_TMP, "messages.db")
os.environ["ENGINE_DB_PATH"] = os.path.join(_TMP, "engine.db")
os.environ["WHATSAPP_DB_PATH"] = os.path.join(_TMP, "whatsapp.db")
os.environ.pop("NEMBU_EXCLUDE_CHATS", None)
os.environ.pop("MOSAIC_EXCLUDE_CHATS", None)

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import agent_config  # noqa: E402
import qa  # noqa: E402


def _write_dbs():
    os.makedirs(os.path.dirname(os.environ["MESSAGES_DB_PATH"]), exist_ok=True)
    con = sqlite3.connect(os.environ["MESSAGES_DB_PATH"])
    con.execute(
        "CREATE TABLE chats (jid TEXT, name TEXT, last_message_time TIMESTAMP)"
    )
    con.executemany(
        "INSERT INTO chats VALUES (?,?,NULL)",
        [
            ("instinct@lid", "Instinct AI"),
            ("agent@lid", "DM 0633"),
            ("work@g.us", "Perf okr"),
        ],
    )
    con.commit()
    con.close()

    con = sqlite3.connect(os.environ["WHATSAPP_DB_PATH"])
    con.execute(
        "CREATE TABLE whatsmeow_chat_settings (chat_jid TEXT, archived INTEGER)"
    )
    con.execute(
        "CREATE TABLE whatsmeow_contacts (their_jid TEXT, full_name TEXT, "
        "first_name TEXT, push_name TEXT, business_name TEXT)"
    )
    con.execute("CREATE TABLE whatsmeow_lid_map (lid TEXT, pn TEXT)")
    con.commit()
    con.close()


class ChatExclusionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _write_dbs()
        os.makedirs(agent_config.config_dir(), exist_ok=True)
        with open(agent_config.exclude_path(), "w", encoding="utf-8") as fh:
            fh.write("# test exclusions\nInstinct\nagent@lid\n")
        agent_config._EXCLUDE_CACHE["key"] = None

    def test_name_substring_match(self):
        self.assertTrue(agent_config.is_excluded_chat("x@g.us", "Instinct AI"))
        self.assertTrue(agent_config.is_excluded_chat("x@g.us", "instinct"))

    def test_exact_jid_match(self):
        self.assertTrue(agent_config.is_excluded_chat("agent@lid", "DM 0633"))

    def test_non_excluded_chat(self):
        self.assertFalse(agent_config.is_excluded_chat("work@g.us", "Perf okr"))

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"NEMBU_EXCLUDE_CHATS": "work@g.us"}):
            self.assertTrue(agent_config.is_excluded_chat("work@g.us", "Perf okr"))

    def test_qa_helper_drops_excluded_chats(self):
        self.assertTrue(qa._excluded_chat("instinct@lid"))
        self.assertTrue(qa._excluded_chat("agent@lid"))
        self.assertFalse(qa._excluded_chat("work@g.us"))


if __name__ == "__main__":
    unittest.main()
