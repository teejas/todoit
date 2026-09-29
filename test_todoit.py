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
    return {"last_reset": "2026-09-28", "daily": [], "todo": [], **kw}


class ParseDue(unittest.TestCase):
    def test_valid(self):
        for kind, raw, want in [
            ("daily", "17:00", "17:00"),
            ("daily", "9:05", "09:05"),
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
        self.assertEqual(self.notify.call_args.args[1], "late")
        self.assertEqual([(t["done"], t["sent"]) for t in s["daily"]], [(False, [])] * 3)
        self.assertEqual([t["title"] for t in s["todo"]], ["open"])
        self.assertEqual(s["last_reset"], "2026-09-29")
        self.assertFalse(todoit.rollover(s, at(29, 9, 0)))  # same day: no-op
        self.assertFalse(todoit.rollover(s, at(28, 23, 0)))  # clock stepped backwards: no-op

    def test_daily_added_after_its_time_stays_quiet_until_tomorrow(self):
        self.assertEqual(todoit.fresh_sent("daily", "17:00", at(28, 19, 0)), ["soon", "missed"])
        self.assertEqual(todoit.fresh_sent("daily", "20:00", at(28, 19, 0)), [])
        self.assertEqual(todoit.fresh_sent("todo", "2026-09-29 17:00", at(28, 19, 0)), [])
        s = state(daily=[task("late add", "17:00", sent=todoit.fresh_sent("daily", "17:00", at(28, 19, 0)))])
        todoit.check(s, at(28, 19, 5))
        todoit.rollover(s, at(29, 0, 1))
        self.notify.assert_not_called()


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
        ]:
            self.assertEqual(todoit.label(kind, t_, t)[0], want)

    def test_momentum_splits_due_today_from_overdue(self):
        s = state(daily=[task("done", "09:00", done=True), task("missed", "12:00"), task("later", "18:00")],
                  todo=[task("late", "2026-09-27 17:00"), task("tonight", "2026-09-28 20:00"),
                        task("next week", "2026-10-05 17:00"), task("early", "2026-10-05 17:00", done=True)])
        self.assertEqual(todoit.momentum(s, at(28, 16, 0)), (2, 7, 2, 2))
        self.assertEqual(todoit.momentum(state(), at(28, 16, 0)), (0, 0, 0, 0))

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

    def test_reload_refuses_write_after_midnight_reshuffle(self):
        with TemporaryDirectory() as d, mock.patch.object(todoit, "DB", Path(d) / "tasks.json"), \
                mock.patch.object(todoit, "notify"), mock.patch.object(todoit, "now") as now:
            todoit.save(state(todo=[task("A", "2026-09-30 17:00", done=True), task("B", "2026-09-30 17:00")]))
            now.return_value = at(28, 23, 59)
            drawn = todoit.load()
            self.assertIsNotNone(todoit.reload(drawn))  # same day: safe to write by index
            now.return_value = at(29, 0, 5)
            self.assertIsNone(todoit.reload(drawn))  # rollover purged A, so index 1 no longer means B


if __name__ == "__main__":
    unittest.main()
