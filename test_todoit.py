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
        for n, (keys, want) in enumerate([(["j", "\n"], 1), (["2"], 1), (["k", "\r"], 0), (["\x1b"], None),
                                           (["9", "\n"], 0), (["0", "\n"], 0),
                                           ([todoit.curses.KEY_DOWN, todoit.curses.KEY_ENTER], 1)]):
            scr.get_wch.side_effect = keys
            self.assertEqual(todoit.pick(scr, panel, "q?", ["a", "b"]), want, keys)
            if n == 0:
                self.assertEqual(panel.call_args.args, (["q?", "  1 a", "▸ 2 b"], 2))
        scr.timeout.assert_called_with(1000)

    def test_pick_search_mode(self):
        scr, panel = mock.Mock(), mock.Mock()
        items = ["todoit  ~/personal/todoit/main", "kafka-ingest  ~/vapi/kafka/main", "scratch"]
        for n, (keys, want) in enumerate([(["k", "a", "f", "\n"], 1), (["c", "h", " ", "s", "\n"], 2),
                                           (["z", "\n", "\x7f", todoit.curses.KEY_DOWN, todoit.curses.KEY_DOWN, "\n"], 2),
                                           (["j", "2", "\n", "\x1b"], None), (["\x1b"], None)]):
            scr.get_wch.side_effect = keys
            self.assertEqual(todoit.pick(scr, panel, "ws?", items, search=True), want, keys)
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

    def test_prompt_keep_on_esc(self):
        scr = mock.Mock(**{"getmaxyx.return_value": (24, 80)})
        scr.get_wch.side_effect = ["a", "b", "\x1b"]
        keep = [""]
        with mock.patch.object(todoit.curses, "curs_set"), mock.patch.object(todoit, "C", return_value=0):
            self.assertIsNone(todoit.prompt(scr, "x: ", "", keep=keep))
            self.assertEqual(keep, ["ab"])
            scr.get_wch.side_effect = ["\x1b"]
            self.assertIsNone(todoit.prompt(scr, "x: "))

    def test_prompt_text_survives_back(self):
        texts = []

        def prompt(scr, label_, text="", keep=None):
            self.assertTrue(label_.startswith("prompt"))
            texts.append(text)
            if len(texts) == 1:
                keep[0] = "mine"
                return None
            return text

        _, argvs, m = self.spawn([0, 0, 0, 0, None, 1, 0], prompt, runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual(texts[1], "mine")
        self.assertEqual(argvs[3], run_line("codex", "gpt-6.1-sol", "mine"))
        m["draft"].assert_called_once()

    def test_prompt_label_after_typed_text(self):
        labels = []

        def prompt(scr, label_, text="", keep=None):
            labels.append(label_)
            if len(labels) == 1:
                keep[0] = "mine"
                return None
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

    def test_esc_back_to_first_step_cancels(self):
        for picks, prompts in [([None], []), ([1, None, None], []), ([0, None, None], []),
                               ([0, 1, None, None], [None]), ([0, 1, None, None], [""]),
                               ([0, 0, 0, 3, None, None, None, None], [""]),
                               ([2, 0, 0, None, None, None], [None])]:
            with self.subTest(picks=picks, prompts=prompts):
                result, argvs, m = self.spawn(picks, prompts, runs=[LIST, NO_MACHINES])
                self.assertIsNone(result)
                self.assertEqual(argvs, WS_LIST)
                if picks == [2, 0, 0, None, None, None]:
                    m["draft"].assert_called_once()

    def test_draft_failure_requires_typed_prompt(self):
        with self.assertLogs("todoit", "ERROR"):
            result, argvs, m = self.spawn([0, 0, 0, 0, None, None, None, None], [""],
                                         drafted=(None, "drafter timed out"), runs=[LIST, NO_MACHINES])
        self.assertIsNone(result)
        self.assertEqual(argvs, WS_LIST)
        self.assertIn(["✗ drafter timed out"], [c.args[4] for c in self.panel.call_args_list])
        self.assertEqual(m["prompt"].call_args.args[1], "prompt: ")
        m["draft"].assert_called_once()
        with self.assertLogs("todoit", "ERROR"):
            result, argvs, _ = self.spawn([0, 0, 0, 0], ["typed it"], drafted=(None, "drafter timed out"),
                                         runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual(argvs[3][4], shlex.join(["claude", "--model", "claude-opus-5-5", "--", "typed it"]))
        self.assertEqual(result, "→ claude · claude-opus-5-5 · repo (new tab)")

    def test_failed_draft_is_retried_after_back(self):
        with self.assertLogs("todoit", "ERROR"):
            result, _, m = self.spawn([0, 0, 0, 0, 0], ["", ""],
                                     drafted=[(None, "boom"), ("Fix it", None)], runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual(m["draft"].call_args_list, [mock.call(TASK["title"], CHECKOUT)] * 2)
        self.assertEqual(result, "→ claude · claude-opus-5-5 · repo (new tab)")
        self.assertEqual(m["prompt"].call_args_list[1].args[1], "prompt (enter = suggested): ")

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
        result, argvs, m = self.spawn([0, 0, None, None, 1, 1, 0], [""], runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual(argvs, WS_LIST + [TAB_WX, run_line("codex", "gpt-6.1-sol")])
        self.assertEqual(result, "→ codex · gpt-6.1-sol · plain (new tab)")
        self.assertEqual(m["pick"].call_args_list[3].args[2], "how?")
        self.assertEqual(m["pick"].call_args_list[3].kwargs, {"default": 0})
        self.assertEqual(m["pick"].call_args_list[4].kwargs, {"search": True, "default": 0})
        m["draft"].assert_called_once_with(TASK["title"], "plain")

    def test_back_hops_skipped_steps(self):
        result, argvs, m = self.spawn([2, None, 0, 0, 0, 0], [""], runs=[LIST, NO_MACHINES, CREATED, RAN])
        self.assertEqual([c.args[2] for c in m["pick"].call_args_list],
                         ["workspace?", "harness?", "workspace?", "how?", "harness?", "model?"])
        self.assertEqual(argvs, WS_LIST + [TAB_WN, run_line("claude", "claude-opus-5-5")])
        self.assertEqual(result, "→ claude · claude-opus-5-5 · repo (new tab)")

    def test_back_from_prompt_reuses_draft(self):
        result, argvs, m = self.spawn([0, 0, 0, 0, None, 1, 0], [None, ""], runs=[LIST, NO_MACHINES, CREATED, RAN])
        m["draft"].assert_called_once_with(TASK["title"], CHECKOUT)
        self.assertEqual(argvs[3], run_line("codex", "gpt-6.1-sol"))
        self.assertEqual(result, "→ codex · gpt-6.1-sol · repo (new tab)")
        self.assertEqual(m["pick"].call_args_list[4].kwargs, {"default": 0})

    def test_back_to_other_workspace_redrafts(self):
        result, _, m = self.spawn([0, 1, 0, 0, None, None, None, 1, 0, 0], ["b", None, None, ""],
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
