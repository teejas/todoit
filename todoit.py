#!/usr/bin/env python3
"""todoit: daily + ad-hoc todo TUI with macOS banner reminders.

    todoit          open the TUI
    todoit notify   send due-soon / missed banners (launchd runs this every 5 min)
"""
import curses
import json
import locale
import logging
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

DB = Path(os.environ.get("TODOIT_HOME", Path.home() / ".todoit")) / "tasks.json"
HEADS_UP = timedelta(minutes=30)
DEFAULT_TIME = "17:00"
KINDS = ("daily", "todo")
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
URL = re.compile(r"https?://[^\s)]+")
HINT = {"daily": "due HH:MM", "todo": "due today|tomorrow|+N|fri|MM-DD|YYYY-MM-DD [HH:MM]"}
HELP = "space done  a add todo  r add daily  e edit  d delete  o open link  j/k move  q quit"
CHEERS = ("nice.", "crushed it.", "one down.", "boom.", "look at you go.", "chef's kiss.", "shipped.", "unstoppable.")
G, DRAG = 0.05, 0.92  # particle gravity (rows/frame^2) and air drag; tune to taste
RED, YELLOW, GREEN, CYAN, MAGENTA, BLUE = range(1, 7)
log = logging.getLogger("todoit")


def now():
    return datetime.now()


def plain(title):
    return LINK.sub(r"\1", title)


# ---------- model ----------

def parse_due(kind, raw, today):
    """User input -> 'HH:MM' (daily) or 'YYYY-MM-DD HH:MM' (todo). Raises ValueError/OverflowError."""
    parts = raw.lower().split()
    if not parts:
        raise ValueError("due date required")
    has_time = re.fullmatch(r"\d{1,2}:\d{2}", parts[-1])
    hhmm = datetime.strptime(parts.pop(), "%H:%M").strftime("%H:%M") if has_time else DEFAULT_TIME
    if kind == "daily":
        if parts or not has_time:
            raise ValueError("daily tasks take HH:MM")
        return hhmm
    word = " ".join(parts) or "today"
    if word == "today":
        d = today
    elif word == "tomorrow":
        d = today + timedelta(days=1)
    elif re.fullmatch(r"\+\d+", word):
        d = today + timedelta(days=int(word[1:]))
    elif len(word) >= 3 and any(day.startswith(word) for day in DAYS):
        wd = next(i for i, day in enumerate(DAYS) if day.startswith(word))
        d = today + timedelta(days=(wd - today.weekday()) % 7)
    elif re.fullmatch(r"\d{1,2}[-/]\d{1,2}", word):
        d = date(today.year, *map(int, re.split(r"[-/]", word)))
        if d < today:
            d = d.replace(year=today.year + 1)
    else:
        d = date.fromisoformat(word)
    return f"{d} {hhmm}"


def due_at(kind, task, today):
    if kind == "daily":
        return datetime.combine(today, datetime.strptime(task["due"], "%H:%M").time())
    return datetime.strptime(task["due"], "%Y-%m-%d %H:%M")


def fresh_sent(kind, due, t):
    """`sent` for a newly set due. A daily set after today's due time stays quiet until tomorrow."""
    return ["soon", "missed"] if kind == "daily" and t >= due_at(kind, {"due": due}, t.date()) else []


def notify(title, body, sound):
    """macOS banner. Args go via argv so quotes in task titles can't break the AppleScript."""
    r = subprocess.run(["osascript", "-e", "on run argv", "-e",
                        "display notification (item 2 of argv) with title (item 1 of argv) sound name (item 3 of argv)",
                        "-e", "end run", title, body, sound], capture_output=True, text=True, check=False)
    if r.returncode:
        log.error("banner failed (exit %s): %s | %s: %s", r.returncode, title, body, r.stderr.strip())
    else:
        log.info("banner: %s | %s", title, body)


def rollover(s, t):
    """Midnight reset: flag unfinished dailies as missed, uncheck them, drop finished todos."""
    today = t.date().isoformat()
    if s["last_reset"] >= today:  # >= so a clock stepping backwards can't re-run it
        return False
    for task in s["daily"]:
        if not task["done"] and "missed" not in task["sent"]:  # due 23:59, or the Mac slept through it
            notify(f"❌ missed · was due {task['due']}", plain(task["title"]), "Basso")
        task["done"], task["sent"] = False, []
    purged = [x["title"] for x in s["todo"] if x["done"]]
    s["todo"] = [x for x in s["todo"] if not x["done"]]
    log.info("rollover %s -> %s: reset %d dailies, purged done todos %s",
             s["last_reset"], today, len(s["daily"]), purged)
    s["last_reset"] = today
    return True


def check(s, t):
    """Send each task's heads-up and missed banner at most once (dailies re-arm at midnight)."""
    for kind in KINDS:
        for task in s[kind]:
            due = due_at(kind, task, t.date())
            if task["done"] or t < due - HEADS_UP:
                continue
            when = f"{due:%H:%M}" if due.date() == t.date() else f"{due:%a %-m/%-d %H:%M}"
            if t >= due:
                tag, title, sound = "missed", f"❌ missed · was due {when}", "Basso"
            else:
                tag, title, sound = "soon", f"⏰ due {when}", "Glass"
            if tag not in task["sent"]:
                task["sent"].append(tag)
                notify(title, plain(task["title"]), sound)


def load():
    t = now()
    try:
        s = json.loads(DB.read_text())
    except FileNotFoundError:
        log.info("no %s yet, starting empty", DB)
        s = {"last_reset": t.date().isoformat(), "daily": [], "todo": []}
        save(s)
    if rollover(s, t):
        save(s)
    return s


def reload(old):
    """Fresh state for a write, or None if a midnight rollover reshuffled the lists since `old` was drawn."""
    s = load()
    return s if s["last_reset"] == old["last_reset"] else None


def save(s):
    # ponytail: no file lock; the TUI and notifier writing in the same instant can drop one write. add fcntl.flock if it bites
    DB.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DB.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DB)  # atomic swap: a crash mid-write can't truncate tasks.json


# ---------- TUI ----------

def C(n):
    return curses.color_pair(n)


def fit(text, cols):
    """Clip text to `cols` terminal columns; wide chars (CJK, emoji) take two."""
    out = []
    for c in text:
        cols -= 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if cols < 0:
            break
        out.append(c)
    return "".join(out)


def put(scr, y, x, text, attr=0):
    h, w = scr.getmaxyx()
    if 0 <= y < h and 0 <= x < w:
        try:
            scr.addstr(y, x, fit(text, w - x), attr)
        except curses.error:  # writing the bottom-right cell raises after drawing; harmless
            pass


def label(kind, task, t):
    """Right-hand due label and its color."""
    due = due_at(kind, task, t.date())
    days = (due.date() - t.date()).days
    if kind == "daily":
        text = task["due"]
    else:
        day = {0: "today", 1: "tomorrow"}.get(days) or (f"{due:%a}" if 1 < days < 7 else f"{due:%-m/%-d}")
        text = f"{day} {due:%H:%M}"
    if task["done"]:
        return text, C(GREEN) | curses.A_DIM
    if t >= due:
        return ("OVERDUE " if kind == "todo" else "missed ") + text, C(RED) | curses.A_BOLD
    if t >= due - HEADS_UP or (kind == "todo" and days == 0):
        return text, C(YELLOW)
    return text, curses.A_DIM


def momentum(s, t):
    """(done, total, due later today, overdue). Checked todos linger until midnight, so done ~= done today."""
    tasks = [(k, x) for k in KINDS for x in s[k]]
    dues = [due_at(k, x, t.date()) for k, x in tasks if not x["done"]]
    return (sum(x["done"] for _, x in tasks), len(tasks),
            sum(t < d and d.date() == t.date() for d in dues), sum(t >= d for d in dues))


def draw(scr, s, cur, t):
    """Render the list; returns the screen row of each task, in cursor order."""
    scr.erase()
    h, w = scr.getmaxyx()
    put(scr, 0, 1, "todoit", C(MAGENTA) | curses.A_BOLD)
    stamp = f"{t:%a %b %-d  %H:%M}"
    put(scr, 0, w - len(stamp) - 1, stamp, curses.A_DIM)
    done, total, today, overdue = momentum(s, t)
    bar = max(10, min(30, w - 55))
    fill = round(bar * done / total) if total else 0
    put(scr, 1, 1, "MOMENTUM", C(MAGENTA) | curses.A_BOLD)
    put(scr, 1, 10, "━" * fill, C(GREEN) | curses.A_BOLD)
    put(scr, 1, 10 + fill, "━" * (bar - fill), curses.A_DIM)
    put(scr, 1, 11 + bar, f"{done}/{total} done", curses.A_BOLD)
    stats = f"{today} due today · {overdue} overdue"
    put(scr, 1, w - len(stats) - 1, stats, C(RED) | curses.A_BOLD if overdue else curses.A_DIM)
    y, ys = 3, []
    # ponytail: no scrolling; add it when the list outgrows the terminal
    for kind in KINDS:
        tasks = s[kind]
        put(scr, y, 1, "─" * (w - 2), C(CYAN) | curses.A_DIM)
        put(scr, y, 2, f" {kind.upper()} {sum(x['done'] for x in tasks)}/{len(tasks)} ", C(CYAN) | curses.A_BOLD)
        if kind == "daily":
            put(scr, y, w - 21, " resets at midnight ", C(CYAN) | curses.A_DIM)
        y += 1
        if not tasks:
            put(scr, y, 4, f"nothing here, press {'r' if kind == 'daily' else 'a'} to add", curses.A_DIM)
            y += 1
        for task in tasks:
            sel = len(ys) == cur
            lab, lab_attr = label(kind, task, t)
            title = plain(task["title"]) + (" ↗" if URL.search(task["title"]) else "")
            put(scr, y, 1, "▸" if sel else " ", C(MAGENTA) | curses.A_BOLD)
            put(scr, y, 3, "[✓]" if task["done"] else "[ ]", C(GREEN) | curses.A_BOLD if task["done"] else 0)
            put(scr, y, 7, fit(title, max(0, w - len(lab) - 10)),
                (curses.A_DIM if task["done"] else 0) | (curses.A_BOLD if sel else 0))
            put(scr, y, w - len(lab) - 2, lab, lab_attr)
            ys.append(y)
            y += 1
        y += 1
    put(scr, h - 1, 1, HELP, curses.A_DIM)
    return ys


def prompt(scr, label_, text=""):
    """One-line editor on the bottom row. Enter returns the text, Esc returns None."""
    curses.curs_set(1)
    scr.timeout(-1)
    try:
        while True:
            h, w = scr.getmaxyx()
            scr.move(h - 1, 0)
            scr.clrtoeol()
            shown_label = fit(label_, max(0, w - 20))  # always leave room to type
            put(scr, h - 1, 1, shown_label, C(YELLOW) | curses.A_BOLD)
            x = min(len(shown_label) + 1, w - 1)
            shown = text[-max(1, w - x - 1):]
            put(scr, h - 1, x, shown)
            scr.move(h - 1, min(x + len(shown), w - 1))
            ch = scr.get_wch()
            if ch in ("\n", "\r", curses.KEY_ENTER):
                return text.strip()
            if ch == "\x1b":
                return None
            if ch in ("\x7f", "\b", curses.KEY_BACKSPACE):
                text = text[:-1]
            elif isinstance(ch, str) and ch.isprintable():
                text += ch
    finally:
        curses.curs_set(0)
        scr.timeout(1000)


def confirm(scr, msg):
    h, _ = scr.getmaxyx()
    scr.move(h - 1, 0)
    scr.clrtoeol()
    put(scr, h - 1, 1, msg, C(RED) | curses.A_BOLD)
    scr.timeout(-1)
    try:
        return scr.getch() in (ord("y"), ord("Y"))
    finally:
        scr.timeout(1000)


def ask_task(scr, kind, task=None):
    """Prompt for (title, due); re-asks until the due parses and a new todo due is in the future. None if cancelled."""
    title = prompt(scr, "title: ", task["title"] if task else "")
    if not title:
        return None
    hint, raw = HINT[kind], task["due"] if task else ""
    while True:
        raw = prompt(scr, f"{hint}: ", raw)
        if raw is None:
            return None
        try:
            due = parse_due(kind, raw, now().date())
        except (ValueError, OverflowError):
            hint = "✗ invalid · " + HINT[kind]
            continue
        if kind == "todo" and due != (task and task["due"]) and due_at(kind, {"due": due}, now().date()) <= now():
            hint = "✗ that's in the past · " + HINT[kind]
            continue
        return title, due


def burst(x, y, n, speed, up=0.0, color=None):
    """n particles flying out of (x, y). A particle is [x, y, vx, vy, life, attr, is_rocket]."""
    ps = []
    for _ in range(n):
        a, v = random.uniform(0, 2 * math.pi), speed * random.uniform(0.3, 1)
        # x velocity doubled because terminal cells are ~2x taller than wide
        ps.append([x, y, 2 * v * math.cos(a), v * math.sin(a) - up, random.randint(14, 30),
                   C(color or random.randint(1, 6)), False])
    return ps


def rocket(w, h):
    rise = random.uniform(0.35, 0.7) * h
    return [random.uniform(0.15, 0.85) * w, h - 2, random.uniform(-0.3, 0.3),
            -math.sqrt(2 * G * rise), 999, C(YELLOW), True]


def step(scr, ps):
    """Advance one frame of physics and draw every particle."""
    alive = []
    for p in ps:
        if p[6] and p[3] >= 0:  # rocket reached its apex: explode
            alive += burst(p[0], p[1], 60, 1.1, color=random.randint(1, 6))
            continue
        if not p[6]:
            p[2] *= DRAG
            p[3] *= DRAG
        p[0] += p[2]
        p[1] += p[3]
        p[3] += G
        p[4] -= 1
        if p[4] > 0:
            alive.append(p)
            ch = "|" if p[6] else "✦" if p[4] > 20 else "*" if p[4] > 10 else "·"
            put(scr, round(p[1]), round(p[0]), ch, p[5] | curses.A_BOLD)
    ps[:] = alive


def play(scr, s, cur, frames, fx):
    """~30fps animation over the list. fx(frame, particles) spawns and overlays. Any key skips."""
    ps = []
    scr.timeout(33)
    try:
        for f in range(frames):
            draw(scr, s, cur, now())
            step(scr, ps)
            fx(f, ps)
            if scr.getch() != -1:
                break
    finally:
        scr.timeout(1000)


def celebrate(scr, s, cur, kind, i, y):
    h, w = scr.getmaxyx()
    if all(x["done"] for x in s[kind]):
        text = " ✦ ALL DAILIES DONE ✦ " if kind == "daily" else " ✦ TODO LIST CLEARED ✦ "

        def fx(f, ps):
            if f % 8 == 0 and f < 64:
                ps.append(rocket(w, h))
            put(scr, h // 2, (w - len(text)) // 2, text, C(1 + f // 3 % 6) | curses.A_BOLD | curses.A_REVERSE)
        play(scr, s, cur, 120, fx)
    else:
        title, cheer = plain(s[kind][i]["title"]), random.choice(CHEERS)

        def fx(f, ps):
            if f == 0:
                ps.extend(burst(7 + min(len(title), w - 8) // 2, y, 60, 1.2, up=0.4))  # out of the title
            put(scr, y, 7, title[:3 * f], C(GREEN) | curses.A_BOLD)  # green sweep across the title
            put(scr, y, 9 + len(title), cheer[:f], C(MAGENTA) | curses.A_BOLD)  # typed-out cheer
        play(scr, s, cur, 45, fx)


def tui(scr):
    curses.curs_set(0)
    curses.set_escdelay(25)
    curses.use_default_colors()
    for n, color in enumerate((curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_GREEN,
                               curses.COLOR_CYAN, curses.COLOR_MAGENTA, curses.COLOR_BLUE), 1):
        curses.init_pair(n, color, -1)
    scr.timeout(1000)  # tick every second: clock, due colors, midnight rollover
    cur = 0
    while True:
        s = load()
        rows = [(k, i) for k in KINDS for i in range(len(s[k]))]
        cur = max(0, min(cur, len(rows) - 1))
        ys = draw(scr, s, cur, now())
        ch = scr.getch()
        # every write reloads first so it doesn't clobber what the notifier just saved
        if ch == ord("q"):
            return
        if ch in (ord("j"), curses.KEY_DOWN):
            cur += 1
        elif ch in (ord("k"), curses.KEY_UP):
            cur -= 1
        elif ch in (ord("a"), ord("r")):
            kind = "todo" if ch == ord("a") else "daily"
            got = ask_task(scr, kind)
            if got:
                s = load()
                s[kind].append({"title": got[0], "due": got[1], "done": False, "sent": fresh_sent(kind, got[1], now())})
                save(s)
                log.info("added %s: %s (due %s)", kind, *got)
                cur = len(s["daily"]) - 1 + (len(s["todo"]) if kind == "todo" else 0)
        elif rows:
            kind, i = rows[cur]
            task = s[kind][i]
            if ch in (ord(" "), ord("x"), 10):
                s = reload(s)
                if s:
                    task = s[kind][i]
                    task["done"] = not task["done"]
                    save(s)
                    log.info("%s: %s", "done" if task["done"] else "undone", task["title"])
                    if task["done"]:
                        celebrate(scr, s, cur, kind, i, ys[cur])
            elif ch == ord("e"):
                got = ask_task(scr, kind, task)
                s = reload(s) if got else None
                if s:
                    task = s[kind][i]
                    if got[1] != task["due"]:
                        task["sent"] = fresh_sent(kind, got[1], now())
                    task["title"], task["due"] = got
                    save(s)
                    log.info("edited %s: %s (due %s)", kind, *got)
            elif ch == ord("d"):
                s = reload(s) if confirm(scr, f"delete '{plain(task['title'])}'? y/n") else None
                if s:
                    log.info("deleted %s: %s", kind, s[kind].pop(i)["title"])
                    save(s)
            elif ch == ord("o"):
                m = URL.search(task["title"])
                if m:
                    subprocess.run(["open", m.group()], check=False)


if __name__ == "__main__":
    DB.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=DB.parent / "todoit.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if sys.argv[1:] == ["notify"]:
        state = load()
        check(state, now())
        save(state)
    else:
        locale.setlocale(locale.LC_ALL, "")
        try:
            curses.wrapper(tui)
        except KeyboardInterrupt:
            pass
