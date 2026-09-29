import json
import subprocess
import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import todoit

MON = date(2026, 9, 28)


def at(day, hh, mm):
    return datetime(2026, 9, day, hh, mm)


def task(title, due, done=False, sent=None):
    return {"title": title, "due": due, "done": done, "sent": sent or []}


def state(**kw):
    return {"last_reset": "2026-09-28", "daily": [], "weekly": [], "todo": [], **kw}


class ParseDue(unittest.TestCase):
    def test_valid(self):
        for kind, raw, want in [
            ("daily", "17:00", "17:00"),
            ("daily", "9:05", "09:05"),
            ("weekly", "fri", "fri 17:00"),
            ("weekly", "Monday 9:05", "mon 09:05"),
            ("weekly", "thurs 8:00", "thu 08:00"),
            ("weekly", "fri 17:00", "fri 17:00"),  # stored format round-trips (edit)
            ("todo", "tomorrow", "2026-09-29 17:00"),
            ("todo", "today 9:30", "2026-09-28 09:30"),
            ("todo", "15:00", "2026-09-28 15:00"),
            ("todo", "+3", "2026-10-01 17:00"),
            ("todo", "fri", "2026-10-02 17:00"),
            ("todo", "Monday", "2026-09-28 17:00"),
            ("todo", "thurs 9:00", "2026-10-01 09:00"),
            ("todo", "12-25 08:00", "2026-12-25 08:00"),
            ("todo", "01/05", "2027-01-05 17:00"),  # past MM-DD rolls to next year
            ("todo", "2026-10-15 18:00", "2026-10-15 18:00"),  # stored format round-trips (edit)
        ]:
            self.assertEqual(todoit.parse_due(kind, raw, MON), want, raw)

    def test_invalid(self):
        for kind, raw in [("daily", ""), ("daily", "tomorrow"), ("daily", "tomorrow 9:00"), ("daily", "25:00"),
                          ("todo", ""), ("todo", "  "), ("todo", "someday"), ("todo", "13-40"),
                          # a weekday must be the whole word, not a prefix swallowing the rest
                          ("todo", "fri 9am"), ("todo", "month"), ("todo", "sunset"), ("todo", "sat next week")]:
            with self.assertRaises(ValueError, msg=raw):
                todoit.parse_due(kind, raw, MON)
        for raw in ("", "17:00", "today", "tomorrow", "+3", "12-25", "2026-10-15",
                    "fri 9am", "sat next week", "mo"):
            with self.assertRaises(ValueError, msg=raw):
                todoit.parse_due("weekly", raw, MON)

    def test_weekly_due_is_this_iso_week(self):
        task_ = task("review", "fri 17:00")
        self.assertEqual(todoit.due_at("weekly", task_, MON), datetime(2026, 10, 2, 17, 0))
        self.assertEqual(todoit.due_at("weekly", task_, date(2026, 10, 4)), datetime(2026, 10, 2, 17, 0))


class Banners(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(todoit, "notify")
        self.notify = patcher.start()
        self.addCleanup(patcher.stop)

    def test_heads_up_then_missed_each_once(self):
        s = state(daily=[task("review a PR", "17:00")])
        todoit.check(s, at(28, 16, 0))
        self.notify.assert_not_called()
        todoit.check(s, at(28, 16, 31))
        todoit.check(s, at(28, 16, 36))
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(self.notify.call_args.args[:2], ("⏰ due 17:00", "review a PR"))
        todoit.check(s, at(28, 17, 2))
        todoit.check(s, at(28, 17, 7))
        self.assertEqual(self.notify.call_count, 2)
        self.assertIn("missed", self.notify.call_args.args[0])

    def test_weekly_heads_up_then_missed_each_once(self):
        s = state(weekly=[task("review a PR", "mon 17:00")])
        todoit.check(s, at(28, 16, 0))
        self.notify.assert_not_called()
        todoit.check(s, at(28, 16, 31))
        todoit.check(s, at(28, 16, 36))
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(self.notify.call_args.args[:2], ("⏰ due 17:00", "review a PR"))
        todoit.check(s, at(28, 17, 2))
        todoit.check(s, at(28, 17, 7))
        self.assertEqual(self.notify.call_count, 2)
        self.assertIn("missed", self.notify.call_args.args[0])

    def test_done_tasks_are_silent(self):
        todoit.check(state(daily=[task("a", "17:00", done=True)]), at(28, 18, 0))
        self.notify.assert_not_called()

    def test_overdue_todo_warns_once_ever_with_plain_title(self):
        s = state(todo=[task("review [rec](https://x.io/a)", "2026-09-27 17:00")])
        todoit.check(s, at(28, 9, 0))
        todoit.rollover(s, at(29, 0, 1))
        todoit.check(s, at(29, 9, 0))
        self.notify.assert_called_once()
        self.assertEqual(self.notify.call_args.args[:2], ("❌ missed · was due Sun 9/27 17:00", "review rec"))

    def test_rollover_resets_and_flags_unsent_misses(self):
        s = state(daily=[task("late", "23:59"), task("done", "12:00", done=True, sent=["soon"]),
                         task("warned", "12:00", sent=["soon", "missed"])],
                  todo=[task("finished", "2026-09-30 17:00", done=True), task("open", "2026-09-30 17:00")])
        self.assertTrue(todoit.rollover(s, at(29, 0, 2)))
        self.notify.assert_called_once()  # only "late": its 23:59 due fell between notifier runs
        self.assertEqual(self.notify.call_args.args[:2], ("❌ missed · was due 23:59", "late"))
        self.assertEqual([(t["done"], t["sent"]) for t in s["daily"]], [(False, [])] * 3)
        self.assertEqual([t["title"] for t in s["todo"]], ["open"])
        self.assertEqual(s["last_reset"], "2026-09-29")
        self.assertFalse(todoit.rollover(s, at(29, 9, 0)))  # same day: no-op
        self.assertFalse(todoit.rollover(s, at(28, 23, 0)))  # clock stepped backwards: no-op

    def test_weekly_rollover_on_monday_flags_unsent_misses(self):
        s = state(last_reset="2026-10-04", weekly=[task("late", "sun 23:59"),
                  task("warned", "fri 17:00", sent=["missed"]), task("done", "mon 17:00", done=True)])
        self.assertTrue(todoit.rollover(s, datetime(2026, 10, 5, 0, 2)))
        self.notify.assert_called_once()
        self.assertEqual(self.notify.call_args.args[:2], ("❌ missed · was due Sun 23:59", "late"))
        self.assertEqual([(t["done"], t["sent"]) for t in s["weekly"]], [(False, [])] * 3)
        self.assertEqual(s["last_reset"], "2026-10-05")

    def test_weekly_stays_checked_within_week(self):
        s = state(last_reset="2026-09-29", weekly=[task("done", "fri 17:00", done=True, sent=["soon"])])
        self.assertTrue(todoit.rollover(s, datetime(2026, 9, 30, 0, 2)))
        self.assertTrue(s["weekly"][0]["done"])
        self.assertEqual(s["weekly"][0]["sent"], ["soon"])
        self.notify.assert_not_called()

    def test_weekly_rollover_after_multi_week_gap(self):
        s = state(weekly=[task("done", "fri 17:00", done=True)])
        self.assertTrue(todoit.rollover(s, datetime(2026, 10, 20, 10, 0)))
        self.assertFalse(s["weekly"][0]["done"])

    def test_daily_added_after_its_time_stays_quiet_until_tomorrow(self):
        self.assertEqual(todoit.fresh_sent("daily", "17:00", at(28, 19, 0)), ["soon", "missed"])
        self.assertEqual(todoit.fresh_sent("daily", "20:00", at(28, 19, 0)), [])
        self.assertEqual(todoit.fresh_sent("todo", "2026-09-29 17:00", at(28, 19, 0)), [])
        s = state(daily=[task("late add", "17:00", sent=todoit.fresh_sent("daily", "17:00", at(28, 19, 0)))])
        todoit.check(s, at(28, 19, 5))
        todoit.rollover(s, at(29, 0, 1))
        self.notify.assert_not_called()

    def test_weekly_added_after_its_slot_stays_quiet_until_monday(self):
        self.assertEqual(todoit.fresh_sent("weekly", "mon 09:00", at(28, 10, 0)), ["soon", "missed"])
        self.assertEqual(todoit.fresh_sent("weekly", "fri 17:00", at(28, 10, 0)), [])


class Osascript(unittest.TestCase):
    def run_notify(self, returncode, stderr=""):
        done = subprocess.CompletedProcess([], returncode, "", stderr)
        with mock.patch.object(todoit.subprocess, "run", return_value=done) as run, \
                self.assertLogs("todoit", "INFO") as logs:
            todoit.notify('⏰ due 17:00', 'say "hi" -e', "Glass")
        return run.call_args.args[0], logs.output

    def test_title_is_passed_as_argv_data_not_script(self):
        argv, logs = self.run_notify(0)
        self.assertEqual(argv[-3:], ['⏰ due 17:00', 'say "hi" -e', "Glass"])
        self.assertNotIn("hi", " ".join(argv[:-3]))
        self.assertTrue(logs[0].startswith("INFO:todoit:banner:"))

    def test_failure_is_logged_as_error(self):
        _, logs = self.run_notify(1, "not authorized\n")
        self.assertIn("ERROR:todoit:banner failed (exit 1)", logs[0])
        self.assertIn("not authorized", logs[0])


class AskTask(unittest.TestCase):
    def ask(self, answers, kind="todo", task_=None):
        with mock.patch.object(todoit, "prompt", side_effect=answers), \
                mock.patch.object(todoit, "now", return_value=at(28, 19, 0)):
            return todoit.ask_task(None, kind, task_)

    def test_reprompts_until_due_parses(self):
        self.assertEqual(self.ask(["neon", "someday", "+9999999", "tomorrow"]), ("neon", "2026-09-29 17:00"))

    def test_rejects_new_todo_due_in_the_past(self):
        self.assertEqual(self.ask(["neon", "today", "today 21:00"]), ("neon", "2026-09-28 21:00"))

    def test_editing_title_of_overdue_todo_keeps_its_due(self):
        old = task("neon", "2026-09-27 17:00")
        self.assertEqual(self.ask(["neon form", "2026-09-27 17:00"], task_=old), ("neon form", "2026-09-27 17:00"))

    def test_cancel(self):
        self.assertIsNone(self.ask([""]))
        self.assertIsNone(self.ask(["neon", None]))


class Display(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(todoit, "C", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_labels(self):
        t = at(28, 16, 45)
        for kind, t_, want in [
            ("todo", task("a", "2026-09-28 17:00"), "today 17:00"),
            ("todo", task("a", "2026-09-29 09:00"), "tomorrow 09:00"),
            ("todo", task("a", "2026-10-02 17:00"), "Fri 17:00"),
            ("todo", task("a", "2026-10-15 17:00"), "10/15 17:00"),
            ("todo", task("a", "2026-09-27 17:00"), "OVERDUE 9/27 17:00"),
            ("daily", task("a", "12:00"), "missed 12:00"),
            ("daily", task("a", "12:00", done=True), "12:00"),
            ("daily", task("a", "17:00"), "17:00"),
            ("weekly", task("a", "fri 17:00"), "Fri 17:00"),
            ("weekly", task("a", "mon 09:00"), "missed Mon 09:00"),
        ]:
            self.assertEqual(todoit.label(kind, t_, t)[0], want)

    def test_proposal_lines_use_plain_titles_and_readable_due(self):
        s = state(todo=[task("[review PR](https://example.com)", "2026-09-27 17:00")])
        with mock.patch.object(todoit, "now", return_value=at(28, 19, 0)):
            self.assertEqual(todoit.proposal_line({"op": "add", "kind": "todo", "title": "[call mom](https://x.io)",
                                                   "due": "2026-10-02 09:00"}, s), '+ todo "call mom" due Fri 09:00')
            self.assertEqual(todoit.proposal_line({"op": "edit", "kind": "todo", "i": 0,
                                                   "title": "[review](https://x.io)", "due": "2026-09-29 10:00"}, s),
                             '~ todo "review PR": title → review, due → tomorrow 10:00')
            self.assertEqual(todoit.proposal_line({"op": "delete", "kind": "todo", "i": 0}, s),
                             '- todo "review PR"')

    def test_chat_strips_nul_before_drawing(self):
        scr = mock.Mock(**{"getmaxyx.return_value": (12, 40)})
        with mock.patch.object(todoit, "draw"), mock.patch.object(todoit, "put") as put:
            todoit.draw_chat(scr, state(), 0, [("agent", "hi\0 there")])
        scr.clrtobot.assert_called_once()
        self.assertIn("agent: hi there", [call.args[3] for call in put.call_args_list])

    def test_weekly_label_colors(self):
        with mock.patch.object(todoit, "C", side_effect=lambda n: n):
            self.assertEqual(todoit.label("weekly", task("a", "mon 17:00"), at(28, 16, 0))[1], todoit.YELLOW)
            self.assertEqual(todoit.label("weekly", task("a", "mon 09:00"), at(28, 16, 0))[1],
                             todoit.RED | todoit.curses.A_BOLD)
            self.assertEqual(todoit.label("weekly", task("a", "fri 17:00", done=True), at(28, 16, 0))[1],
                             todoit.GREEN | todoit.curses.A_DIM)

    def test_draw_weekly_section_and_empty_hint(self):
        scr = mock.Mock(**{"getmaxyx.return_value": (20, 90)})
        with mock.patch.object(todoit, "put") as put:
            todoit.draw(scr, state(), 0, at(28, 16, 0))
        calls = [c.args for c in put.call_args_list]
        self.assertIn(" WEEKLY 0/0 ", [c[3] for c in calls])
        self.assertIn("nothing here, press w to add", [c[3] for c in calls])
        note = " resets mondays "
        self.assertTrue(any(c[2:4] == (90 - len(note) - 1, note) for c in calls))

    def test_momentum_splits_due_today_from_overdue(self):
        s = state(daily=[task("done", "09:00", done=True), task("missed", "12:00"), task("later", "18:00")],
                  todo=[task("late", "2026-09-27 17:00"), task("tonight", "2026-09-28 20:00"),
                        task("next week", "2026-10-05 17:00"), task("early", "2026-10-05 17:00", done=True)])
        self.assertEqual(todoit.momentum(s, at(28, 16, 0)), (2, 7, 2, 2))
        self.assertEqual(todoit.momentum(state(), at(28, 16, 0)), (0, 0, 0, 0))

    def test_momentum_counts_weeklies(self):
        s = state(weekly=[task("done", "fri 17:00", done=True), task("missed", "mon 09:00")])
        self.assertEqual(todoit.momentum(s, at(28, 16, 0)), (1, 2, 0, 1))

    def test_shimmer_stays_on_the_filled_part_of_the_bar(self):
        scr = mock.Mock(**{"getmaxyx.return_value": (20, 90)})  # bar 30 wide
        s = state(daily=[task("a", "09:00", done=True), task("b", "18:00")])  # 1/2 done -> 15 filled
        with mock.patch.object(todoit, "put") as put, mock.patch.object(todoit, "now", return_value=at(28, 16, 0)):
            for f in range(40):
                todoit.shimmer(scr, s, f)
            drawn = {c.args[2] for c in put.call_args_list}
        self.assertEqual(drawn, set(range(todoit.BAR_X, todoit.BAR_X + 15)))  # sweeps every filled cell, never the dim rest

    def test_fit_counts_wide_chars_as_two_columns(self):
        self.assertEqual(todoit.fit("日本語abc", 5), "日本")  # 語 needs 2 more cols: stop, never skip ahead
        self.assertEqual(todoit.fit("review a PR", 6), "review")

    def test_rocket_explodes_at_apex_and_dead_particles_vanish(self):
        ps = [[10, 5, 0, 0, 999, 0, True], [3, 3, 0, 0, 1, 0, False]]
        with mock.patch.object(todoit, "put"):
            todoit.step(None, ps)
        self.assertEqual(len(ps), 60)
        self.assertFalse(any(p[6] for p in ps))


class Store(unittest.TestCase):
    def test_first_run_then_round_trip(self):
        with TemporaryDirectory() as d, mock.patch.object(todoit, "DB", Path(d) / "new" / "tasks.json"), \
                mock.patch.object(todoit, "now", return_value=at(28, 10, 0)):
            s = todoit.load()
            self.assertEqual(s, state())
            s["todo"].append(task("neon order form", "2026-09-29 17:00"))
            todoit.save(s)
            self.assertEqual(todoit.load(), s)
            self.assertEqual(list(todoit.DB.parent.glob("*.tmp")), [])

    def test_load_legacy_store_without_weekly(self):
        with TemporaryDirectory() as d, mock.patch.object(todoit, "DB", Path(d) / "tasks.json"), \
                mock.patch.object(todoit, "now", return_value=at(28, 10, 0)):
            old = state(last_reset="2026-09-27")
            old.pop("weekly")
            todoit.save(old)
            self.assertEqual(todoit.load()["weekly"], [])

    def test_reload_refuses_write_after_midnight_reshuffle(self):
        with TemporaryDirectory() as d, mock.patch.object(todoit, "DB", Path(d) / "tasks.json"), \
                mock.patch.object(todoit, "notify"), mock.patch.object(todoit, "now") as now:
            todoit.save(state(todo=[task("A", "2026-09-30 17:00", done=True), task("B", "2026-09-30 17:00")]))
            now.return_value = at(28, 23, 59)
            drawn = todoit.load()
            self.assertIsNotNone(todoit.reload(drawn))  # same day: safe to write by index
            now.return_value = at(29, 0, 5)
            self.assertIsNone(todoit.reload(drawn))  # rollover purged A, so index 1 no longer means B


class Agent(unittest.TestCase):
    def test_parse_reply_accepts_fences_and_rejects_garbage(self):
        self.assertEqual(todoit.parse_agent_reply('Sure:\n```json\n{"reply":"ok","ops":[]}\n```'),
                         {"reply": "ok", "ops": []})
        for raw in ("garbage", "{}", '{"reply":4,"ops":[]}', '{"reply":"ok","ops":{}}'):
            with self.assertRaises(ValueError, msg=raw):
                todoit.parse_agent_reply(raw)

    def test_prompt_has_time_tasks_without_sent_and_chat_history(self):
        s = state(daily=[task("stretch", "09:00", sent=["soon"])])
        with mock.patch.object(todoit, "now", return_value=at(28, 19, 0)):
            prompt = todoit.agent_prompt(s, [{"role": "you", "text": "hi"}], "move stretch")
        self.assertIn("2026-09-28 19:00 Monday", prompt)
        self.assertIn('"i": 0', prompt)
        self.assertIn('"role": "you"', prompt)
        self.assertIn("New user message: move stretch", prompt)
        self.assertNotIn("sent", prompt)
        self.assertIn("no tools", prompt)

    def test_validate_and_apply_original_indices_and_sent(self):
        s = state(daily=[task("stretch", "18:00", sent=["soon"]), task("walk", "21:00", sent=["soon"])],
                  todo=[task("A", "2026-09-30 17:00"), task("B", "2026-09-30 17:00"),
                        task("C", "2026-09-30 17:00", sent=["soon"])])
        raw = [{"op": "delete", "kind": "todo", "i": 0},
               {"op": "edit", "kind": "todo", "i": 2, "title": "C moved", "due": "fri 9:00"},
               {"op": "delete", "kind": "todo", "i": 1},
               {"op": "edit", "kind": "daily", "i": 0, "title": "stretch more"},
               {"op": "edit", "kind": "daily", "i": 1, "due": "17:00"},
               {"op": "add", "kind": "todo", "title": "new", "due": "tomorrow 8:00"},
               {"op": "add", "kind": "daily", "title": "early", "due": "16:00"}]
        valid, invalid = todoit.validate_ops(raw, s, at(28, 19, 0), set())
        self.assertEqual(invalid, [])
        self.assertEqual(valid[1]["due"], "2026-10-02 09:00")
        with TemporaryDirectory() as d, mock.patch.object(todoit, "DB", Path(d) / "tasks.json"), \
                mock.patch.object(todoit, "now", return_value=at(28, 19, 0)):
            todoit.save(s)
            with mock.patch.object(todoit, "save", wraps=todoit.save) as save:
                applied = todoit.apply_ops(s, valid)
                save.assert_called_once()
            self.assertEqual(todoit.load(), applied)
        self.assertEqual([(x["title"], x["due"]) for x in applied["todo"]],
                         [("C moved", "2026-10-02 09:00"), ("new", "2026-09-29 08:00")])
        self.assertEqual(applied["todo"][0]["sent"], [])
        self.assertEqual(applied["daily"][0]["sent"], ["soon"])
        self.assertEqual(applied["daily"][1]["sent"], ["soon", "missed"])
        self.assertEqual(applied["daily"][2]["sent"], ["soon", "missed"])
        self.assertFalse(applied["todo"][1]["done"])

    def test_validation_rejects_past_due_bad_types_and_duplicate_index(self):
        s = state(todo=[task("old", "2026-09-27 17:00")])
        raw = [{"op": "add", "kind": "todo", "title": "late", "due": "today 10:00"},
               {"op": "edit", "kind": "todo", "i": 0, "due": "today 10:00"},
               {"op": "edit", "kind": "todo", "i": 0, "title": "still old"},
               {"op": "delete", "kind": "todo", "i": 0},
               {"op": "edit", "kind": "todo", "i": True, "done": 1}]
        valid, invalid = todoit.validate_ops(raw, s, at(28, 19, 0), set())
        self.assertEqual(valid, [{"op": "edit", "kind": "todo", "i": 0, "title": "still old"}])
        self.assertEqual([why for _, why in invalid],
                         ["due is in the past", "due is in the past", "duplicate task index", "index out of range"])
        with self.assertRaisesRegex(ValueError, "done must be boolean"):
            todoit.validate_op({"op": "edit", "kind": "todo", "i": 0, "done": 1},
                               s, at(28, 19, 0), set())

    def test_validation_rejects_malformed_ops(self):
        s = state(todo=[task("old", "2026-09-30 17:00")])
        for raw, want in [("add x", "op must be an object"),
                          ({"op": "add", "kind": "monthly", "title": "x", "due": "fri"}, "invalid op or kind"),
                          ({"op": "edit", "kind": "todo", "i": 0}, "edit needs a field"),
                          ({"op": "add", "kind": "todo", "title": "  ", "due": "fri"}, "title required"),
                          ({"op": "add", "kind": "todo", "title": "x", "due": 5}, "due must be text"),
                          ({"op": "add", "kind": "todo", "title": "x", "due": "someday"}, "isoformat")]:
            with self.subTest(want=want), self.assertRaisesRegex(ValueError, want):
                todoit.validate_op(raw, s, at(28, 19, 0), set())

    def test_weekly_ops_round_trip(self):
        s = state(weekly=[task("groceries", "sun 10:00")])
        with mock.patch.object(todoit, "now", return_value=at(28, 19, 0)), \
                mock.patch.object(todoit, "C", return_value=0):
            self.assertIn('"weekly": [{"i": 0, "title": "groceries", "due": "sun 10:00"', todoit.agent_prompt(s, [], "hi"))
            valid, invalid = todoit.validate_ops([{"op": "add", "kind": "weekly", "title": "1:1", "due": "fri 9:00"},
                                                  {"op": "edit", "kind": "weekly", "i": 0, "due": "sat"}],
                                                 s, at(28, 19, 0), set())
            self.assertEqual(invalid, [])
            self.assertEqual([op["due"] for op in valid], ["fri 09:00", "sat 17:00"])
            self.assertEqual(todoit.proposal_line(valid[0], s), '+ weekly "1:1" due Fri 09:00')
            with TemporaryDirectory() as d, mock.patch.object(todoit, "DB", Path(d) / "tasks.json"):
                todoit.save(s)
                self.assertEqual([x["due"] for x in todoit.apply_ops(s, valid)["weekly"]], ["sat 17:00", "fri 09:00"])

    def test_edit_done_is_applied(self):
        s = state(todo=[task("old", "2026-09-30 17:00")])
        op = todoit.validate_op({"op": "edit", "kind": "todo", "i": 0, "done": True}, s, at(28, 19, 0), set())
        with TemporaryDirectory() as d, mock.patch.object(todoit, "DB", Path(d) / "tasks.json"), \
                mock.patch.object(todoit, "now", return_value=at(28, 19, 0)):
            todoit.save(s)
            self.assertTrue(todoit.apply_ops(s, [op])["todo"][0]["done"])

    def test_propose_returns_call_errors(self):
        good = {"op": "add", "kind": "daily", "title": "good", "due": "9:00"}
        late = {"op": "add", "kind": "todo", "title": "late", "due": "today 10:00"}
        first = subprocess.CompletedProcess([], 0, json.dumps({"reply": "ok", "ops": [good, late]}), "")
        for runs, want_ops, want_skipped in [([subprocess.TimeoutExpired("agent", 120)], 0, 0),  # first call fails
                                             ([first, subprocess.TimeoutExpired("agent", 120)], 1, 1)]:  # retry fails
            with self.subTest(calls=len(runs)), mock.patch.object(todoit.subprocess, "run", side_effect=runs):
                _, ops, skipped, error = todoit.propose(["agent"], "base", state(), at(28, 19, 0))
            self.assertEqual((len(ops), len(skipped), error), (want_ops, want_skipped, "agent timed out"))

    def test_edit_unchanged_due_preserves_sent(self):
        s = state(todo=[task("old", "2026-09-29 17:00", sent=["soon"])])
        raw = {"op": "edit", "kind": "todo", "i": 0, "title": "new", "due": "2026-09-29 17:00"}
        valid, invalid = todoit.validate_ops([raw], s, at(28, 19, 0), set())
        self.assertEqual(invalid, [])
        with TemporaryDirectory() as d, mock.patch.object(todoit, "DB", Path(d) / "tasks.json"), \
                mock.patch.object(todoit, "now", return_value=at(28, 19, 0)):
            todoit.save(s)
            applied = todoit.apply_ops(s, valid)
        self.assertEqual(applied["todo"][0]["sent"], ["soon"])

    def test_edit_overdue_title_keeps_due(self):
        s = state(todo=[task("old", "2026-09-27 17:00", sent=["missed"])])
        raw = {"op": "edit", "kind": "todo", "i": 0, "title": "new", "due": "2026-09-27 17:00"}
        valid, invalid = todoit.validate_ops([raw], s, at(28, 19, 0), set())
        self.assertEqual(invalid, [])
        self.assertEqual(valid[0]["due"], "2026-09-27 17:00")

    def test_nonprintable_title_is_rejected(self):
        raw = {"op": "add", "kind": "todo", "title": "bad\0title", "due": "tomorrow"}
        self.assertEqual(todoit.validate_ops([raw], state(), at(28, 19, 0), set())[1][0][1],
                         "title has control characters")

    def test_retry_merges_valid_op_with_corrected_replacement(self):
        first = {"reply": "two changes", "ops": [
            {"op": "add", "kind": "todo", "title": "good", "due": "tomorrow"},
            {"op": "add", "kind": "todo", "title": "late", "due": "today 10:00"}]}
        second = {"reply": "fixed", "ops": [{"op": "add", "kind": "todo", "title": "late", "due": "fri 9:00"}]}
        done = [subprocess.CompletedProcess([], 0, json.dumps(x), "") for x in (first, second)]
        with mock.patch.object(todoit.subprocess, "run", side_effect=done) as run:
            reply, ops, skipped, error = todoit.propose(["agent"], "base prompt", state(), at(28, 19, 0))
        self.assertEqual((reply, skipped, error), ("two changes", [], None))
        self.assertEqual([op["due"] for op in ops], ["2026-09-29 17:00", "2026-10-02 09:00"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].kwargs["input"], "base prompt")
        self.assertIn("base prompt", run.call_args_list[1].kwargs["input"])
        self.assertIn("Invalid ops", run.call_args_list[1].kwargs["input"])
        self.assertIn("due is in the past", run.call_args_list[1].kwargs["input"])

    def test_retry_reusing_valid_index_is_skipped(self):
        s = state(todo=[task("A", "2026-09-30 17:00"), task("B", "2026-09-30 17:00")])
        first = {"reply": "edit", "ops": [{"op": "edit", "kind": "todo", "i": 0, "title": "A1"},
                                          {"op": "edit", "kind": "todo", "i": 1, "due": "today 10:00"}]}
        duplicate = {"reply": "retry", "ops": [{"op": "edit", "kind": "todo", "i": 0, "title": "A2"}]}
        done = [subprocess.CompletedProcess([], 0, json.dumps(x), "") for x in [first] + [duplicate] * 3]
        with mock.patch.object(todoit.subprocess, "run", side_effect=done):
            _, ops, skipped, error = todoit.propose(["agent"], "base", s, at(28, 19, 0))
        self.assertEqual([op["title"] for op in ops], ["A1"])
        self.assertEqual(skipped[0][1], "duplicate task index")
        self.assertIsNone(error)

    def test_retry_keeps_unreplaced_error_and_valid_replacement(self):
        invalid = [{"op": "add", "kind": "todo", "title": title, "due": "today 10:00"}
                   for title in ("A", "B")]
        first = {"reply": "edit", "ops": invalid}
        correction = {"reply": "retry", "ops": [{**invalid[0], "due": "tomorrow"}]}
        empty = {"reply": "retry", "ops": []}
        done = [subprocess.CompletedProcess([], 0, json.dumps(x), "") for x in [first, correction, empty, empty]]
        with mock.patch.object(todoit.subprocess, "run", side_effect=done):
            _, ops, skipped, error = todoit.propose(["agent"], "base", state(), at(28, 19, 0))
        self.assertEqual([op["title"] for op in ops], ["A"])
        self.assertEqual(skipped, [(invalid[1], "due is in the past")])
        self.assertIsNone(error)

    def test_retry_ignores_extra_replacements(self):
        first = {"reply": "edit", "ops": [{"op": "add", "kind": "todo", "title": "A", "due": "today 10:00"}]}
        correction = {"reply": "retry", "ops": [{"op": "add", "kind": "todo", "title": title,
                                                 "due": "tomorrow"} for title in ("A", "extra")]}
        done = [subprocess.CompletedProcess([], 0, json.dumps(x), "") for x in (first, correction)]
        with mock.patch.object(todoit.subprocess, "run", side_effect=done):
            _, ops, skipped, error = todoit.propose(["agent"], "base", state(), at(28, 19, 0))
        self.assertEqual([op["title"] for op in ops], ["A"])
        self.assertEqual(skipped, [])
        self.assertIsNone(error)

    def test_retry_stops_after_three_failures_and_skips(self):
        first = {"reply": "one good", "ops": [
            {"op": "add", "kind": "daily", "title": "good", "due": "9:00"},
            {"op": "add", "kind": "todo", "title": "late", "due": "today 10:00"}]}
        bad = {"reply": "still late", "ops": [first["ops"][1]]}
        done = [subprocess.CompletedProcess([], 0, json.dumps(x), "") for x in [first] + [bad] * 3]
        with mock.patch.object(todoit.subprocess, "run", side_effect=done) as run:
            _, ops, skipped, error = todoit.propose(["agent"], "base", state(), at(28, 19, 0))
        self.assertEqual(run.call_count, 4)
        self.assertEqual(len(ops), 1)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0][1], "due is in the past")
        self.assertIsNone(error)

    def test_config_and_stdin_command(self):
        with TemporaryDirectory() as d, mock.patch.object(todoit, "CONFIG", Path(d) / "config.json"):
            self.assertEqual(todoit.agent_argv(), todoit.DEFAULT_AGENT)
            todoit.CONFIG.write_text("{}")
            self.assertEqual(todoit.agent_argv(), todoit.DEFAULT_AGENT)
            todoit.CONFIG.write_text('{"agent":["codex","exec","-"]}')
            self.assertEqual(todoit.agent_argv(), ["codex", "exec", "-"])
            todoit.CONFIG.write_text("[]")
            with self.assertRaises(ValueError):
                todoit.agent_argv()
            todoit.CONFIG.write_text("{")
            with self.assertRaises(ValueError):
                todoit.agent_argv()
        done = subprocess.CompletedProcess([], 0, '{"reply":"ok","ops":[]}', "")
        with mock.patch.object(todoit.subprocess, "run", return_value=done) as run:
            self.assertEqual(todoit.call_agent(["codex", "exec", "-"], "the prompt")[0]["reply"], "ok")
        run.assert_called_once_with(["codex", "exec", "-"], input="the prompt", capture_output=True,
                                    text=True, timeout=120, check=False)

    def test_command_errors_are_returned(self):
        for result, want in [(FileNotFoundError("not found"), "not found"),
                             (PermissionError("permission denied"), "permission denied"),
                             (subprocess.TimeoutExpired("agent", 120), "timed out"),
                             (subprocess.CompletedProcess([], 2, "", "first\nlast\n"), "last"),
                             (subprocess.CompletedProcess([], 0, "sorry, can't help", ""), "invalid agent reply")]:
            kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with self.subTest(want=want), mock.patch.object(todoit.subprocess, "run", **kwargs):
                _, error = todoit.call_agent(["agent"], "prompt")
                self.assertIn(want, error)

    def test_chat_logs_agent_error_once(self):
        with mock.patch.object(todoit, "draw_chat"), mock.patch.object(todoit, "load", return_value=state()), \
                mock.patch.object(todoit, "prompt", side_effect=["hi", ""]), \
                mock.patch.object(todoit, "agent_argv", return_value=["agent"]), \
                mock.patch.object(todoit, "propose", return_value=(None, [], [], "agent timed out")), \
                mock.patch.object(todoit.curses, "flushinp"), self.assertLogs("todoit", "ERROR") as logs:
            todoit.chat(mock.Mock(), 0)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("agent timed out", logs.output[0])

    def test_chat_shows_retry_error_and_skipped_without_confirm(self):
        skipped = [({"op": "add", "title": "late"}, "due is in the past")]
        with mock.patch.object(todoit, "draw_chat") as draw, mock.patch.object(todoit, "load", return_value=state()), \
                mock.patch.object(todoit, "prompt", side_effect=["hi", ""]), \
                mock.patch.object(todoit, "agent_argv", return_value=["agent"]), \
                mock.patch.object(todoit, "propose", return_value=("ok", [], skipped, "agent timed out")), \
                mock.patch.object(todoit.curses, "flushinp"), mock.patch.object(todoit, "confirm") as confirm:
            todoit.chat(mock.Mock(), 0)
        confirm.assert_not_called()
        self.assertEqual(draw.call_args.args[3][1:], [
            ("agent", "ok"), (None, "✗ agent timed out"),
            (None, '✗ skipped: {"op": "add", "title": "late"} (due is in the past)')])

    def test_malformed_config_is_shown_in_chat(self):
        with TemporaryDirectory() as d, mock.patch.object(todoit, "CONFIG", Path(d) / "config.json"), \
                mock.patch.object(todoit, "draw_chat") as draw, \
                mock.patch.object(todoit, "prompt", side_effect=["hello", ""]), \
                mock.patch.object(todoit, "now", return_value=at(28, 19, 0)), \
                mock.patch.object(todoit, "load", return_value=state()), \
                mock.patch.object(todoit, "propose") as propose, self.assertLogs("todoit", "ERROR"):
            todoit.CONFIG.write_text("{")
            todoit.chat(mock.Mock(), 0)
        propose.assert_not_called()
        self.assertTrue(any("config:" in line for _, line in draw.call_args.args[3]))

    def test_apply_refuses_midnight_rollover(self):
        with TemporaryDirectory() as d, mock.patch.object(todoit, "DB", Path(d) / "tasks.json"), \
                mock.patch.object(todoit, "notify"), mock.patch.object(todoit, "now") as now:
            todoit.save(state(todo=[task("done", "2026-09-28 20:00", done=True)]))
            now.return_value = at(28, 23, 59)
            drawn = todoit.load()
            now.return_value = at(29, 0, 1)
            self.assertIsNone(todoit.apply_ops(drawn, [{"op": "add", "kind": "todo", "title": "new",
                                                      "due": "2026-09-30 17:00"}]))
            self.assertEqual(todoit.load()["todo"], [])

    def test_chat_applies_only_after_confirmation(self):
        s = state()
        ops = [{"op": "add", "kind": "todo", "title": "new", "due": "2026-09-29 17:00"}]
        for approved in (False, True):
            events = []
            with self.subTest(approved=approved), mock.patch.object(todoit, "draw_chat") as draw, \
                    mock.patch.object(todoit, "prompt", side_effect=["add new", ""]), \
                    mock.patch.object(todoit, "now", return_value=at(28, 19, 0)), \
                    mock.patch.object(todoit, "agent_argv", return_value=["agent"]), \
                    mock.patch.object(todoit, "propose", return_value=("ok", ops, [], None)), \
                    mock.patch.object(todoit, "C", return_value=0), \
                    mock.patch.object(todoit.curses, "flushinp", side_effect=lambda: events.append("flush")), \
                    mock.patch.object(todoit, "confirm", side_effect=lambda _scr, _msg: events.append("confirm") or approved), \
                    mock.patch.object(todoit, "apply_ops", return_value=s) as apply, \
                    mock.patch.object(todoit, "load", return_value=s):
                todoit.chat(mock.Mock(), 0)
            self.assertEqual(apply.call_count, int(approved))
            self.assertEqual(events, ["flush", "confirm"])
            self.assertIn((None, "applied 1 change" if approved else "not applied"), draw.call_args.args[3])


if __name__ == "__main__":
    unittest.main()
