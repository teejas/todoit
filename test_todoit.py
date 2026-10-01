import json
import os
import shlex
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


def task(title, due, done=False, sent=None, done_on=None):
    t = {"title": title, "due": due, "done": done, "sent": sent or []}
    if done_on is not None:
        t["done_on"] = done_on
    return t


def state(**kw):
    return {"last_reset": "2026-09-28", "daily": [], "weekly": [], "todo": [], **kw}


TASK = task("Review [PR 41](https://x.io/pr/41)!", "2026-09-30 17:00")
CHECKOUT, FEAT, WT = "/Users/t/vapi/repo/main", "/Users/t/vapi/repo/feat", "/Users/t/vapi/repo/review-pr-41"
MINI_CHECKOUT, MINI_WT = "/Users/t.s/vapi/mono/main", "/Users/t.s/vapi/mono/review-pr-41"  # remote user's home differs


def ok(result):
    return subprocess.CompletedProcess([], 0, json.dumps({"result": result, "id": "x"}), "")


LIST = ok({"workspaces": [{"workspace_id": "wN", "label": "repo",
                          "worktree": {"checkout_path": CHECKOUT, "is_linked_worktree": False}},
                          {"workspace_id": "wX", "label": "plain", "worktree": None},
                          {"workspace_id": "wL", "label": "feat",
                           "worktree": {"checkout_path": FEAT, "is_linked_worktree": True}}]})
MINI = {"id": "1d99", "label": "mac-mini", "target": "t@mini", "session": "default", "enabled": True, "selected": False}
MACHINES = subprocess.CompletedProcess([], 0, json.dumps([MINI, {**MINI, "id": "2", "label": "old", "target": "t@old", "enabled": False}]), "")  # bare array
NO_MACHINES = subprocess.CompletedProcess([], 0, "[]\n", "")
MINI_LIST = ok({"workspaces": [{"workspace_id": "w1H", "label": "arch-world", "worktree": {"checkout_path": MINI_CHECKOUT, "is_linked_worktree": False}},
                               {"workspace_id": "w17", "label": "load tests", "worktree": {"checkout_path": "/Users/t.s/vapi/mono/tests", "is_linked_worktree": True}}]})
DOWN = subprocess.CompletedProcess([], 255, "", "ssh: connect to host mini port 22: Operation timed out\n")
GARBAGE = subprocess.CompletedProcess([], 0, "not json", "")
CREATED = ok({"root_pane": {"pane_id": "w9:p1"}, "tab": {"tab_id": "w9:t1"}})
RAN = subprocess.CompletedProcess([], 0, "", "")
WS_LIST = [["herdr", "workspace", "list"], ["herdr", "machine", "list", "--json"]]
SSH = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "t@mini"]
def remote(*args): return SSH + [shlex.join(["herdr", "--session", "default", *args])]
LOCAL_ITEMS = ["repo  ~/vapi/repo/main", "plain", "feat  ~/vapi/repo/feat"]
MINI_ITEMS = ["mac-mini · arch-world  ~/vapi/mono/main", "mac-mini · load tests  ~/vapi/mono/tests"]
TAB_WN = ["herdr", "tab", "create", "--workspace", "wN", "--cwd", CHECKOUT, "--label", "Review PR 41!", "--no-focus"]
TAB_WX = ["herdr", "tab", "create", "--workspace", "wX", "--label", "Review PR 41!", "--no-focus"]
def run_line(harness, model, p="Fix it"): return ["herdr", "pane", "run", "w9:p1", shlex.join([harness, "--model", model, "--", p])]


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
        self.assertIsNone(self.ask([None]))
        self.assertIsNone(self.ask(["neon", None, None]))

    def test_due_esc_goes_back_to_title(self):
        with mock.patch.object(todoit, "prompt", side_effect=["neon", "someday", None, "neon 2", "tomorrow"]) as prompt, \
                mock.patch.object(todoit, "now", return_value=at(28, 19, 0)):
            self.assertEqual(todoit.ask_task(None, "todo"), ("neon 2", "2026-09-29 17:00"))
        hint = todoit.HINT["todo"]
        self.assertEqual(prompt.call_args_list, [
            mock.call(None, "title: ", "", esc_empty=True),
            mock.call(None, f"{hint}: ", "", esc_empty=True),
            mock.call(None, f"✗ invalid · {hint}: ", "someday", esc_empty=True),
            mock.call(None, "title: ", "neon", esc_empty=True),
            mock.call(None, f"{hint}: ", "", esc_empty=True),
        ])


class Edit(unittest.TestCase):
    def test_chars(self):
        left, right = todoit.curses.KEY_LEFT, todoit.curses.KEY_RIGHT
        cases = [("x", 1, "axbc", 2), ("日", 3, "abc日", 4),
                 (left, 0, "abc", 0), (left, 2, "abc", 1),
                 (right, 1, "abc", 2), (right, 3, "abc", 3), ("\n", 1, "abc", 1),
                 (todoit.curses.KEY_F1, 1, "abc", 1)]  # unknown int keys are ignored
        cases += [(ch, pos, text, want) for ch in ("\x7f", "\b", todoit.curses.KEY_BACKSPACE)
                  for pos, text, want in [(2, "ac", 1), (0, "abc", 0)]]
        with mock.patch.object(todoit.curses, "keyname", return_value=b"KEY_F(1)"):
            for ch, pos, text, want in cases:
                with self.subTest(ch=ch, pos=pos):
                    self.assertEqual(todoit.edit(mock.Mock(), ch, "abc", pos, 0), (text, want, None))

    def test_words(self):
        text = "buy oat-milk later"
        left = [(0, 0), (4, 0), (8, 4), (12, 8), (18, 13)]
        right = [(0, 3), (3, 7), (7, 12), (12, 18), (18, 18)]
        for keys, name, cases in [([1001], b"kLFT3", left), ([1002], b"kRIT3", right),
                                  (["\x1b", "b"], b"", left), (["\x1b", "f"], b"", right)]:
            for pos, want in cases:
                scr = mock.Mock(**{"get_wch.side_effect": keys[1:]})
                with self.subTest(keys=keys, pos=pos), mock.patch.object(todoit.curses, "keyname", return_value=name):
                    self.assertEqual(todoit.edit(scr, keys[0], text, pos, 0), (text, want, None))
                if keys[0] == "\x1b":
                    self.assertEqual(scr.timeout.call_args_list, [mock.call(0), mock.call(-1)])

    def test_alt_backspace_and_ignored_chords(self):
        for ch in ("\x7f", "\b", todoit.curses.KEY_BACKSPACE, "x", todoit.curses.KEY_LEFT):
            for pos in (0, 12):
                scr = mock.Mock(**{"get_wch.return_value": ch})
                deleted = ch in ("\x7f", "\b", todoit.curses.KEY_BACKSPACE) and pos
                want = ("buy oat- later", 8) if deleted else ("buy oat-milk later", pos)
                with self.subTest(ch=ch, pos=pos), mock.patch.object(todoit.curses, "keyname", return_value=b""):
                    self.assertEqual(todoit.edit(scr, "\x1b", "buy oat-milk later", pos, 0), (*want, None))
                self.assertEqual(scr.timeout.call_args_list, [mock.call(0), mock.call(-1)])

    def test_esc(self):
        for last, t, want in [(None, 0, ("", 0, 0)), (0, 1, ("", 0, 1)), (0, 0.1, (None, 0, None))]:
            scr = mock.Mock(**{"get_wch.side_effect": todoit.curses.error()})
            with mock.patch.object(todoit.time, "monotonic", return_value=t):
                self.assertEqual(todoit.edit(scr, "\x1b", "old", 2, last), want)
            self.assertEqual(scr.timeout.call_args_list, [mock.call(0), mock.call(-1)])


class Prompt(unittest.TestCase):
    def edit(self, keys, text="", times=(), width=80, **kw):
        scr = mock.Mock()
        scr.getmaxyx.return_value = (5, width)
        scr.get_wch.side_effect = keys
        with mock.patch.object(todoit, "C", return_value=0), \
                mock.patch.object(todoit.curses, "curs_set"), \
                mock.patch.object(todoit.curses, "keyname", return_value=b""), \
                mock.patch.object(todoit.time, "monotonic", side_effect=times):
            return todoit.prompt(scr, "title: ", text, **kw), scr

    def test_cursor_and_nav_edges(self):
        left, right = todoit.curses.KEY_LEFT, todoit.curses.KEY_RIGHT
        for keys, text, nav, want in [([left, "x", "\n"], "ab", False, "axb"),
                                     ([left, "x", "\n"], "ab", True, "axb"),
                                     ([left, left, right, "x", "\n"], "ab", True, "axb"),
                                     ([right], "ab", True, "ab"), ([left], "", True, todoit.BACK),
                                     ([left, "x", "\n"], "", False, "x")]:
            keep = ["saved"]
            with self.subTest(keys=keys, nav=nav):
                result, scr = self.edit(keys, text, keep=keep, nav=nav)
                self.assertEqual(result, want)
                self.assertEqual(keep, [""] if want is todoit.BACK else ["saved"])
                scr.timeout.assert_called_with(1000)

    def test_cursor_window(self):
        text = "0123456789" * 3
        for n, shown, x in [(0, text[12:], 23), (20, text[10:29], 5), (30, text[:19], 5)]:
            result, scr = self.edit([todoit.curses.KEY_LEFT] * n + ["\n"], text, width=24)
            self.assertEqual(result, text)
            self.assertEqual(scr.addstr.call_args.args[2], shown)
            scr.move.assert_called_with(4, x)

    def test_on_text_before_draw(self):
        events = []
        with mock.patch.object(todoit, "put", side_effect=lambda *args: events.append(None)):
            result, _ = self.edit([todoit.curses.KEY_LEFT, "x", "\n"], "ab", on_text=events.append)
        self.assertEqual(result, "axb")
        self.assertEqual(events, ["ab", None, None, "ab", None, None, "axb", None, None])

    def test_word_delete(self):
        for backspace in ("\x7f", "\b", todoit.curses.KEY_BACKSPACE):
            keys = list("buy oat-milk") + ["\x1b", backspace, "\x1b", backspace, "\x1b", "x",
                                           "\x7f", todoit.curses.KEY_BACKSPACE, "\n"]
            self.assertEqual(self.edit(keys)[0], "bu")

    def test_esc_clears(self):
        for text in ("old", ""):
            keys = ["\x1b", todoit.curses.error()] + list("new") + ["\n"]
            self.assertEqual(self.edit(keys, text, [0])[0], "new")

    def test_esc_empty(self):
        esc = ["\x1b", todoit.curses.error()]
        keys = ["a"] + esc + esc
        result, scr = self.edit(keys, times=[0, 5], esc_empty=True)
        self.assertIsNone(result)
        self.assertEqual(scr.get_wch.call_count, len(keys))
        self.assertEqual(self.edit(keys + ["x", "\n"], times=[0, 5])[0], "x")

    def test_word_delete_empty(self):
        self.assertEqual(self.edit(["\x1b", "\x7f", "x", "\n"], esc_empty=True)[0], "x")
        for ch in ("b", "f", "x"):
            self.assertEqual(self.edit(["\x1b", ch, "x", "\n"], esc_empty=True)[0], "x")

    def test_double_esc(self):
        esc = ["\x1b", todoit.curses.error()]
        keys = esc + ["a"] + esc + esc + esc
        result, scr = self.edit(keys, "old", [0, 0.1, 1, 1.1])
        self.assertIsNone(result)
        self.assertEqual(scr.get_wch.call_count, len(keys))


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
            ("daily", task("a", "12:00", done=True, done_on="2026-09-28"), "12:00"),  # date-only stamp
            ("daily", task("a", "12:00", done=True, done_on="2026-09-28 11:42"), "done 11:42"),
            ("weekly", task("a", "fri 17:00", done=True, done_on="2026-09-26 18:05"), "done Sat 18:05"),
            ("daily", task("a", "17:00"), "17:00"),
            ("weekly", task("a", "fri 17:00"), "Fri 17:00"),
            ("weekly", task("a", "mon 09:00"), "missed Mon 09:00"),
        ]:
            self.assertEqual(todoit.label(kind, t_, t)[0], want)

    def test_draw_panel_header_and_focus_window(self):
        scr = mock.Mock(**{"getmaxyx.return_value": (12, 40)})
        with mock.patch.object(todoit, "draw"), mock.patch.object(todoit, "put") as put:
            todoit.draw_panel(scr, state(), 0, "t", list("abcde"), focus=4)
        texts = [c.args[3] for c in put.call_args_list]
        self.assertIn(' spawn agent · "t" ', texts)
        self.assertEqual([x for x in texts if x in "abcde"], ["a", "e"])
        scr.clrtobot.assert_called_once()
        scr.refresh.assert_called_once()

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
        self.assertIn("0/0 today", [c[3] for c in calls])
        self.assertIn(" WEEKLY 0/0 ", [c[3] for c in calls])
        self.assertIn("nothing here, press w to add", [c[3] for c in calls])
        note = " resets mondays "
        self.assertTrue(any(c[2:4] == (90 - len(note) - 1, note) for c in calls))

    def test_momentum_splits_due_today_from_overdue(self):
        s = state(daily=[task("done", "09:00", done=True), task("missed", "12:00"), task("later", "18:00")],
                  todo=[task("late", "2026-09-27 17:00"), task("tonight", "2026-09-28 20:00"),
                        task("next week", "2026-10-05 17:00"),
                        task("early", "2026-10-05 17:00", done=True, done_on="2026-09-28"),
                        task("legacy", "2026-10-05 17:00", done=True)])
        self.assertEqual(todoit.momentum(s, at(28, 16, 0)), (2, 6, 2, 2))
        self.assertEqual(todoit.momentum(state(), at(28, 16, 0)), (0, 0, 0, 0))

    def test_momentum_counts_weeklies(self):
        s = state(weekly=[task("done", "fri 17:00", done=True), task("missed", "mon 09:00"),
                          task("today", "mon 17:00", done=True)])
        self.assertEqual(todoit.momentum(s, at(28, 16, 0)), (1, 2, 0, 1))
        for due, done, done_on, want in [
            ("wed 17:00", False, None, (0, 1, 1, 0)),
            ("mon 17:00", True, "2026-09-28", (0, 0, 0, 0)),
            ("mon 17:00", True, "2026-09-30", (1, 1, 0, 0)),
            ("mon 17:00", False, None, (0, 1, 0, 1)),
            ("fri 17:00", False, None, (0, 0, 0, 0)),
            ("fri 17:00", True, "2026-09-30", (1, 1, 0, 0)),
            ("fri 17:00", True, "2026-09-30 08:15", (1, 1, 0, 0)),
            ("mon 17:00", True, "2026-09-28 17:30", (0, 0, 0, 0)),
        ]:
            self.assertEqual(todoit.momentum(state(weekly=[task("weekly", due, done, done_on=done_on)]),
                                             at(30, 12, 0)), want)
        s = state(weekly=[task("late", "mon 09:00")])
        self.assertEqual(todoit.momentum(s, at(30, 12, 0)), (0, 1, 0, 1))
        s["weekly"][0]["done"] = True
        s["weekly"][0]["done_on"] = "2026-09-30"
        self.assertEqual(todoit.momentum(s, at(30, 12, 0)), (1, 1, 0, 0))

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


class Navigation(unittest.TestCase):
    def test_section_start(self):
        rows = [("daily", 0), ("daily", 1), ("weekly", 0), ("todo", 0), ("todo", 1), ("todo", 2)]
        skip = rows[:2] + rows[3:]
        for rs, down, want in [(rows, True, [2, 2, 3, 5, 5, 5]), (rows, False, [0, 0, 0, 2, 3, 3]),
                               (skip, True, [2, 2, 4, 4, 4]), (skip, False, [0, 0, 0, 2, 2]),
                               ([], True, [0]), ([], False, [0])]:
            for cur, n in enumerate(want):
                self.assertEqual(todoit.section_start(rs, cur, down), n, (rs, cur, down))

    def test_find(self):
        s = state(daily=[task("walk", "09:00"), task("Review [PR 41](https://x.io/hidden)", "17:00")],
                  weekly=[task("review PR notes", "fri 17:00")], todo=[task("last", "2026-09-30 17:00")])
        rows = [("daily", 0), ("daily", 1), ("weekly", 0), ("todo", 0)]
        for rs, cur, q, want in [(rows, 3, "WALK", 0), (rows, 1, "PR 41", 1),
                                (rows, 1, " review pR ", 2), (rows, 2, "pR REView", 1),
                                (rows, 0, "41 revIEw", 1), (rows, 0, "hidden", None),
                                (rows, 0, "https", None), (rows, 0, "missing", None), ([], 0, "walk", None)]:
            self.assertEqual(todoit.find(s, rs, cur, q), want, (rs, cur, q))

    def test_live_search(self):
        s = state(daily=[task("alpha", "09:00"), task("beta", "10:00"), task("alpha two", "11:00")])
        miss = "✗ no match: zzz"
        cases = [(0, [(["", "a", "al", "alpha"], "alpha")], "/nq",
                  [(0, None), (0, None), (1, None), (2, None), (2, None), (2, None), (0, None)]),
                 (2, [(["alpha"], "alpha")], "/q", [(0, None), (1, None), (2, None), (0, None), (0, None)]),
                 (1, [(["alpha", "zzz"], "zzz")], "/nq",
                  [(0, None), (1, None), (2, None), (1, None), (1, miss), (1, miss)]),
                 (0, [(["alpha"], "alpha"), (["beta"], None)], "//nq",
                  [(0, None), (2, None), (2, None), (1, None), (2, None), (0, None)]),
                 (0, [(["alpha"], "alpha"), (["beta", ""], "")], "//nq",
                  [(0, None), (2, None), (2, None), (1, None), (2, None), (2, None), (0, None)])]
        for start, answers, keys, want in cases:
            replies = iter(answers)
            scr = mock.Mock(**{"getch.side_effect": list(map(ord, "j" * start + keys))})

            def prompt(scr_, label_, esc_empty=False, on_text=None):
                self.assertEqual((scr_, label_, esc_empty), (scr, "search: ", True))
                typed, answer = next(replies)
                for q in typed:
                    on_text(q)
                return answer

            with self.subTest(start=start, answers=answers), \
                    mock.patch.multiple(todoit.curses, curs_set=mock.DEFAULT, set_escdelay=mock.DEFAULT,
                                        use_default_colors=mock.DEFAULT, init_pair=mock.DEFAULT), \
                    mock.patch.object(todoit, "load", return_value=s), \
                    mock.patch.object(todoit, "now", return_value=at(28, 10, 0)), \
                    mock.patch.object(todoit, "prompt", side_effect=prompt), mock.patch.object(todoit, "draw") as draw:
                todoit.tui(scr)
            self.assertEqual([(c.args[2], c.args[4] if len(c.args) > 4 else None) for c in draw.call_args_list], want)
            self.assertTrue(all(c.args[:2] == (scr, s) and c.args[3] == at(28, 10, 0) for c in draw.call_args_list))


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


class Spawn(unittest.TestCase):
    def setUp(self):
        self.scr = mock.Mock(**{"getmaxyx.return_value": (24, 80)})   # spawn reads getmaxyx()[1]; bare Mock is not subscriptable
        self.panel = mock.patch.object(todoit, "draw_panel").start()
        mock.patch.object(todoit.curses, "flushinp").start()
        mock.patch.dict(os.environ, {"HERDR_ENV": "1"}).start()
        self.addCleanup(mock.patch.stopall)

    def spawn(self, picks, prompts, drafted=("Fix it", None), runs=()):
        """Drive todoit.spawn with scripted picker/prompt/draft/herdr answers; returns (result, herdr argvs, mocks)."""
        with mock.patch.object(todoit, "pick", side_effect=picks) as pick, \
                mock.patch.object(todoit, "prompt", side_effect=prompts) as prompt, \
                mock.patch.object(todoit, "draft", **({"side_effect": drafted} if isinstance(drafted, list) else {"return_value": drafted})) as draft, \
                mock.patch.object(todoit.subprocess, "run", side_effect=list(runs)) as run:
            result = todoit.spawn(self.scr, state(), 0, TASK)
        return result, [c.args[0] for c in run.call_args_list], {"pick": pick, "prompt": prompt, "draft": draft, "run": run}

    def test_slug_and_tilde(self):
        self.assertEqual(todoit.slug("Review [PR 41](https://x.io/pr/41)!"), "review-pr-41")
        self.assertEqual(todoit.slug("!!!"), "task")
        s = todoit.slug("a" * 39 + "-" + "b" * 30)
        self.assertTrue(len(s) <= 40 and not s.endswith("-"))
        self.assertEqual(todoit.tilde("/Users/t.s/vapi/x"), "~/vapi/x")
        self.assertEqual(todoit.tilde("/opt/x"), "/opt/x")

    def test_pick_digit_mode(self):
        scr, panel = mock.Mock(), mock.Mock()
        esc = ["\x1b", todoit.curses.error()]
        for n, (keys, want) in enumerate([(["j", "\n"], 1), (["2"], 1), (["k", "\r"], 0), (esc + esc, None),
                                           (esc + ["\n"], 0), ([todoit.curses.KEY_LEFT], todoit.BACK),
                                           (["j", todoit.curses.KEY_RIGHT], 1), (esc + ["x"] + esc + ["\n"], 0),
                                           (esc + ["j"] + esc + ["\n"], 1),
                                           (["9", "\n"], 0), (["0", "\n"], 0),
                                           ([todoit.curses.KEY_DOWN, todoit.curses.KEY_ENTER], 1)]):
            scr.get_wch.side_effect = keys
            with mock.patch.object(todoit.time, "monotonic", side_effect=[0, 0.1]):
                self.assertEqual(todoit.pick(scr, panel, "q?", ["a", "b"]), want, keys)
            if n == 0:
                self.assertEqual(panel.call_args.args, (["q?", "  1 a", "▸ 2 b"], 2))
        scr.timeout.assert_called_with(1000)

    def test_pick_search_mode(self):
        scr, panel = mock.Mock(), mock.Mock()
        items = ["todoit  ~/personal/todoit/main", "kafka-ingest  ~/vapi/kafka/main", "scratch"]
        esc = ["\x1b", todoit.curses.error()]
        for n, (keys, want) in enumerate([(["k", "a", "f", "\n"], 1), (["c", "h", " ", "s", "\n"], 2),
                                           (["z", "\n", "\x7f", todoit.curses.KEY_DOWN, todoit.curses.KEY_DOWN, "\n"], 2),
                                           (["j", "2", "\n"] + esc + esc, None), (esc + ["\n"], 0),
                                           (list("zz") + esc + list("kaf") + ["\n"], 1),
                                           (esc + [todoit.curses.KEY_DOWN] + esc + ["\n"], 1),
                                           (list("kaf zz") + ["\x1b", "\x7f", "\n"], 1),
                                           (list("kaf zz") + ["\x1b", "\b", "\n"], 1),
                                           (list("kaf zz") + ["\x1b", todoit.curses.KEY_BACKSPACE, "\n"], 1),
                                           (list("kaf") + ["\x1b", "x", "\n"], 1),
                                           (list("kaf") + [todoit.curses.KEY_LEFT] * 4, todoit.BACK),
                                           (list("kaf") + [todoit.curses.KEY_RIGHT], 1),
                                           (["z", todoit.curses.KEY_RIGHT] + esc + ["\n"], 0)]):
            scr.get_wch.side_effect = keys
            scr.timeout.reset_mock()
            with mock.patch.object(todoit.time, "monotonic", side_effect=[0, 0.1]):
                self.assertEqual(todoit.pick(scr, panel, "ws?", items, search=True), want, keys)
            self.assertEqual(scr.timeout.call_args_list,
                             [mock.call(-1)] + [mock.call(0), mock.call(-1)] * keys.count("\x1b") + [mock.call(1000)])
            if n == 0:
                self.assertEqual(panel.call_args.args, (["ws? kaf_", "▸ kafka-ingest  ~/vapi/kafka/main"], 1))

    def test_pick_default(self):
        scr, panel = mock.Mock(), mock.Mock()
        scr.get_wch.side_effect = ["\n"]
        self.assertEqual(todoit.pick(scr, panel, "q?", ["a", "b"], default=1), 1)
        self.assertEqual(panel.call_args.args, (["q?", "  1 a", "▸ 2 b"], 2))
        scr.get_wch.side_effect = ["\n"]
        self.assertEqual(todoit.pick(scr, panel, "ws?", ["a", "b", "c"], search=True, default=2), 2)
        self.assertEqual(panel.call_args.args, (["ws? _", "  a", "  b", "▸ c"], 3))
        scr.get_wch.side_effect = ["\n"]
        self.assertEqual(todoit.pick(scr, panel, "q?", ["a", "b"], default=None), 0)

    def test_pick_search_cursor(self):
        left, right = todoit.curses.KEY_LEFT, todoit.curses.KEY_RIGHT
        for keys, want, header in [([left], todoit.BACK, "ws? _"),
                                   (list("af") + [left, "b", "\n"], 0, "ws? ab_f"),
                                   (list("af") + [left, right, "b", "\n"], 1, "ws? afb_"),
                                   (list("af") + [right], 1, "ws? af_")]:
            scr, panel = mock.Mock(**{"get_wch.side_effect": keys}), mock.Mock()
            with self.subTest(keys=keys), mock.patch.object(todoit.curses, "keyname", return_value=b""):
                self.assertEqual(todoit.pick(scr, panel, "ws?", ["abf", "afb"], search=True), want)
            self.assertEqual(panel.call_args.args[0][0], header)
            if "b" in keys and left in keys:
                self.assertIn("ws? a_f", [c.args[0][0] for c in panel.call_args_list])

    def test_prompt_keep_on_back(self):
        scr = mock.Mock(**{"getmaxyx.return_value": (24, 80)})
        scr.get_wch.side_effect = ["a", "b"] + [todoit.curses.KEY_LEFT] * 3
        keep = [""]
        with mock.patch.object(todoit.curses, "curs_set"), mock.patch.object(todoit, "C", return_value=0):
            self.assertIs(todoit.prompt(scr, "x: ", "", keep=keep, nav=True), todoit.BACK)
            self.assertEqual(keep, ["ab"])
            scr.get_wch.side_effect = [todoit.curses.KEY_RIGHT]
            self.assertEqual(todoit.prompt(scr, "x: ", " ab ", nav=True), "ab")
            scr.get_wch.side_effect = ["a", todoit.curses.KEY_LEFT, "b", todoit.curses.KEY_RIGHT, "c", "\n"]
            self.assertEqual(todoit.prompt(scr, "x: ", keep=keep), "bac")
            self.assertEqual(keep, ["ab"])
            scr.get_wch.side_effect = [todoit.curses.KEY_LEFT]
            self.assertIs(todoit.prompt(scr, "x: ", nav=True), todoit.BACK)

    def test_prompt_nav_esc_clears_and_cancels(self):
        scr = mock.Mock(**{"getmaxyx.return_value": (24, 80)})
        esc, keep = ["\x1b", todoit.curses.error()], ["saved"]
        for keys, want in [(esc + [todoit.curses.KEY_RIGHT], ""), (esc + esc, None)]:
            scr.get_wch.side_effect = keys
            with mock.patch.object(todoit.curses, "curs_set"), mock.patch.object(todoit, "C", return_value=0), \
                    mock.patch.object(todoit.time, "monotonic", side_effect=[0, 0.1]):
                self.assertEqual(todoit.prompt(scr, "x: ", "old", keep=keep, nav=True), want)
            self.assertEqual(keep, ["saved"])

    def test_prompt_text_survives_back(self):
        texts = []

        def prompt(scr, label_, text="", keep=None, nav=False):
            self.assertTrue(label_.startswith("prompt"))
            self.assertTrue(nav)
            texts.append(text)
            if len(texts) == 1:
                keep[0] = "mine"
                return todoit.BACK
            return text

        _, argvs, m = self.spawn([0, 0, 0, 0, todoit.BACK, 1, 0], prompt, runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual(texts[1], "mine")
        self.assertEqual(argvs[3], run_line("codex", "gpt-6.1-sol", "mine"))
        m["draft"].assert_called_once()

    def test_prompt_label_after_typed_text(self):
        labels = []

        def prompt(scr, label_, text="", keep=None, nav=False):
            self.assertTrue(nav)
            labels.append(label_)
            if len(labels) == 1:
                keep[0] = "mine"
                return todoit.BACK
            return text

        _, argvs, _ = self.spawn([0, 0, 0, 0, 0], prompt, runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual(labels[1], "prompt: ")
        self.assertEqual(argvs[3], run_line("claude", "claude-opus-5-5", "mine"))

    def test_needs_herdr(self):
        with mock.patch.dict(os.environ, {"HERDR_ENV": "0"}), \
                mock.patch.object(todoit.subprocess, "run") as run, mock.patch.object(todoit, "pick") as pick:
            self.assertEqual(todoit.spawn(self.scr, state(), 0, TASK), "✗ needs herdr")
        run.assert_not_called()
        pick.assert_not_called()

    def test_worktree_launch_per_harness(self):
        self.assertEqual(list(todoit.MODELS), ["claude", "codex"])
        for h, harness in enumerate(todoit.MODELS):
            with self.subTest(harness=harness):
                result, argvs, m = self.spawn([0, 1, h, 0], ["review-pr-41", ""], runs=[LIST, NO_MACHINES, CREATED, RAN])
                model = todoit.MODELS[harness][0]
                self.assertEqual(argvs, WS_LIST + [
                    ["herdr", "worktree", "create", "--workspace", "wN", "--branch", "review-pr-41",
                     "--path", WT, "--label", "review-pr-41", "--no-focus"],
                    run_line(harness, model)])
                self.assertEqual(result, f"→ {harness} · {model} · ~/vapi/repo/review-pr-41")
                self.assertEqual(m["prompt"].call_args_list[0].args[1:], ("branch: ", "review-pr-41"))
                m["draft"].assert_called_once_with(TASK["title"], WT)

    def test_tab_in_git_workspace(self):
        result, argvs, m = self.spawn([0, 0, 1, 0], [""], runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual(argvs, WS_LIST + [TAB_WN, run_line("codex", "gpt-6.1-sol")])
        self.assertEqual(result, "→ codex · gpt-6.1-sol · repo (new tab)")
        m["draft"].assert_called_once_with(TASK["title"], CHECKOUT)
        self.assertEqual(m["pick"].call_args_list[0].args[2:], ("workspace?", LOCAL_ITEMS))
        self.assertEqual(m["pick"].call_args_list[0].kwargs, {"search": True, "default": None})
        self.assertEqual(m["pick"].call_args_list[1].args[2:], ("how?", ["new tab in it", "new worktree off it"]))

    def test_tab_in_non_git_workspace_skips_how(self):
        result, argvs, m = self.spawn([1, 1, 0], [""], runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual(argvs, WS_LIST + [TAB_WX, run_line("codex", "gpt-6.1-sol")])
        self.assertEqual(result, "→ codex · gpt-6.1-sol · plain (new tab)")
        self.assertEqual([c.args[2] for c in m["pick"].call_args_list], ["workspace?", "harness?", "model?"])
        m["draft"].assert_called_once_with(TASK["title"], "plain")

    def test_tab_in_linked_worktree_skips_how(self):
        result, argvs, m = self.spawn([2, 0, 0], [""], runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual(argvs, WS_LIST + [
            ["herdr", "tab", "create", "--workspace", "wL", "--cwd", FEAT, "--label", "Review PR 41!", "--no-focus"],
            ["herdr", "pane", "run", "w9:p1", shlex.join(["claude", "--model", "claude-opus-5-5", "--", "Fix it"])]])
        self.assertEqual(result, "→ claude · claude-opus-5-5 · feat (new tab)")
        self.assertEqual([c.args[2] for c in m["pick"].call_args_list], ["workspace?", "harness?", "model?"])
        m["draft"].assert_called_once_with(TASK["title"], FEAT)

    def test_other_model_is_prompted(self):
        result, argvs, _ = self.spawn([0, 0, 1, 3], ["gpt-x", "it's $HOME"], drafted=("suggested", None),
                                     runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual(argvs[3][4], shlex.join(["codex", "--model", "gpt-x", "--", "it's $HOME"]))
        self.assertEqual(shlex.split(argvs[3][4]), ["codex", "--model", "gpt-x", "--", "it's $HOME"])
        self.assertEqual(result, "→ codex · gpt-x · repo (new tab)")

    def test_double_esc_at_any_step_cancels(self):
        for picks, prompts in [([None], []), ([0, None], []), ([0, 1], [None]), ([0, 0, None], []),
                               ([0, 0, 0, None], []), ([0, 0, 0, 3], [None]), ([0, 0, 0, 0], [None])]:
            with self.subTest(picks=picks, prompts=prompts):
                result, argvs, m = self.spawn(picks, prompts, runs=[LIST, NO_MACHINES])
                self.assertIsNone(result)
                self.assertEqual(argvs, WS_LIST)
                self.assertEqual(m["pick"].call_count, len(picks))
                self.assertEqual(m["prompt"].call_count, len(prompts))
                if picks == [0, 0, 0, 0]:
                    m["draft"].assert_called_once()
                else:
                    m["draft"].assert_not_called()

    def test_back_on_first_step_stays(self):
        result, argvs, m = self.spawn([todoit.BACK, 0, todoit.BACK, todoit.BACK, None], [], runs=[LIST, NO_MACHINES])
        self.assertIsNone(result)
        self.assertEqual(argvs, WS_LIST)
        self.assertEqual([c.args[2] for c in m["pick"].call_args_list],
                         ["workspace?", "workspace?", "how?", "workspace?", "workspace?"])
        self.assertEqual(m["pick"].call_args_list[-1].kwargs, {"search": True, "default": 0})
        m["prompt"].assert_not_called()
        m["draft"].assert_not_called()

    def test_blank_branch_or_model_stays(self):
        for picks, prompts, label, cwd in [([0, 1, 0, 0], ["", "review-pr-41", ""], "branch: ", WT),
                                           ([0, 0, 1, 3], ["", "gpt-x", ""], "model: ", CHECKOUT)]:
            with self.subTest(label=label):
                result, argvs, m = self.spawn(picks, prompts, runs=[LIST, NO_MACHINES, CREATED, RAN])
                self.assertIsNotNone(result)
                self.assertEqual(len(argvs), 4)
                self.assertEqual(m["pick"].call_count, len(picks))
                self.assertEqual(m["prompt"].call_count, len(prompts))
                calls = m["prompt"].call_args_list
                self.assertEqual(calls[0], calls[1])
                self.assertEqual(calls[0].args[1], label)
                self.assertEqual(calls[0].kwargs, {"nav": True})
                m["draft"].assert_called_once_with(TASK["title"], cwd)

    def test_draft_failure_requires_typed_prompt(self):
        with self.assertLogs("todoit", "ERROR"):
            result, argvs, m = self.spawn([0, 0, 0, 0], ["", None],
                                         drafted=(None, "drafter timed out"), runs=[LIST, NO_MACHINES])
        self.assertIsNone(result)
        self.assertEqual(argvs, WS_LIST)
        self.assertIn(["✗ drafter timed out"], [c.args[4] for c in self.panel.call_args_list])
        self.assertEqual(m["prompt"].call_args.args[1], "prompt: ")
        self.assertEqual(m["draft"].call_args_list, [mock.call(TASK["title"], CHECKOUT)] * 2)
        self.assertEqual(m["pick"].call_count, 4)
        self.assertEqual(m["prompt"].call_count, 2)
        with self.assertLogs("todoit", "ERROR"):
            result, argvs, _ = self.spawn([0, 0, 0, 0], ["typed it"], drafted=(None, "drafter timed out"),
                                         runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual(argvs[3][4], shlex.join(["claude", "--model", "claude-opus-5-5", "--", "typed it"]))
        self.assertEqual(result, "→ claude · claude-opus-5-5 · repo (new tab)")

    def test_failed_draft_is_retried_after_back(self):
        with self.assertLogs("todoit", "ERROR"):
            result, _, m = self.spawn([0, 0, 0, 0, 0], [todoit.BACK, ""],
                                     drafted=[(None, "boom"), ("Fix it", None)], runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual(m["draft"].call_args_list, [mock.call(TASK["title"], CHECKOUT)] * 2)
        self.assertEqual(result, "→ claude · claude-opus-5-5 · repo (new tab)")
        self.assertEqual(m["prompt"].call_args_list[1].args[1], "prompt (enter = suggested): ")
        self.assertEqual(m["pick"].call_args_list[-1].args[2], "model?")

    def test_herdr_error_is_shown(self):
        bad = subprocess.CompletedProcess([], 1, "", '{"error":{"code":"git_failed","message":"branch exists"},"id":"x"}\n')
        plain_err = subprocess.CompletedProcess([], 1, "", "first\nplain text\n")
        for picks, prompts, runs, want in [([0, 1, 0, 0], ["b", ""], [LIST, NO_MACHINES, bad], "✗ branch exists"),
                                          ([0, 0, 0, 0], [""], [LIST, NO_MACHINES, plain_err], "✗ plain text"),
                                          ([], [], [bad], "✗ branch exists"),
                                          ([1, 1, 0], [""], [LIST, plain_err, CREATED, RAN], "→ codex · gpt-6.1-sol · plain (new tab)")]:
            with self.subTest(want=want), self.assertLogs("todoit", "ERROR"):
                result, argvs, _ = self.spawn(picks, prompts, runs=runs)
                self.assertEqual(result, want)
                self.assertEqual(len(argvs), len(runs))

    def test_remote_tab_lists_after_local_and_runs_over_ssh(self):
        result, argvs, m = self.spawn([3, 0, 1, 0], [""], runs=[LIST, MACHINES, MINI_LIST, CREATED, RAN])
        self.assertEqual(m["pick"].call_args_list[0].args[2:], ("workspace?", LOCAL_ITEMS + MINI_ITEMS))
        self.assertEqual(m["pick"].call_args_list[1].args[2], "how?")
        self.assertEqual(argvs, WS_LIST + [
            remote("workspace", "list"),
            remote("tab", "create", "--workspace", "w1H", "--cwd", MINI_CHECKOUT, "--label", "Review PR 41!", "--no-focus"),
            remote("pane", "run", "w9:p1", shlex.join(["codex", "--model", "gpt-6.1-sol", "--", "Fix it"]))])
        self.assertEqual(result, "→ codex · gpt-6.1-sol · mac-mini · arch-world (new tab)")
        m["draft"].assert_called_once_with(TASK["title"], MINI_CHECKOUT)
        self.assertEqual(m["run"].call_args_list[2].kwargs["timeout"], 10)
        self.assertEqual(m["run"].call_args_list[3].kwargs["timeout"], 90)

    def test_remote_worktree_is_sibling_of_remote_checkout(self):
        result, argvs, m = self.spawn([3, 1, 0, 0], ["review-pr-41", ""], runs=[LIST, MACHINES, MINI_LIST, CREATED, RAN])
        self.assertEqual(argvs[3:], [
            remote("worktree", "create", "--workspace", "w1H", "--branch", "review-pr-41", "--path", MINI_WT,
                   "--label", "review-pr-41", "--no-focus"),
            remote("pane", "run", "w9:p1", shlex.join(["claude", "--model", "claude-opus-5-5", "--", "Fix it"]))])
        self.assertEqual(result, "→ claude · claude-opus-5-5 · mac-mini · ~/vapi/mono/review-pr-41")
        m["draft"].assert_called_once_with(TASK["title"], MINI_WT)

    def test_ssh_quoting_round_trips(self):
        _, argvs, _ = self.spawn([3, 0, 1, 3], ["gpt-x", "it's $HOME"], runs=[LIST, MACHINES, MINI_LIST, CREATED, RAN])
        run = argvs[4]
        self.assertEqual(run[:7], SSH)
        self.assertEqual(len(run), 8)
        outer = shlex.split(run[7])
        self.assertEqual(outer, ["herdr", "--session", "default", "pane", "run", "w9:p1",
                                 shlex.join(["codex", "--model", "gpt-x", "--", "it's $HOME"])])
        self.assertEqual(shlex.split(outer[6]), ["codex", "--model", "gpt-x", "--", "it's $HOME"])

    def test_unreachable_machine_is_skipped(self):
        for bad in (DOWN, GARBAGE, subprocess.CompletedProcess([], 0, "null\n", "")):
            with self.subTest(bad=bad):
                with self.assertLogs("todoit", "ERROR") as logs:
                    result, argvs, m = self.spawn([1, 1, 0], [""], runs=[LIST, MACHINES, bad, CREATED, RAN])
                self.assertEqual(m["pick"].call_args_list[0].args[2:], ("workspace?", LOCAL_ITEMS))
                self.assertTrue(logs.output[0].startswith("ERROR:todoit:mac-mini (t@mini) skipped: "))
                if bad is DOWN:
                    self.assertIn("Operation timed out", logs.output[0])
                self.assertEqual(argvs, WS_LIST + [remote("workspace", "list"), TAB_WX, run_line("codex", "gpt-6.1-sol")])
                self.assertEqual(result, "→ codex · gpt-6.1-sol · plain (new tab)")

    def test_back_to_workspace_retargets(self):
        result, argvs, m = self.spawn([0, 0, todoit.BACK, todoit.BACK, 1, 1, 0], [""], runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual(argvs, WS_LIST + [TAB_WX, run_line("codex", "gpt-6.1-sol")])
        self.assertEqual(result, "→ codex · gpt-6.1-sol · plain (new tab)")
        self.assertEqual(m["pick"].call_args_list[3].args[2], "how?")
        self.assertEqual(m["pick"].call_args_list[3].kwargs, {"default": 0})
        self.assertEqual(m["pick"].call_args_list[4].kwargs, {"search": True, "default": 0})
        m["draft"].assert_called_once_with(TASK["title"], "plain")

    def test_back_hops_skipped_steps(self):
        result, argvs, m = self.spawn([2, todoit.BACK, 0, 0, 0, 0], [""], runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual([c.args[2] for c in m["pick"].call_args_list],
                         ["workspace?", "harness?", "workspace?", "how?", "harness?", "model?"])
        self.assertEqual(argvs, WS_LIST + [TAB_WN, run_line("claude", "claude-opus-5-5")])
        self.assertEqual(result, "→ claude · claude-opus-5-5 · repo (new tab)")

    def test_back_from_prompt_reuses_draft(self):
        result, argvs, m = self.spawn([0, 0, 0, 0, todoit.BACK, 1, 0], [todoit.BACK, ""], runs=[LIST, NO_MACHINES, CREATED, RAN])
        m["draft"].assert_called_once_with(TASK["title"], CHECKOUT)
        self.assertEqual(argvs[3], run_line("codex", "gpt-6.1-sol"))
        self.assertEqual(result, "→ codex · gpt-6.1-sol · repo (new tab)")
        self.assertEqual(m["pick"].call_args_list[4].kwargs, {"default": 0})

    def test_back_to_other_workspace_redrafts(self):
        result, _, m = self.spawn([0, 1, 0, 0, todoit.BACK, todoit.BACK, todoit.BACK, 1, 0, 0], ["b", todoit.BACK, todoit.BACK, ""],
                                 runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual(m["draft"].call_args_list,
                         [mock.call(TASK["title"], "/Users/t/vapi/repo/b"), mock.call(TASK["title"], "plain")])
        self.assertEqual(m["prompt"].call_args_list[2].args[1:], ("branch: ", "b"))
        self.assertEqual(result, "→ claude · claude-opus-5-5 · plain (new tab)")

    def test_draft_argv_and_errors(self):
        for res, want in [(subprocess.CompletedProcess([], 0, "  Do\nthe\0 thing ", ""), ("Do the thing", None)),
                          (subprocess.TimeoutExpired("claude", 30), "timed out"),
                          (FileNotFoundError("no claude"), "failed to start"),
                          (subprocess.CompletedProcess([], 2, "", "a\nlast\n"), "last"),
                          (subprocess.CompletedProcess([], 0, "   ", ""), "empty draft")]:
            kwargs = {"side_effect": res} if isinstance(res, Exception) else {"return_value": res}
            with self.subTest(want=want), mock.patch.object(todoit.subprocess, "run", **kwargs) as run:
                text, error = todoit.draft(TASK["title"], "/tmp/wt")
                if isinstance(want, tuple):
                    self.assertEqual((text, error), want)
                    run.assert_called_once_with(todoit.DRAFTER, input=mock.ANY, capture_output=True,
                                                text=True, timeout=30, check=False)
                    self.assertIn("https://x.io/pr/41", run.call_args.kwargs["input"])
                    self.assertIn("/tmp/wt", run.call_args.kwargs["input"])
                else:
                    self.assertIsNone(text)
                    self.assertIn(want, error)


if __name__ == "__main__":
    unittest.main()
