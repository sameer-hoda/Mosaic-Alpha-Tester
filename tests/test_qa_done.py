import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import datetime
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="nembu-done-test-")
os.environ["NEMBU_HOME"] = _TMP
os.environ["MOSAIC_HOME"] = _TMP
os.environ["NEMBU_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["MOSAIC_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["STORE_DIR"] = os.path.join(_TMP, "store")
os.environ["MESSAGES_DB_PATH"] = os.path.join(_TMP, "messages.db")
os.environ["ENGINE_DB_PATH"] = os.path.join(_TMP, "engine.db")
os.environ["WHATSAPP_DB_PATH"] = os.path.join(_TMP, "whatsapp.db")

import mstate  # noqa: E402
import qa  # noqa: E402
import qa_profiles  # noqa: E402


SCHEMA = """
CREATE TABLE messages (
  id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
  is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT,
  media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER,
  local_path TEXT, quoted_id TEXT, quoted_sender TEXT,
  PRIMARY KEY (id, chat_jid)
)
"""

ROWS = [
    # Group A: busy, but nothing from the user.
    ("a1", "111@g.us", "other", "chatter one", "2026-11-04 09:00:00", 0, ""),
    ("a2", "111@g.us", "other", "chatter two", "2026-11-04 09:01:00", 0, ""),
    ("a3", "111@g.us", "other", "chatter three", "2026-11-04 09:02:00", 0, ""),
    # Group B: two user actions, each with an outcome reply, plus noise.
    ("b1", "222@g.us", "me", "shipped the UPI plan", "2026-11-04 10:00:00", 1, ""),
    ("b2", "222@g.us", "rishab", "looks good, live now", "2026-11-04 10:05:00", 0, ""),
    ("b3", "222@g.us", "me", "reviewed the MoM", "2026-11-04 11:00:00", 1, ""),
    ("b4", "222@g.us", "other", "thanks", "2026-11-04 11:05:00", 0, ""),
    ("b5", "222@g.us", "me", "my suggestion is to junk this and redo the MoM plan", "2026-11-04 12:00:00", 1, ""),
    ("b1dup", "222@g.us", "me", "shipped the UPI plan", "2026-11-04 10:01:00", 1, ""),
    ("ack", "222@g.us", "me", "ok", "2026-11-04 10:02:00", 1, ""),
    ("url", "222@g.us", "me", "https://example.com/x", "2026-11-04 10:03:00", 1, ""),
    ("media", "222@g.us", "me", "", "2026-11-04 10:04:00", 1, "image"),
    ("voice", "222@g.us", "me", "", "2026-11-04 10:08:00", 1, "audio"),
    # ASK-01 / SUP-015: the deciding kill recommendation.
    ("B1C2D3E4F5A60718293B", "120363000000000003@g.us", "other",
     "my recommendation is to not invest any more in biometric and kill today; "
     "multiple cuts showed downstream funnel issues",
     "2026-11-06 15:57:05", 0, ""),
    # ASK-12 / SUP-008: the two plans a tomorrow question must cite.
    ("A1B2C3D4E5F60718293A", "120363000000000001@g.us", "111111111111112",
     "Airlines go live is tomorrow", "2026-11-09 13:38:12", 0, ""),
    ("C1D2E3F4A5B6071829", "55555555555555@lid", "Instinct",
     "John will visit tomorrow, Nov 10", "2026-11-09 16:22:49", 0, ""),
    # Group C: one user action.
    ("c1", "333@g.us", "me", "sent the merchant MBR invite", "2026-11-04 09:00:00", 1, ""),
    # A future plan that never uses the word "plan".
    ("f1", "444@g.us", "branding", "Airlines go live is tomorrow",
     "2026-11-04 13:38:00", 0, ""),
    # Search precision: one chat mentions "see", another "zorblat".
    ("s1", "666@g.us", "other", "please see the doc for details",
     "2026-11-04 09:00:00", 0, ""),
    ("s2", "666@g.us", "other", "zorblat appears in this note",
     "2026-11-04 09:10:00", 0, ""),
    # Direct message: must be in the Q&A scope.
    ("dm1", "999@lid", "me", "sent the deck to the client", "2026-11-04 10:00:00", 1, ""),
    # Quoted reply: the quoted text must be visible to the model.
    ("q1", "222@g.us", "other", "can you send the deck", "2026-11-04 09:50:00", 0, ""),
    ("q2", "222@g.us", "me", "yes done", "2026-11-04 09:55:00", 1, ""),
    # Question-only and forwarded Nembu/Mosaic summaries must never count as done.
    ("bq", "222@g.us", "me", "can you send the deck by friday?", "2026-11-04 10:06:00", 1, ""),
    ("bmos", "222@g.us", "me", "*Mosaic* · Pending · 9 Nov 17:45\n- old summary", "2026-11-04 10:07:00", 1, ""),
    # Outside the window: must never appear.
    ("old", "222@g.us", "me", "pre-window noise", "2026-10-01 09:00:00", 1, ""),
    ("future", "222@g.us", "me", "future noise", "2026-11-06 10:00:00", 1, ""),
]


class DonePathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = os.environ["MESSAGES_DB_PATH"]
        con = sqlite3.connect(cls.db)
        con.execute(SCHEMA)
        con.executemany(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp,"
            " is_from_me, media_type) VALUES (?,?,?,?,?,?,?)",
            ROWS,
        )
        con.execute("UPDATE messages SET quoted_id='q1' WHERE id='q2'")
        con.commit()
        con.close()
        mc = sqlite3.connect(cls.db)
        mc.execute("CREATE TABLE chats (jid TEXT, name TEXT, "
                   "last_message_time TIMESTAMP)")
        mc.executemany(
            "INSERT INTO chats VALUES (?,?,NULL)",
            [("120363000000000003@g.us", "UPI <> Central Growth"),
             ("120363000000000002@g.us", "Enterprise small group")])
        mc.commit()
        mc.close()
        wa = os.environ["WHATSAPP_DB_PATH"]
        con = sqlite3.connect(wa)
        con.execute(
            "CREATE TABLE whatsmeow_contacts (their_jid TEXT, full_name TEXT, "
            "first_name TEXT, push_name TEXT, business_name TEXT)"
        )
        con.execute(
            "CREATE TABLE whatsmeow_chat_settings (chat_jid TEXT, archived INTEGER)"
        )
        con.execute("CREATE TABLE whatsmeow_lid_map (lid TEXT, pn TEXT)")
        con.execute(
            "INSERT INTO whatsmeow_contacts VALUES "
            "('111122223333@lid','Raghav',NULL,NULL,NULL)"
        )
        con.execute(
            "INSERT INTO whatsmeow_contacts VALUES "
            "('222222222222222@lid','.',NULL,NULL,NULL)"
        )
        con.execute(
            "INSERT INTO whatsmeow_contacts VALUES "
            "('111111111111112@lid',NULL,NULL,'Shaan Mishra',NULL)"
        )
        con.commit()
        con.close()
        import datetime

        cls.start = datetime.datetime(2026, 11, 3)
        cls.end = datetime.datetime(2026, 11, 5)

    def test_scope_ranks_by_user_activity_not_chatter(self):
        chats, omitted = qa._done_scope(self.db, self.start, self.end, cap=40)
        self.assertEqual([c[0] for c in chats],
                         ["222@g.us", "333@g.us", "999@lid"])
        self.assertEqual(omitted, 0)
        self.assertNotIn("111@g.us", [c[0] for c in chats])

    def test_scope_cap_reports_omitted(self):
        chats, omitted = qa._done_scope(self.db, self.start, self.end, cap=1)
        self.assertEqual([c[0] for c in chats], ["222@g.us"])
        self.assertEqual(omitted, 2)

    def test_candidates_include_substantive_user_messages_without_action_verb(self):
        cands, meta = qa._done_candidates(
            [("222@g.us", 3)], self.db, self.start, self.end
        )
        texts = [c["text"] for c in cands]
        self.assertIn("shipped the UPI plan", texts)
        self.assertIn("reviewed the MoM", texts)
        self.assertIn("my suggestion is to junk this and redo the MoM plan", texts)
        self.assertNotIn("ok", texts)
        self.assertNotIn("https://example.com/x", texts)
        self.assertEqual(texts.count("shipped the UPI plan"), 1)
        self.assertTrue(all(c["group"] for c in cands))
        self.assertTrue(all(c["when"] for c in cands))
        self.assertEqual(meta["chats"], 1)

    def test_candidates_carry_the_next_reply_as_outcome(self):
        cands, _meta = qa._done_candidates(
            [("222@g.us", 3)], self.db, self.start, self.end
        )
        c = next(c for c in cands if c["text"] == "shipped the UPI plan")
        self.assertIn("looks good, live now", c["next"])

    def test_done_body_keeps_more_than_ten_items(self):
        done = [{"group": "G", "when": "Mon", "what": f"thing {i}", "quote": ""}
                for i in range(15)]
        body = qa._done_body(done)
        self.assertEqual(len(body.splitlines()), 15)
        self.assertIn("- *G*: thing 0 (Mon)", body)

    def test_done_body_dedupes_identical_what(self):
        done = [{"group": "G", "when": "Mon", "what": "same", "quote": ""},
                {"group": "H", "when": "Tue", "what": "same", "quote": ""}]
        body = qa._done_body(done)
        self.assertEqual(body.count("- "), 1)

    def test_body_limit_fits_a_week_digest(self):
        self.assertGreaterEqual(mstate.MAX_BODY_CHARS, 4000)

    def test_long_answer_is_not_split_below_the_limit(self):
        full = "*Nembu* 🍋 · 20 groups\n" + "".join(
            f"- bullet {i} " + ("x" * 240) + "\n" for i in range(5)
        )
        head, tail = qa._split_head(full, mstate.MAX_BODY_CHARS - 200)
        self.assertEqual(tail, "")
        self.assertEqual(head, full)

    def test_split_head_keeps_a_newline_boundary(self):
        full = ("z" * 60) + "\n" + ("y" * 300) + "\nlast line\n"
        head, tail = qa._split_head(full, 100)
        self.assertTrue(full.startswith(head))
        self.assertEqual(head + tail, full)
        self.assertFalse(head.endswith("y"))

    def test_window_tomorrow_uses_recent_history(self):
        import datetime
        text = "what's the plan for tomorrow"
        start, end = qa._window_from({"task_outline": text}, text)
        self.assertLessEqual(end, mstate.now_local() + datetime.timedelta(seconds=2))
        self.assertLess(start, end)

    def test_flow_messages_ignores_messages_after_end(self):
        context, _total, _media = qa._flow_messages(
            ["222@g.us"], self.start, end=self.end)
        self.assertNotIn("future noise", context)
        self.assertNotIn("pre-window noise", context)

    def test_flow_messages_shows_quoted_reply_and_media(self):
        context, _total, media = qa._flow_messages(
            ["222@g.us"], self.start, end=self.end)
        self.assertIn('↪ other: "can you send the deck"', context)
        self.assertIn("[photo]", context)
        self.assertGreaterEqual(media, 1)

    def test_load_marks_quoted_text(self):
        rows = qa._load("222@g.us")
        q = next(m for m in rows if m["id"] == "q2")
        self.assertEqual(q["quoted_text"], "can you send the deck")

    def test_why_question_is_a_decision(self):
        self.assertEqual(qa._ask_mode("why did they cancel"), "decision")

    def test_person_name_is_case_sensitive(self):
        self.assertEqual(qa._person_thread_name("pending with respect to the plan"), "")
        self.assertEqual(qa._person_thread_name("pending with Rahul"), "Rahul")

    def test_fts_search_finds_a_message(self):
        import qa_search
        hits = qa_search.search(
            os.environ["MESSAGES_DB_PATH"], os.environ.get("ENGINE_DB_PATH"),
            "UPI plan", self.start, self.end)
        self.assertTrue(any(h["msg_id"] == "b1" for h in hits))

    def test_remind_is_not_done(self):
        self.assertEqual(qa._ask_mode("who do I need to remind?"), "open_items")
        self.assertEqual(qa._ask_mode("what did I get done"), "done")

    def test_bare_summarize_asks_for_a_quote(self):
        import router
        out = router.route("summarize this")
        self.assertEqual(out.get("action"), "clarify")
        self.assertIn("Reply to the message", out.get("reply", ""))

    # ── SUP-001: done is evidence-only ──────────────────────────────────────
    def test_question_only_candidates_are_rejected(self):
        self.assertTrue(qa._is_question_only("can you send the deck by friday?"))
        self.assertTrue(qa._is_question_only("what is pending with Anaya"))
        self.assertFalse(qa._is_question_only("shipped the UPI plan"))
        self.assertFalse(qa._is_question_only("update this sheet today"))

    def test_forwarded_mosaic_summary_is_rejected(self):
        self.assertTrue(qa._is_forwarded_mosaic("*Mosaic* · Pending · 9 Nov"))
        self.assertTrue(qa._is_forwarded_mosaic("*Nembu* 🍋 · Pending · 9 Nov"))
        self.assertTrue(qa._is_forwarded_mosaic("*Nembu* · Pending · 9 Nov"))
        self.assertTrue(qa._is_forwarded_mosaic("◆ I couldn't place that"))
        self.assertTrue(qa._is_forwarded_mosaic("🍋 something"))
        self.assertFalse(qa._is_forwarded_mosaic("shipped the UPI plan"))

    def test_done_candidates_skip_questions_and_mosaic_forwards(self):
        cands, _meta = qa._done_candidates(
            [("222@g.us", 3)], self.db, self.start, self.end
        )
        texts = [c["text"] for c in cands]
        self.assertNotIn("can you send the deck by friday?", texts)
        self.assertNotIn("*Mosaic* · Pending · 9 Nov 17:45\n- old summary", texts)

    def test_done_quote_is_verbatim_from_the_users_own_message(self):
        text = "shipped the UPI plan to Raghav"
        self.assertEqual(qa._evidence_quote(text, "UPI plan"), "UPI plan")
        # A quote from someone else's reply must not survive as evidence.
        self.assertEqual(qa._evidence_quote(text, "looks good, live now"), text[:160])
        self.assertEqual(qa._evidence_quote(text, ""), text[:160])

    def test_forwarded_mosaic_is_not_loaded_as_evidence(self):
        rows = qa._load("222@g.us")
        self.assertFalse(any(r["text"].startswith("*Mosaic*") for r in rows))

    # ── SUP-002: DM names ──────────────────────────────────────────────────
    def test_dm_name_uses_saved_contact(self):
        qa._NAME_CACHE.clear()
        qa._CONTACT_CACHE = None
        self.assertEqual(qa._chat_name("111122223333@lid"), "Raghav")

    def test_dm_name_falls_back_to_last4_not_a_raw_lid(self):
        qa._NAME_CACHE.clear()
        qa._CONTACT_CACHE = None
        self.assertEqual(qa._chat_name("999988887777@lid"), "DM 7777")

    def test_eligibility_resolves_dm_names(self):
        resolved = qa_profiles._resolve_dm_names({
            "111122223333@lid": "111122223333@lid",
            "999988887777@lid": "999988887777@lid",
            "555@g.us": "555@g.us",
        })
        self.assertEqual(resolved["111122223333@lid"], "Raghav")
        self.assertEqual(resolved["999988887777@lid"], "DM 7777")
        self.assertEqual(resolved["555@g.us"], "555@g.us")

    # ── SUP-002 / ASK-16: source-line sender names ─────────────────────────
    def test_sender_label_resolves_contact_not_a_bare_lid(self):
        qa._CONTACT_CACHE = None
        self.assertEqual(qa._sender_label("111111111111112"),
                         "Shaan Mishra")
        self.assertFalse(qa._sender_label("111111111111112").isdigit())

    def test_future_context_uses_the_contact_sender_name(self):
        now = qa.mstate.now_local()
        context, _ids, _jids = qa._future_plan_context(
            "what's the plan for tomorrow?", now - datetime.timedelta(days=7),
            now, allowed=None, db_path=self.db)
        self.assertIn("Shaan Mishra", context)
        self.assertNotIn("111111111111112:", context)

    # ── ASK-19: group senders use the chat name, never DM <last4> ──────────
    def test_group_sender_uses_chat_name_not_dm_fallback(self):
        qa._NAME_CACHE.clear()
        qa._CONTACT_CACHE = None
        self.assertEqual(
            qa._sender_label("120363000000000003",
                             "120363000000000003@g.us"),
            "UPI <> Central Growth")
        self.assertEqual(
            qa._sender_label("120363000000000002",
                             "120363000000000002@g.us"),
            "Enterprise small group")
        # An unknown group participant is Someone, not a DM label.
        self.assertEqual(
            qa._sender_label("999988887777", "120363000000000003@g.us"),
            "Someone")
        # The DM fallback still applies to an actual DM.
        self.assertEqual(
            qa._sender_label("999988887777", "999988887777@lid"),
            "DM 7777")

    # ── SUP-003: done scope eligibility ────────────────────────────────────
    def test_scope_exclusions_cover_archived_muted_bot(self):
        fake = {
            "arch@g.us": ("A", False, "archived"),
            "mute@g.us": ("B", False, "muted"),
            "bot@g.us": ("C", False, "bot"),
            "quiet@g.us": ("D", False, "no activity in the last 30 days"),
            "ok@g.us": ("E", True, ""),
        }
        with patch.object(qa_profiles, "eligibility", return_value=fake):
            with patch.dict(os.environ, {"QA_INCLUDE_ARCHIVED": "0"}):
                self.assertEqual(qa._scope_excluded_jids(),
                                 {"arch@g.us", "mute@g.us", "bot@g.us"})
            with patch.dict(os.environ, {"QA_INCLUDE_ARCHIVED": "1"}):
                self.assertEqual(qa._scope_excluded_jids(),
                                 {"mute@g.us", "bot@g.us"})

    def test_done_scope_drops_excluded_chats(self):
        chats, omitted = qa._done_scope(
            self.db, self.start, self.end, cap=40, exclude={"333@g.us"})
        self.assertNotIn("333@g.us", [c[0] for c in chats])
        self.assertEqual(omitted, 0)

    # ── SUP-004: summary-first weekly done ─────────────────────────────────
    @staticmethod
    def _done_items():
        items = []
        for i in range(12):
            jid = ("222@g.us" if i % 3 == 0 else
                   "999@lid" if i % 3 == 1 else "888@lid")
            group = ("222@g.us" if i % 3 == 0 else
                     "DM 7777" if i % 3 == 1 else "Instinct")
            items.append({
                "jid": jid, "group": group, "what": f"item {i}",
                "when": "Mon", "ts": f"2026-11-04 10:{i:02d}:00",
                "confirmed": i == 0,
            })
        return items

    def test_done_split_work_personal_assistant(self):
        groups = qa._done_split(self._done_items())
        self.assertEqual(len(groups["Work"]), 4)
        self.assertEqual(len(groups["Personal"]), 4)
        self.assertEqual(len(groups["Assistant"]), 4)

    def test_done_highlights_are_seven_and_confirm_first(self):
        highlights = qa._done_highlights(self._done_items())
        self.assertEqual(len(highlights), 7)
        self.assertTrue(highlights[0]["confirmed"])

    def test_done_summary_has_top7_and_split(self):
        body = qa._done_summary_body(self._done_items())
        self.assertIn("*Top 7*", body)
        self.assertIn("*Work*", body)
        self.assertIn("*Personal*", body)
        self.assertIn("*Assistant*", body)

    def test_done_full_list_keeps_complete_items(self):
        groups = qa._done_split(self._done_items())
        full = qa._done_full_list(groups)
        for i in range(12):
            self.assertIn(f"item {i}", full)
        for line in full.splitlines():
            if line.startswith("- "):
                self.assertGreater(len(line), 8)

    def test_done_full_list_never_cuts_mid_word(self):
        long_what = ("Shared the complete growth update with the leadership team "
                     "and closed the review before the Friday deadline")
        groups = qa._done_split([{
            "jid": "222@g.us", "group": "222@g.us", "what": long_what,
            "when": "Mon", "ts": "2026-11-04 10:00:00", "confirmed": False,
        }])
        full = qa._done_full_list(groups)
        self.assertIn(long_what, full)

    def test_summary_first_serves_full_list_on_one_more(self):
        import conversation
        items = self._done_items()
        for i, d in enumerate(items):
            d["what"] = (f"item {i} with a complete clause that must not be "
                         "cut mid-word anywhere")
        groups = qa._done_split(items)
        head = "*Nembu* 🍋 · 3 chats · Mon 3–Thu 5 Nov\n*Top 7*\n1. item 0"
        rest = qa._done_full_list(groups)
        reply, turn_key = qa._store_summary_first(head, rest, "__multi__")
        self.assertIn("Say *more*", reply)
        self.assertTrue(turn_key)
        _more, text = conversation.chunk_more()
        for d in items:
            self.assertIn(d["what"], text)

    # ── SUP-005 / ASK-14: honest coverage ──────────────────────────────────
    def test_coverage_footer_reports_read_and_scope(self):
        footer = qa._coverage_footer(123, 7, 20, media=4, voice=2, cut=True)
        self.assertIn("read 123 msgs from 7 chats", footer)
        self.assertIn("20 in scope", footer)
        self.assertIn("2 voice notes not transcribed", footer)
        self.assertIn("4 images/documents not searchable", footer)
        self.assertIn("some messages were left out", footer)

    def test_chats_read_counts_actual_blocks(self):
        context, _total, _media = qa._flow_messages(
            ["222@g.us", "333@g.us"], self.start, end=self.end)
        self.assertEqual(qa._chats_read(context), 2)

    def test_done_footer_reports_sent_candidates_and_scope(self):
        footer = qa._done_footer({
            "window_messages": 3478, "chats_read": 126,
            "sent_messages": 1249, "candidates": 522,
            "chats": 91, "chats_in_scope": 126}, omitted=0)
        self.assertIn("1249 msgs sent", footer)
        self.assertIn("522 substantive candidates", footer)
        self.assertIn("126 chats in scope", footer)
        self.assertIn("91 chats with your messages", footer)
        self.assertIn("read 3478 msgs from 126 chats", footer)
        self.assertNotIn("522 msgs you sent", footer)

    def test_done_meta_sent_count_exceeds_candidates(self):
        _cands, meta = qa._done_candidates(
            [("222@g.us", 3)], self.db, self.start, self.end)
        self.assertGreater(meta["sent_messages"], meta["candidates"])
        self.assertGreaterEqual(meta["voice_unsearchable"], 1)

    # ── SUP-006 / ASK-13: person precision ─────────────────────────────────
    def test_person_query_keeps_direct_mentions_not_unrelated_dms(self):
        con = sqlite3.connect(self.db)
        con.executemany(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp,"
            " is_from_me, media_type) VALUES (?,?,?,?,?,?,?)",
            [
                ("r1", "777@lid", "me", "unrelated personal note alpha",
                 "2026-11-04 08:00:00", 1, ""),
                ("r2", "777@lid", "me", "asked Rahul about the deck",
                 "2026-11-04 08:10:00", 1, ""),
                ("r3", "777@lid", "me", "unrelated personal note beta",
                 "2026-11-04 08:20:00", 1, ""),
            ],
        )
        con.commit()
        con.close()
        try:
            context, _total, _media = qa._flow_messages(
                ["777@lid"], self.start, end=self.end,
                person={"name": "Rahul", "idents": {"555@lid"}})
            self.assertIn("asked Rahul about the deck", context)
            self.assertNotIn("unrelated personal note alpha", context)
            own, _t, _m = qa._flow_messages(
                ["777@lid"], self.start, end=self.end,
                person={"name": "Rahul", "idents": {"777@lid"}})
            self.assertIn("unrelated personal note alpha", own)
        finally:
            con = sqlite3.connect(self.db)
            con.execute("DELETE FROM messages WHERE chat_jid='777@lid'")
            con.commit()
            con.close()

    def test_person_scope_caps_and_excludes_other_dms(self):
        con = sqlite3.connect(self.db)
        rows = []
        for i in range(25):
            rows.append((f"ak{i}", f"9000{i:03d}@g.us", "other",
                         "Nisha will send the deck", "2026-11-04 09:00:00",
                         0, ""))
        rows.append(("akdm", "777@lid", "other",
                     "Nisha Rao at Foodworks", "2026-11-04 09:00:00",
                     0, ""))
        con.executemany(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp,"
            " is_from_me, media_type) VALUES (?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()
        wa = sqlite3.connect(os.environ["WHATSAPP_DB_PATH"])
        wa.execute("INSERT INTO whatsmeow_contacts VALUES "
                   "('15551234567@s.whatsapp.net','Nisha',NULL,NULL,NULL)")
        wa.commit()
        wa.close()
        try:
            jids, _ident = qa._person_scope("Nisha", self.start, self.end)
            self.assertLessEqual(len(jids), 20)
            self.assertIn("15551234567@s.whatsapp.net", jids)
            self.assertNotIn("777@lid", jids)
        finally:
            con = sqlite3.connect(self.db)
            con.execute("DELETE FROM messages WHERE id LIKE 'ak%'")
            con.commit()
            con.close()
            wa = sqlite3.connect(os.environ["WHATSAPP_DB_PATH"])
            wa.execute("DELETE FROM whatsmeow_contacts "
                       "WHERE their_jid='15551234567@s.whatsapp.net'")
            wa.commit()
            wa.close()

    # ── SUP-007 / ASK-11: open items ───────────────────────────────────────
    def test_open_items_have_do_first_overdue_and_sections(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"chat": "Instinct", "what": "Anaya dinner",
             "deadline": "2026-11-06", "priority": "high", "on_you": True},
            {"chat": "Vamsi", "what": "review formats",
             "priority": "medium", "on_you": True},
            {"chat": "UPI Mapper", "what": "PayNet mapper",
             "deadline": "Wed", "priority": "low", "on_you": False},
        ]}
        body = qa._open_items_body(data2, now=now)
        self.assertIn("*Do first*", body)
        self.assertIn("*OVERDUE 3d*", body)
        self.assertIn("*On you*", body)
        self.assertIn("*Others*", body)
        first = body.split("*Do first*", 1)[1].split("*On you*", 1)[0]
        self.assertIn("Anaya dinner", first)
        self.assertIn("PayNet mapper", body)

    def test_open_item_age_days_from_since(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        item = qa._norm_open_item({"what": "x", "since": "2026-11-04"}, now)
        self.assertEqual(item["age_days"], 5)
        self.assertEqual(item["overdue_days"], 0)

    def test_deadline_parser_understands_dates_and_weekdays(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        self.assertEqual(qa._parse_deadline("2026-11-06", now),
                         datetime.date(2026, 11, 6))
        self.assertEqual(qa._parse_deadline("6 Nov", now),
                         datetime.date(2026, 11, 6))
        self.assertEqual(qa._parse_deadline("Wed", now),
                         datetime.date(2026, 11, 11))
        self.assertEqual(qa._parse_deadline("tomorrow", now),
                         datetime.date(2026, 11, 10))

    def test_open_item_owner_quote_infers_on_you(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            item = qa._norm_open_item(
                {"owner": "Sam Rao",
                 "owner_quote": "you need to send energy gel",
                 "what": "Send energy gel", "on_you": False}, now)
        self.assertTrue(item["on_you"])

    def test_on_you_never_says_nothing_when_do_first_has_on_you(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Maya", "owner": "Sam Rao",
             "owner_quote": "please do this task", "what": f"task {i}",
             "on_you": True, "priority": "high"} for i in range(3)
        ]}
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            body = qa._open_items_body(data2, now=now)
        self.assertNotIn("\nNothing waiting on you.", body)
        self.assertIn("task 0", body)

    def test_open_items_render_three_on_you_and_two_others(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        mine = ["Approve alpha budget", "Review beta plan", "Share gamma report",
                "Fix delta bug", "Check epsilon doc"]
        theirs = ["Ship zeta release", "Draft eta proposal", "Book theta room",
                  "Send iota invite", "Plan kappa offsite", "Close lambda ticket"]
        data2 = {"open_items": [
            {"person": "Maya", "owner": "Sam Rao",
             "owner_quote": "please do this task", "what": text,
             "on_you": True, "priority": "high" if i == 0 else "low"}
            for i, text in enumerate(mine)
        ] + [
            {"person": "Raghav", "what": text, "on_you": False,
             "priority": "high" if i < 2 else "low"}
            for i, text in enumerate(theirs)
        ]}
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            body = qa._open_items_body(data2, now=now)
        on_section = body.split("*On you*", 1)[1].split("*Others*", 1)[0]
        others_section = body.split("*Others*", 1)[1]
        self.assertGreaterEqual(on_section.count("- "), 3)
        self.assertGreaterEqual(others_section.count("- "), 2)

    def test_call2_retries_on_network_failure(self):
        calls = {"n": 0}

        def flaky(*_a, **_k):
            calls["n"] += 1
            if calls["n"] < 3:
                return {"_gemini_failed": True, "_reason": "network"}
            return {"ok": True}

        with patch.object(qa, "_call_json", side_effect=flaky), \
                patch.dict(os.environ, {"QA_RETRY_DELAY": "0"}):
            out = qa._call_json_retry("p", {}, {}, "qa-call2", "t", attempts=3)
        self.assertTrue(out.get("ok"))
        self.assertEqual(calls["n"], 3)

    @unittest.skipUnless(os.getenv("MOSAIC_CASE3_STORE"),
                         "set MOSAIC_CASE3_STORE to a copied store")
    def test_case3_real_store_has_three_on_you_and_two_others(self):
        store = os.environ["MOSAIC_CASE3_STORE"]
        env = dict(
            os.environ,
            MOSAIC_HOME=os.path.dirname(store.rstrip("/")),
            STORE_DIR=store,
            MESSAGES_DB_PATH=os.path.join(store, "messages.db"),
            ENGINE_DB_PATH=os.path.join(store, "engine.db"),
            WHATSAPP_DB_PATH=os.path.join(store, "whatsapp.db"),
            MOSAIC_SEARCH_DB=os.path.join(store, "search.db"),
        )
        proc = subprocess.run(
            [sys.executable, os.path.join("scripts", "qa_case3_regression.py")],
            env=env, capture_output=True, text=True, cwd=os.getcwd())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    # ── SUP-008 / ASK-12: future commitments ───────────────────────────────
    def test_future_plan_context_has_both_plan_ids(self):
        now = qa.mstate.now_local()
        start = now - datetime.timedelta(days=7)
        context, ids, jids = qa._future_plan_context(
            "what's the plan for tomorrow?", start, now,
            allowed=None, db_path=self.db)
        self.assertIn("A1B2C3D4E5F60718293A", ids)
        self.assertIn("C1D2E3F4A5B6071829", ids)
        self.assertIn("Airlines go live is tomorrow", context)
        self.assertIn("John will visit tomorrow, Nov 10", context)

    def test_future_plan_scan_ignores_non_future_questions(self):
        self.assertEqual(
            qa._future_plan_chats("what's pending?", self.start, self.end,
                                  db_path=self.db),
            [])

    def test_merged_prompt_anchors_today_and_tomorrow(self):
        now = qa.mstate.now_local()
        tomorrow = now + datetime.timedelta(days=1)
        prompt = qa._merged_prompt(
            "what's the plan for tomorrow?", "tomorrow", {}, ["444@g.us"],
            "context", "lookup")
        self.assertIn(f"Today is {now.strftime('%A %d %b %Y')}", prompt)
        self.assertIn(tomorrow.strftime("%A %d %b %Y"), prompt)

    # ── SUP-009: decision window ───────────────────────────────────────────
    def test_decision_question_uses_thirty_day_window(self):
        text = "why did we kill the biometric ads?"
        self.assertEqual(qa._ask_mode(text), "decision")
        start, end = qa._window_from({"task_outline": text}, text)
        self.assertGreaterEqual((end - start).days, 29)

    def test_plain_lookup_stays_short(self):
        text = "when is the merchant MBR?"
        self.assertEqual(qa._ask_mode(text), "lookup")
        start, end = qa._window_from({"task_outline": text}, text)
        self.assertLess((end - start).days, 29)

    # ── SUP-010: done latency ──────────────────────────────────────────────
    def test_done_question_skips_call1(self):
        sentinel = {"kind": "question", "decision": "resolved", "expert": "done"}
        with patch.object(qa, "_flow_done", return_value=sentinel) as done, \
                patch.object(qa, "_flow_answer",
                             side_effect=AssertionError("call1 path used")):
            out = qa.answer("what did I get done this week")
        self.assertEqual(out, sentinel)
        done.assert_called_once()

    def test_done_batch_is_large_enough(self):
        self.assertGreaterEqual(qa.DONE_BATCH, 250)

    def test_done_extract_runs_batches_in_parallel(self):
        import threading
        import time as _time
        threads = set()

        def fake(*_a, **_k):
            threads.add(threading.get_ident())
            _time.sleep(0.05)
            return {"done": []}

        cands = [{
            "n": i, "jid": "222@g.us", "group": "G", "when": "Mon",
            "ts": f"2026-11-04 10:{i:02d}:00", "text": f"did thing {i}",
            "next": "", "confirmed": False,
        } for i in range(1, 5)]
        with patch.object(qa, "DONE_BATCH", 1), \
                patch.object(qa, "DONE_BATCH_CHARS", 120000), \
                patch.object(qa, "_call_json_retry", side_effect=fake):
            qa._done_extract(cands, "q", self.start, self.end, {},
                             [("222@g.us", 1)])
        self.assertGreaterEqual(len(threads), 2)

    # ── SUP-011: reply shape and urgency ───────────────────────────────────
    def test_reply_mode_is_not_overridden_by_answer_shape(self):
        self.assertEqual(qa._ask_mode("what should I respond to"), "reply")
        self.assertEqual(qa._resolve_mode("reply", "on_you_others"), "reply")
        self.assertEqual(qa._resolve_mode("catch_up", "on_you_others"),
                         "catch_up")
        self.assertEqual(qa._resolve_mode("open_items", "on_you_others"),
                         "open_items")
        self.assertEqual(qa._resolve_mode("reply", "done"), "done")

    def test_raw_respond_text_beats_outline_pending(self):
        self.assertEqual(qa._ask_mode("what should I respond to today?"),
                         "reply")
        self.assertEqual(
            qa._ask_mode("pending tasks: what should I respond to today?"),
            "reply")
        self.assertEqual(
            qa._resolve_mode(
                qa._ask_mode("pending tasks: what should I respond to today?"),
                "on_you_others"),
            "reply")

    # ── SUP-012 / ASK-15: formatting polish ────────────────────────────────
    def test_sentence_like_group_name_is_sanitized(self):
        long_name = ("This is a very long group name that is actually a whole "
                     "sentence about work")
        out = qa._display_group_name(long_name, "5550000000@g.us")
        self.assertLessEqual(len(out), 40)
        self.assertNotEqual(out, long_name)

    def test_punctuation_contact_label_falls_back_to_dm(self):
        qa._NAME_CACHE.clear()
        qa._CONTACT_CACHE = None
        self.assertEqual(qa._display_group_name(".", "222222222222222@lid"),
                         "DM 2222")
        self.assertEqual(qa._chat_name("222222222222222@lid"), "DM 2222")

    def test_done_footer_splits_voice_from_images(self):
        footer = qa._done_footer({
            "window_messages": 100, "chats_read": 5, "sent_messages": 40,
            "candidates": 20, "chats_in_scope": 5,
            "voice_unsearchable": 156, "media_other": 211}, omitted=0)
        self.assertIn("156 voice notes not transcribed", footer)
        self.assertIn("211 images/docs/video not searchable", footer)
        self.assertNotIn("362 media not searchable", footer)

    def test_active_selection_summarize_does_not_clarify(self):
        import router
        qa.clear_selection()
        self.assertTrue(router._needs_summarize_clarify("summarize this"))
        qa.set_selection("222@g.us", "Group B", "q", "a")
        try:
            self.assertFalse(router._needs_summarize_clarify("summarize this"))
        finally:
            qa.clear_selection()

    def test_search_requires_all_terms(self):
        import qa_search
        hits = qa_search.search(
            self.db, os.environ.get("ENGINE_DB_PATH"), "zorblat doc",
            self.start, self.end)
        self.assertEqual(hits, [])

    def test_search_ignores_generic_verbs(self):
        import qa_search
        hits = qa_search.search(
            self.db, os.environ.get("ENGINE_DB_PATH"), "see zorblat",
            self.start, self.end)
        self.assertTrue(hits)
        self.assertTrue(all(h["msg_id"] == "s2" for h in hits))

    # ── SUP-015 / ASK-01: decision kill facts ──────────────────────────────
    def test_decision_prompt_treats_kill_as_fact(self):
        prompt = qa._merged_prompt(
            "why did we kill the biometric ads?", "biometric",
            {}, ["120363000000000003@g.us"], "context", "decision")
        self.assertIn("kill/stop/pause/go/no-go", prompt)
        self.assertIn("recommendation, not a confirmed decision", prompt)

    def test_search_finds_the_kill_recommendation(self):
        import qa_search
        now = qa.mstate.now_local()
        hits = qa_search.search(
            self.db, os.environ.get("ENGINE_DB_PATH"),
            "biometric kill", now - datetime.timedelta(days=30), now)
        self.assertTrue(any(h["msg_id"] == "B1C2D3E4F5A60718293B" for h in hits))

    def test_decision_context_finds_the_kill_message(self):
        now = qa.mstate.now_local()
        context, ids, _jids = qa._decision_context(
            "why did we kill the biometric ads?",
            now - datetime.timedelta(days=30), now,
            allowed=None, db_path=self.db)
        self.assertIn("B1C2D3E4F5A60718293B", ids)
        self.assertIn("kill today", context)

    # ── SUP-005 / ASK-17: decision coverage counts chats ───────────────────
    def test_decision_context_has_countable_chat_headers(self):
        now = qa.mstate.now_local()
        context, _ids, jids = qa._decision_context(
            "why did we kill the biometric ads?",
            now - datetime.timedelta(days=30), now,
            allowed=None, db_path=self.db)
        self.assertGreaterEqual(qa._chats_read(context), 1)
        self.assertEqual(qa._chats_read(context), len(jids))

    # ── SUP-015 / ASK-01: keep the kill recommendation ─────────────────────
    def test_decision_facts_keep_the_kill_recommendation(self):
        now = qa.mstate.now_local()
        facts = []
        qa._decision_context(
            "why did we kill the biometric ads?",
            now - datetime.timedelta(days=30), now,
            allowed=None, db_path=self.db, facts_out=facts)
        kill = next(f for f in facts
                    if f.get("id") == "B1C2D3E4F5A60718293B")
        self.assertIn("kill today", kill["quote"])
        self.assertIn("Recommendation, not a confirmed decision", kill["what"])
        rendered = qa._fallback_answer_body("decision", {"facts": facts})
        self.assertIn("kill today", rendered)
        self.assertIn("Recommendation, not a confirmed decision", rendered)

    def test_decision_answer_keeps_the_quote_and_source_id(self):
        facts = [{
            "group": "UPI <> Central Growth", "who": "Someone",
            "when": "Fri 06 Nov 15:57",
            "what": "Recommendation, not a confirmed decision: kill today",
            "quote": "my recommendation is to not invest any more and kill today",
            "id": "B1C2D3E4F5A60718293B",
        }]
        body = qa._apply_decision_facts(
            "No information or reason for killing biometric ads is available.", facts)
        self.assertIn("kill today", body)
        self.assertIn("Recommendation, not a confirmed decision", body)
        self.assertIn("B1C2D3E4F5A60718293B", body)

    # ── ASK-18: open items never repeat across sections ────────────────────
    def test_open_items_do_not_repeat_across_sections(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Sam Rao", "what": "Provide performance feedback",
             "on_you": True, "priority": "high"},
            {"person": "Raghav", "what": "Share account-wise breakdown",
             "on_you": False, "priority": "high"},
            {"person": "Nisha",
             "what": "Execute merchant-level interventions",
             "on_you": False, "priority": "medium"},
            {"person": "Vamsi", "what": "Review formats",
             "on_you": True, "priority": "low"},
        ]}
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            body = qa._open_items_body(data2, now=now)
        do = body.split("*Do first*", 1)[1].split("*On you*", 1)[0]
        on = body.split("*On you*", 1)[1].split("*Others*", 1)[0]
        oth = body.split("*Others*", 1)[1]
        for text in ("Provide performance feedback",
                     "Share account-wise breakdown",
                     "Execute merchant-level interventions"):
            sections = [s for s in (do, on, oth) if text in s]
            self.assertEqual(len(sections), 1, text)

    def test_open_items_dedupe_paraphrases_and_structured_plain(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {
            "open_items": [
                {"person": "Vihaan Gupta",
                 "what": "Provide feedback on Merchant MBR draft",
                 "on_you": True, "priority": "high"},
                {"person": "Vihaan Gupta",
                 "what": "Move Zephyr core loop cost approvals to Varun",
                 "on_you": True, "priority": "high"},
            ],
            "on_you": [
                "Review draft document for monthly Merchant MBR scheduled "
                "for November 17th.",
                "Re-route Zephyr core loop cost approvals (comms, Juspay, "
                "etc.) to Varun.",
            ],
        }
        items = qa._open_items(data2, now)
        self.assertEqual(len(items), 2)
        text = " ".join(i["what"] for i in items)
        self.assertIn("Provide feedback on Merchant MBR draft", text)
        self.assertIn("Move Zephyr core loop cost approvals to Varun", text)

    def test_on_you_counts_do_first_items(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Sam Rao", "owner": "Sam Rao",
             "owner_quote": "asked Sam to provide feedback",
             "what": "Provide performance feedback", "on_you": True,
             "priority": "high"},
            {"person": "Sam Rao", "owner": "Sam Rao",
             "owner_quote": "asked Sam to approve the Zephyr budget",
             "what": "Approve Zephyr budget", "on_you": True,
             "priority": "high"},
            {"person": "Sam Rao", "owner": "Sam Rao",
             "owner_quote": "asked Sam to review the MBR draft",
             "what": "Review MBR draft", "on_you": True,
             "priority": "high"},
            {"person": "Raghav", "what": "Share breakdown",
             "on_you": False, "priority": "low"},
            {"person": "Nisha", "what": "Execute interventions",
             "on_you": False, "priority": "low"},
        ]}
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            body = qa._open_items_body(data2, now=now)
        do = body.split("*Do first*", 1)[1].split("*On you*", 1)[0]
        on = body.split("*On you*", 1)[1].split("*Others*", 1)[0]
        others = body.split("*Others*", 1)[1]
        self.assertGreaterEqual(on.count("- "), 3)
        self.assertGreaterEqual(others.count("- "), 2)
        for text in ("Provide performance feedback", "Approve Zephyr budget",
                     "Review MBR draft"):
            self.assertIn(text, do)
            self.assertNotIn(text, on)

    def test_on_you_is_deterministic_when_model_says_false(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Sam Rao", "owner": "Sam Rao",
             "owner_quote": "asked Sam to provide feedback",
             "what": "Provide performance feedback", "on_you": False,
             "priority": "high"},
            {"person": "Sam Rao", "owner": "Sam Rao",
             "owner_quote": "asked Sam to approve the Zephyr budget",
             "what": "Approve Zephyr budget", "on_you": False,
             "priority": "high"},
            {"person": "Sam Rao", "owner": "Sam Rao",
             "owner_quote": "asked Sam to review the MBR draft",
             "what": "Review MBR draft", "on_you": False,
             "priority": "high"},
            {"person": "Vihaan Gupta", "what": "Check Zephyr",
             "on_you": False, "priority": "low"},
        ] + [
            {"person": f"Owner {i}", "what": f"Other task {i}",
             "on_you": False, "priority": "low"} for i in range(9)
        ]}
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            body = qa._open_items_body(data2, now=now)
        on = body.split("*On you*", 1)[1].split("*Others*", 1)[0]
        others = body.split("*Others*", 1)[1]
        self.assertGreaterEqual(on.count("- "), 3)
        self.assertGreaterEqual(others.count("- "), 2)
        self.assertNotIn("Nothing waiting on you.", on)

    def test_no_minimum_promotes_non_owner_items(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Sam Rao", "owner": "Sam Rao",
             "owner_quote": "asked Sam to do the owner task",
             "what": "Owner task", "on_you": False},
            {"person": "Nisha", "what": "Coupon close", "on_you": True},
            {"person": "PayNet", "what": "Mapper", "on_you": True},
        ]}
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            body = qa._open_items_body(data2, now=now)
        on = body.split("*On you*", 1)[1].split("*Others*", 1)[0]
        others = body.split("*Others*", 1)[1]
        do = body.split("*Do first*", 1)[1].split("*On you*", 1)[0]
        self.assertEqual(on.count("- "), 1)
        self.assertIn("Sam Rao", on)
        self.assertNotIn("Nisha", on)
        self.assertNotIn("PayNet", on)
        self.assertIn("Nisha", others)
        self.assertIn("PayNet", others)
        self.assertIn("Owner task", do)
        self.assertIn("Coupon close", do)
        self.assertIn("Mapper", do)

    def test_case11_non_owner_items_stay_out_of_on_you(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "PayNet", "what": "Resolve mapper", "on_you": True},
            {"person": "Sai", "what": "Share deck", "on_you": True},
            {"person": "Roshni", "what": "Confirm date", "on_you": True},
            {"person": "Vikram", "what": "Send invoice", "on_you": True},
            {"person": "VIK", "what": "Review plan", "on_you": True},
            {"person": "YJ", "what": "Approve budget", "on_you": True},
            {"person": "Sam Rao", "owner": "Sam Rao",
             "owner_quote": "asked Sam to provide feedback",
             "what": "Provide feedback", "on_you": False},
        ]}
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            body = qa._open_items_body(data2, now=now)
        on = body.split("*On you*", 1)[1].split("*Others*", 1)[0]
        for name in ("PayNet", "Sai", "Roshni", "Vikram", "VIK", "YJ"):
            self.assertNotIn(name, on)
        self.assertIn("Provide feedback", on)

    def test_case8_akanksha_coupon_stays_out_of_on_you(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Nisha",
             "what": "Close Quickcart coupon escalation by Nov 6",
             "on_you": True, "deadline": "2026-11-06", "priority": "high"},
            {"person": "Sam Rao", "what": "Send energy gel email",
             "on_you": False},
        ]}
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            body = qa._open_items_body(data2, now=now)
        on = body.split("*On you*", 1)[1].split("*Others*", 1)[0]
        others = body.split("*Others*", 1)[1]
        do = body.split("*Do first*", 1)[1].split("*On you*", 1)[0]
        self.assertIn("Nisha", others)
        self.assertNotIn("Nisha", on)
        self.assertIn("coupon", do)

    def test_open_items_collapse_check_zapro_paraphrase(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Vihaan Gupta", "what": "Check Zephyr",
             "on_you": False},
            {"person": "Vihaan Gupta",
             "what": "review Zephyr request from Vihaan Gupta",
             "on_you": False},
            {"person": "Vihaan Gupta",
             "what": "Move Zephyr core loop cost approvals to Varun",
             "on_you": False},
        ]}
        items = qa._open_items(data2, now)
        # The check/review paraphrase collapses; the move task stays separate.
        self.assertEqual(len(items), 2)
        text = " ".join(i["what"] for i in items)
        self.assertIn("Check Zephyr", text)
        self.assertIn("Move Zephyr core loop cost approvals to Varun", text)
        self.assertNotIn("review Zephyr request", text)

    def test_you_in_a_direct_ask_resolves_to_user(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            item = qa._norm_open_item(
                {"person": "Vihaan Gupta",
                 "what": "asked you to check Zephyr",
                 "on_you": True}, now)
        self.assertTrue(item["on_you"])

    def test_owner_item_survives_a_false_model_boolean(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            item = qa._norm_open_item(
                {"person": "Sam Rao", "owner": "Sam Rao",
                 "owner_quote": "asked Sam to send the energy gel email",
                 "what": "Send energy gel email", "on_you": False}, now)
        self.assertTrue(item["on_you"])

    def test_owner_requester_example(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            item = qa._norm_open_item(
                {"person": "Maya", "owner": "Sam Rao",
                 "owner_quote": "asked Sam to submit feedback",
                 "what": "Provide feedback", "on_you": False}, now)
        self.assertTrue(item["on_you"])
        self.assertEqual(item["person"], "Maya")
        self.assertEqual(item["owner"], "Sam Rao")
        line = qa._open_item_line(item)
        self.assertIn("Sam Rao", line)
        self.assertNotIn("Maya", line)

    def test_owner_decides_not_the_requester(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            item = qa._norm_open_item(
                {"person": "Sam Rao", "owner": "Maya",
                 "owner_quote": "Maya to submit feedback",
                 "what": "Provide feedback", "on_you": True}, now)
        self.assertFalse(item["on_you"])

    def test_user_request_owner_is_the_next_action_recipient(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            item = qa._norm_open_item(
                {"person": "Sam Rao", "owner": "Sam Rao",
                 "owner_quote": "i need Phi commerce call, when can you schedule?",
                 "next_action": "Chaitanya",
                 "what": "Schedule Orbit Commerce call", "on_you": True}, now)
        self.assertEqual(item["owner"], "Chaitanya")
        self.assertFalse(item["on_you"])

    def test_user_group_request_has_no_user_owner(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            item = qa._norm_open_item(
                {"person": "Sam Rao", "owner": "Sam Rao",
                 "owner_quote": "Change offer callout to 75+50 and coupon "
                                "code to SHOP125",
                 "what": "Change offer callout and coupon code",
                 "on_you": True}, now)
        self.assertEqual(item["owner"], "")
        self.assertFalse(item["on_you"])

    def test_user_self_task_stays_on_you(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            item = qa._norm_open_item(
                {"person": "Sam Rao", "owner": "Sam Rao",
                 "owner_quote": "i need to send the deck",
                 "what": "Send the deck", "on_you": False}, now)
        self.assertTrue(item["on_you"])

    def test_owner_missing_goes_to_others_even_with_user_person(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            owner_missing = qa._norm_open_item(
                {"person": "Sam Rao", "what": "Send email"}, now)
            both_missing = qa._norm_open_item(
                {"chat": "Instinct", "what": "Send email"}, now)
            other_person = qa._norm_open_item(
                {"person": "PayNet", "what": "Resolve mapper"}, now)
        self.assertFalse(owner_missing["on_you"])
        self.assertFalse(both_missing["on_you"])
        self.assertFalse(other_person["on_you"])

    def test_case3_owner_items_are_stable_three_runs(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Maya", "owner": "Sam Rao",
             "owner_quote": "asked Sam to provide performance feedback",
             "what": "Provide performance feedback", "on_you": False,
             "priority": "high"},
            {"person": "Anjali", "owner": "Sam Rao",
             "owner_quote": "asked Sam to approve Zephyr budget",
             "what": "Approve Zephyr budget", "on_you": False,
             "priority": "high"},
            {"person": "Vihaan Gupta", "owner": "Sam Rao",
             "owner_quote": "asked Sam to review MBR draft",
             "what": "Review MBR draft", "on_you": False,
             "priority": "high"},
        ] + [
            {"person": f"Owner {i}", "owner": f"Owner {i}",
             "owner_quote": f"Owner {i} will update",
             "what": f"Other task {i}", "on_you": True, "priority": "low"}
            for i in range(9)
        ]}
        for _ in range(3):
            with patch.object(qa, "_own_names", return_value={"sam rao"}):
                body = qa._open_items_body(data2, now=now)
            on = body.split("*On you*", 1)[1].split("*Others*", 1)[0]
            self.assertEqual(on.count("- "), 3)
            self.assertNotIn("Nothing waiting on you.", on)

    def test_case16_non_owner_items_stay_out_of_on_you(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Zephyr", "owner": "Zephyr", "what": "Send details",
             "on_you": True},
            {"person": "Richeek", "owner": "Richeek", "what": "Share doc",
             "on_you": True},
            {"person": "Shaan", "owner": "Shaan",
             "what": "Confirm slot", "on_you": True},
            {"person": "Sam Rao", "owner": "Sam Rao",
             "owner_quote": "asked Sam to review the plan",
             "what": "Review plan", "on_you": False},
        ]}
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            body = qa._open_items_body(data2, now=now)
        on = body.split("*On you*", 1)[1].split("*Others*", 1)[0]
        for name in ("Zephyr", "Richeek", "Shaan"):
            self.assertNotIn(name, on)
        self.assertIn("Review plan", on)

    def test_owner_dedupe_collapses_same_task_from_different_requesters(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Maya", "owner": "Sam Rao",
             "owner_quote": "asked Sam to provide feedback",
             "what": "Provide feedback on Merchant MBR draft"},
            {"person": "Anjali", "owner": "Sam Rao",
             "owner_quote": "asked Sam to provide feedback",
             "what": "Provide feedback on Merchant MBR draft"},
        ]}
        items = qa._open_items(data2, now)
        self.assertEqual(len(items), 1)

    def test_owner_dedupe_keeps_zapro_pair_and_move_separate(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Vihaan Gupta", "owner": "Sam Rao",
             "owner_quote": "please check Zephyr approvals",
             "what": "Check Zephyr"},
            {"person": "Vihaan Gupta", "owner": "Sam Rao",
             "owner_quote": "please review the Zephyr request",
             "what": "review Zephyr request from Vihaan Gupta"},
            {"person": "Vihaan Gupta", "owner": "Sam Rao",
             "owner_quote": "please move Zephyr core loop cost approvals",
             "what": "Move Zephyr core loop cost approvals to Varun"},
        ]}
        items = qa._open_items(data2, now)
        self.assertEqual(len(items), 2)
        text = " ".join(i["what"] for i in items)
        self.assertIn("Check Zephyr", text)
        self.assertIn("Move Zephyr core loop cost approvals to Varun", text)

    def test_single_owner_item_is_not_padded(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Maya", "owner": "Sam Rao",
             "owner_quote": "asked Sam to do the owner task",
             "what": "Owner task", "on_you": False},
        ] + [
            {"person": f"Owner {i}", "owner": f"Owner {i}",
             "owner_quote": f"Owner {i} will update",
             "what": f"Other task {i}", "on_you": True} for i in range(9)
        ]}
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            body = qa._open_items_body(data2, now=now)
        on = body.split("*On you*", 1)[1].split("*Others*", 1)[0]
        self.assertEqual(on.count("- "), 1)

    def test_regression_run3_other_people_stay_in_others(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Raghav", "owner": "Sam Rao",
             "owner_quote": "Will share an update on campaigns and C2",
             "what": "Share account-wise breakdown and C2 offer coverage",
             "on_you": True},
            {"person": "Tara", "owner": "Sam Rao",
             "owner_quote": "Tara to update the central tracking sheet",
             "what": "Update central tracking sheet", "on_you": True},
            {"person": "Nisha", "owner": "Sam Rao",
             "owner_quote": "AK to resolve the Vendor legal clause",
             "what": "Resolve Vendor legal clause", "on_you": True},
            {"person": "Maya", "owner": "Sam Rao",
             "owner_quote": "asked Sam to provide feedback",
             "what": "Provide performance feedback", "on_you": False},
        ]}
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            body = qa._open_items_body(data2, now=now)
        on = body.split("*On you*", 1)[1].split("*Others*", 1)[0]
        for text in ("Share account-wise breakdown",
                     "Update central tracking sheet",
                     "Resolve Vendor legal"):
            self.assertNotIn(text, on)
        self.assertIn("Provide performance feedback", on)

    def test_case16_five_non_owner_items_stay_out_of_on_you(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Raghav", "owner": "Sam Rao",
             "owner_quote": "Will share an update on campaigns",
             "what": "Share campaign update", "on_you": True},
            {"person": "Tara", "owner": "Sam Rao",
             "owner_quote": "Tara to update the brand ads sheet",
             "what": "Update brand ads sheet", "on_you": True},
            {"person": "Nisha", "owner": "Sam Rao",
             "owner_quote": "Nisha to follow up and close by 6th",
             "what": "Close Quickcart coupon", "on_you": True},
            {"person": "Maya", "owner": "Sam Rao",
             "owner_quote": "Maya to send M0 M1 update",
             "what": "Send M0 M1 update", "on_you": True},
            {"person": "Vihaan", "owner": "Sam Rao",
             "owner_quote": "Vihaan will send the weekly STO",
             "what": "Send weekly STO", "on_you": True},
            {"person": "Maya", "owner": "Sam Rao",
             "owner_quote": "asked Sam for the KAM revenue report",
             "what": "Share KAM revenue report", "on_you": False},
        ]}
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            body = qa._open_items_body(data2, now=now)
        on = body.split("*On you*", 1)[1].split("*Others*", 1)[0]
        for text in ("Share campaign update", "Update brand ads sheet",
                     "Close Quickcart coupon", "Send M0 M1 update",
                     "Send weekly STO"):
            self.assertNotIn(text, on)
        self.assertIn("Share KAM revenue report", on)

    def test_case8_akanksha_details_owner_is_akanksha_not_rachit(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Nisha", "owner": "Rohan",
             "owner_quote": "Nisha to share the Quickcart/Food details",
             "what": "Share Quickcart/Food details", "on_you": True},
            {"person": "Maya", "owner": "Sam Rao",
             "owner_quote": "asked Sam to send energy gel email",
             "what": "Send energy gel email", "on_you": False},
        ]}
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            items = qa._open_items(data2, now)
            body = qa._open_items_body(data2, now=now)
        akanksha = next(i for i in items
                        if i["what"] == "Share Quickcart/Food details")
        self.assertEqual(akanksha["owner"], "Nisha")
        self.assertFalse(akanksha["on_you"])
        on = body.split("*On you*", 1)[1].split("*Others*", 1)[0]
        do = body.split("*Do first*", 1)[1].split("*On you*", 1)[0]
        self.assertNotIn("Nisha", on)
        self.assertIn("Sam Rao", on)
        self.assertIn("Send energy gel email", do)

    def test_case9_john_and_rishab_are_not_sameer(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "John", "owner": "Sam Rao",
             "owner_quote": "John's list ... inspect the T Nagar door",
             "what": "Inspect the T Nagar main door", "on_you": True},
            {"person": "Raghav", "owner": "Sam Rao",
             "owner_quote": "Will share an update on campaigns and C2",
             "what": "Share C2 update", "on_you": True},
            {"person": "Maya", "owner": "Sam Rao",
             "owner_quote": "asked Sam to provide feedback",
             "what": "Provide feedback", "on_you": False},
        ]}
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            items = qa._open_items(data2, now)
        owners = {i["what"]: i["owner"] for i in items}
        self.assertEqual(owners["Inspect the T Nagar main door"], "John")
        self.assertEqual(owners["Share C2 update"], "Raghav")
        self.assertNotEqual(owners["Inspect the T Nagar main door"],
                            "Sam Rao")
        self.assertNotEqual(owners["Share C2 update"], "Sam Rao")

    def test_case3_genuine_owner_items_are_on_you(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Maya", "owner": "Sam Rao",
             "owner_quote": "asked Sam to submit performance feedback",
             "what": "Submit performance feedback", "on_you": False},
            {"person": "Anaya", "owner": "Sam Rao",
             "owner_quote": "Anaya requested resolution for the Lakeview Paris bathroom leak",
             "what": "Resolve Lakeview Paris bathroom leak", "on_you": False},
            {"person": "Vihaan Gupta", "owner": "Sam Rao",
             "owner_quote": "please check Zephyr approvals",
             "what": "Check Zephyr approvals", "on_you": False},
        ]}
        with patch.object(qa, "_own_names", return_value={"sam rao"}):
            body = qa._open_items_body(data2, now=now)
        on = body.split("*On you*", 1)[1].split("*Others*", 1)[0]
        do = body.split("*Do first*", 1)[1].split("*On you*", 1)[0]
        self.assertGreaterEqual(on.count("- "), 3)
        self.assertIn("Submit performance feedback", do)
        self.assertIn("Resolve Lakeview Paris bathroom leak", do)
        self.assertIn("Check Zephyr approvals", do)
        self.assertNotIn("Nothing waiting on you.", on)

    def test_recall_direct_asks_finds_the_case3_trio(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        con = sqlite3.connect(self.db)
        rows = [
            ("rec-mudit", "222@g.us", "Maya",
             "Maya asked Sam to submit performance feedback",
             "2026-11-04 13:00:00", 0, ""),
            ("rec-anusha", "222@g.us", "Anaya",
             "Anaya requested resolution/inspection for the Lakeview leak",
             "2026-11-04 13:01:00", 0, ""),
            ("rec-vardhan", "222@g.us", "Vihaan",
             "Vihaan: please check Zephyr approvals",
             "2026-11-04 13:02:00", 0, ""),
            ("rec-rishab", "222@g.us", "Raghav",
             "Raghav will share an update", "2026-11-04 13:03:00", 0, ""),
            ("rec-user", "222@g.us", "Sam",
             "i need Phi commerce call, when can you schedule?",
             "2026-11-04 13:04:00", 1, ""),
        ]
        con.executemany(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp,"
            " is_from_me, media_type) VALUES (?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()
        try:
            with patch.object(qa, "_own_names", return_value={"sam rao"}):
                items = qa._recall_direct_asks(
                    ["222@g.us"], self.start, self.end, {})
                resolved = [qa._norm_open_item(i, now) for i in items]
                merged = qa._open_items({
                    "open_items": [{
                        "person": "Maya", "owner": "Sam Rao",
                        "owner_quote": "asked Sam to submit performance feedback",
                        "what": "Submit performance feedback",
                    }] + items,
                }, now)
            text = " ".join(i["what"] for i in items)
            self.assertIn("Maya asked Sam", text)
            self.assertIn("Anaya requested", text)
            self.assertIn("Vihaan: please check Zephyr", text)
            self.assertNotIn("Raghav will share", text)
            self.assertNotIn("Phi commerce", text)
            trio = [i for i in resolved
                    if any(k in i["what"] for k in
                           ("Maya asked", "Anaya requested",
                            "Vihaan: please check"))]
            self.assertEqual(len(trio), 3)
            self.assertTrue(all(i["on_you"] for i in trio))
            feedback = [i for i in merged
                        if "performance feedback" in i["what"].lower()]
            self.assertEqual(len(feedback), 1)
        finally:
            con = sqlite3.connect(self.db)
            con.execute("DELETE FROM messages WHERE id LIKE 'rec-%'")
            con.commit()
            con.close()

    def test_open_items_dedupe_structured_and_plain_within_section(self):
        now = datetime.datetime(2026, 11, 9, 12, 0)
        data2 = {"open_items": [
            {"person": "Raghav",
             "what": "Share account-wise breakdown with Nisha",
             "on_you": False},
        ], "others": [
            "Share the account-wise breakdown for Nisha.",
        ]}
        items = qa._open_items(data2, now)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["person"], "Raghav")

    # ── case 5 regression: miss questions stay catch-up ────────────────────
    def test_miss_question_is_catch_up_not_open_items(self):
        self.assertEqual(qa._ask_mode("what did I miss today?"), "catch_up")
        self.assertEqual(qa._resolve_mode("catch_up", "on_you_others"),
                         "catch_up")
        self.assertEqual(qa._ask_mode("what should I respond to today?"),
                         "reply")

    # ── SUP-014: large store stays bounded ─────────────────────────────────
    def test_ingest_watchdog_reports_lag(self):
        import worker
        now = qa.mstate.now_local()
        worker.mstate.set_watermark(
            "watchdog", "ingest_ran",
            (now - datetime.timedelta(seconds=300)).isoformat())
        out = worker._ingest_watchdog(now, max_lag_s=120)
        self.assertGreater(out["lag_s"], 120)

    def test_run_once_always_ingests(self):
        import worker
        calls = []
        with patch.object(worker, "tick_ingest",
                          side_effect=lambda: calls.append("ingest")), \
                patch.object(worker, "_process_retries", return_value=None), \
                patch.object(worker, "_bridge_ready", return_value=False), \
                patch.object(worker, "tick_drain", return_value={}), \
                patch.object(worker, "tick_retention", return_value={}):
            worker.run_once()
        self.assertEqual(calls, ["ingest"])

    def test_large_store_bounds_cards_and_profiles(self):
        import engine
        import mstate as ms
        import qa_profiles
        import watch_rules
        td = tempfile.mkdtemp(prefix="mosaic-large-")
        db = os.path.join(td, "messages.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE chats (jid TEXT, name TEXT, "
                    "last_message_time TIMESTAMP)")
        con.execute(SCHEMA)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        chats, msgs = [], []
        for i in range(1500):
            jid = f"12036300{i:06d}@g.us"
            chats.append((jid, f"Group {i}", stamp))
            msgs.append((f"m{i}", jid, "other", "hello", stamp, 0, ""))
        con.executemany("INSERT INTO chats VALUES (?,?,?)", chats)
        con.executemany(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp,"
            " is_from_me, media_type) VALUES (?,?,?,?,?,?,?)", msgs)
        con.commit()
        con.close()
        eng = os.path.join(td, "engine.db")
        wa = os.path.join(td, "whatsapp.db")
        con = sqlite3.connect(wa)
        con.execute("CREATE TABLE whatsmeow_contacts (their_jid TEXT, "
                    "full_name TEXT, first_name TEXT, push_name TEXT, "
                    "business_name TEXT)")
        con.execute("CREATE TABLE whatsmeow_chat_settings (chat_jid TEXT, "
                    "archived INTEGER)")
        con.commit()
        con.close()
        calls = []

        def fake_card(jid, _name, _rows):
            calls.append(jid)
            return {"objective": "x", "themes": "y", "latest": "z"}

        with patch.object(engine, "MESSAGES_DB_PATH", db), \
                patch.object(engine, "WHATSAPP_DB_PATH", wa), \
                patch.object(watch_rules, "MESSAGES_DB_PATH", db), \
                patch.object(watch_rules, "WHATSAPP_DB_PATH", wa), \
                patch.object(ms, "ENGINE_DB_PATH", eng), \
                patch.object(qa_profiles, "QA_SCOPE_CHATS", 2000), \
                patch.object(qa, "_build_card", side_effect=fake_card):
            qa.ensure_cards()
            self.assertLessEqual(len(calls), qa.CARD_BATCH)
            out = qa_profiles.ensure_profiles()
            self.assertLessEqual(out["built"], qa_profiles.PROFILE_BATCH)


if __name__ == "__main__":
    unittest.main()
