#!/usr/bin/env python3
"""todoit: daily, weekly + ad-hoc todo TUI with macOS banner reminders.

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
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

DB = Path(os.environ.get("TODOIT_HOME", Path.home() / ".todoit")) / "tasks.json"
# skip user settings (plugins, hooks) and MCP servers: inheriting them is ~230K+ input tokens and ~9s per turn vs ~3.5s
DRAFTER = ["claude", "-p", "--model", "claude-sonnet-5-5", "--tools", "", "--no-session-persistence",
           "--strict-mcp-config", "--setting-sources", ""]
HEADS_UP = timedelta(minutes=30)
DEFAULT_TIME = "17:00"
DOUBLE_ESC = 0.5  # seconds between Esc taps
BACK = object()  # pick/prompt result for ←: previous spawn step
KINDS = ("daily", "weekly", "todo")
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
URL = re.compile(r"https?://[^\s)]+")
HINT = {"daily": "due HH:MM", "weekly": "due DAY [HH:MM]", "todo": "due today|tomorrow|+N|fri|MM-DD|YYYY-MM-DD [HH:MM]"}
HELP = "space done a/r/w add todo/daily/weekly  e edit  d del  o link  c agent  q quit  / find  ⇧↑↓ section"
MODELS = {"claude": ["claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5"],
          "codex": ["gpt-6.1-sol", "gpt-6-astra", "gpt-5.6-sol"]}
CHEERS = ("nice.", "crushed it.", "one down.", "boom.", "look at you go.", "chef's kiss.", "shipped.", "unstoppable.")
G, DRAG = 0.05, 0.92  # particle gravity (rows/frame^2) and air drag; tune to taste
RED, YELLOW, GREEN, CYAN, MAGENTA, BLUE = range(1, 7)
BAR_X = 10  # momentum bar's first column
log = logging.getLogger("todoit")


def now():
    return datetime.now()


def plain(title):
    return LINK.sub(r"\1", title)


def matches(q, text):
    return all(t in text.lower() for t in q.lower().split())


# ---------- model ----------

def parse_due(kind, raw, today):
    """User input -> 'HH:MM' (daily), 'ddd HH:MM' (weekly), or 'YYYY-MM-DD HH:MM' (todo). Raises ValueError/OverflowError."""
    parts = raw.lower().split()
    if not parts:
        raise ValueError("due date required")
    has_time = re.fullmatch(r"\d{1,2}:\d{2}", parts[-1])
    hhmm = datetime.strptime(parts.pop(), "%H:%M").strftime("%H:%M") if has_time else DEFAULT_TIME
    if kind == "daily":
        if parts or not has_time:
            raise ValueError("daily tasks take HH:MM")
        return hhmm
    if kind == "weekly":
        word = " ".join(parts)
        if len(word) < 3 or not any(day.startswith(word) for day in DAYS):
            raise ValueError("weekly tasks take a weekday")
        return f"{next(day[:3] for day in DAYS if day.startswith(word))} {hhmm}"
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
    if kind == "weekly":
        day, hhmm = task["due"].split()
        wd = next(i for i, name in enumerate(DAYS) if name.startswith(day))
        monday = today - timedelta(days=today.weekday())
        return datetime.combine(monday + timedelta(days=wd), datetime.strptime(hhmm, "%H:%M").time())
    return datetime.strptime(task["due"], "%Y-%m-%d %H:%M")


def fresh_sent(kind, due, t):
    """`sent` for a new due. Daily/weekly slots already past stay quiet until the next reset."""
    return ["soon", "missed"] if kind != "todo" and t >= due_at(kind, {"due": due}, t.date()) else []


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
    """Reset dailies at midnight and weeklies on Monday; drop finished todos."""
    today = t.date().isoformat()
    if s["last_reset"] >= today:  # >= so a clock stepping backwards can't re-run it
        return False
    new_week = date.fromisoformat(s["last_reset"]).isocalendar()[:2] != t.date().isocalendar()[:2]
    for kind in (("daily", "weekly") if new_week else ("daily",)):
        for task in s[kind]:
            if not task["done"] and "missed" not in task["sent"]:  # due 23:59, or the Mac slept through it
                when = task["due"].capitalize() if kind == "weekly" else task["due"]
                notify(f"❌ missed · was due {when}", plain(task["title"]), "Basso")
            task["done"], task["sent"] = False, []
    purged = [x["title"] for x in s["todo"] if x["done"]]
    s["todo"] = [x for x in s["todo"] if not x["done"]]
    log.info("rollover %s -> %s: reset %d dailies, %d weeklies, purged done todos %s",
             s["last_reset"], today, len(s["daily"]), len(s["weekly"]) if new_week else 0, purged)
    s["last_reset"] = today
    return True


def check(s, t):
    """Send each banner at most once (dailies re-arm at midnight, weeklies on Monday)."""
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
        s = {"last_reset": t.date().isoformat(), "daily": [], "weekly": [], "todo": []}
        save(s)
    s.setdefault("weekly", [])
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


# ---------- spawn ----------

def slug(title):
    return re.sub(r"[^a-z0-9]+", "-", plain(title).lower())[:40].strip("-") or "task"


def draft(title, cwd):
    ask = (f"Write the prompt a coding agent will receive to work on this todo in {cwd}: {title}\n"
           "One paragraph, under 80 words, concrete, no preamble, no quotes; include any URL from the todo. "
           "Output only the prompt.")
    try:
        r = subprocess.run(DRAFTER, input=ask, capture_output=True, text=True, timeout=30, check=False)
    except OSError as e:
        return None, f"drafter failed to start: {e}"
    except subprocess.TimeoutExpired:
        return None, "drafter timed out"
    if r.returncode:
        detail = r.stderr.strip().splitlines()
        return None, f"drafter exited {r.returncode}: {detail[-1] if detail else 'no stderr'}"
    text = " ".join(r.stdout.replace("\0", " ").split())
    return (text, None) if text else (None, "empty draft")


def tilde(path):
    # ponytail: macOS homes only (`/Users/<user>`); Path.home() is wrong for paths on a remote machine
    return re.sub(r"^/Users/[^/]+", "~", path)


def herdr(*args, machine=None, timeout=90):
    """herdr CLI locally or, given a `herdr machine list --json` entry, on that machine over ssh
    (machines are client-side SSH profiles; each runs its own herdr server)."""
    cmd = ["herdr", *args]
    if machine:
        # ponytail: ConnectTimeout=3, sequential, no threads: an asleep machine costs ~3s before the picker opens; remote listings capped at 10s
        cmd = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", machine["target"],  # -n: never read the TUI's stdin (would eat keystrokes)
               shlex.join(["herdr", "--session", machine["session"], *args])]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except OSError as e:
        raise RuntimeError(f"herdr failed to start: {e}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"herdr {args[0]} {args[1]} timed out")
    if r.returncode:
        try:
            msg = json.loads(r.stderr)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            detail = r.stderr.strip().splitlines()
            msg = detail[-1] if detail else f"herdr exited {r.returncode}"
        raise RuntimeError(msg)
    out = json.loads(r.stdout) if r.stdout.strip() else {}
    return (out.get("result") or {}) if isinstance(out, dict) else out  # `machine list --json` is a bare array, not the API envelope


def remote_workspaces():
    """(machine, workspace) for every workspace on every enabled saved machine. A machine that can't be
    listed (asleep, offline, no key, bad JSON) is logged and skipped so the rest still show."""
    rows = []
    try:
        machines = [m for m in herdr("machine", "list", "--json") if m["enabled"]]
    except (RuntimeError, ValueError, KeyError, TypeError) as e:
        log.error("herdr machine list: %s", e)   # e.g. an older herdr without `machine`: local-only, not fatal
        return rows
    log.info("machines: %s", [m["label"] for m in machines])
    for mc in machines:
        try:
            rows += [(mc, w) for w in herdr("workspace", "list", machine=mc, timeout=10)["workspaces"]]
        except (RuntimeError, ValueError, KeyError, TypeError) as e:
            log.error("%s (%s) skipped: %s", mc["label"], mc["target"], e)
    return rows


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
    elif kind == "weekly":
        text = f"{due:%a} {due:%H:%M}"
    else:
        day = {0: "today", 1: "tomorrow"}.get(days) or (f"{due:%a}" if 1 < days < 7 else f"{due:%-m/%-d}")
        text = f"{day} {due:%H:%M}"
    if task["done"]:
        if len(task.get("done_on", "")) > 10:  # date-only stamps predate completion times
            at = datetime.strptime(task["done_on"], "%Y-%m-%d %H:%M")
            text = f"done {at:%H:%M}" if at.date() == t.date() else f"done {at:%a %H:%M}"
        return text, C(GREEN) | curses.A_DIM
    if t >= due:
        return ("OVERDUE " if kind == "todo" else "missed ") + text, C(RED) | curses.A_BOLD
    if t >= due - HEADS_UP or (kind != "daily" and days == 0):
        return text, C(YELLOW)
    return text, curses.A_DIM


def momentum(s, t):
    """(done, total, due later today, overdue); done/total cover today's plate."""
    tasks = [(k, x, due_at(k, x, t.date())) for k in KINDS for x in s[k]]
    plate = [x for k, x, d in tasks
             if d.date() == t.date() or (not x["done"] and t >= d)
             or (x["done"] and x.get("done_on", "")[:10] == t.date().isoformat())]
    dues = [d for _, x, d in tasks if not x["done"]]
    return (sum(x["done"] for x in plate), len(plate),
            sum(t < d and d.date() == t.date() for d in dues), sum(t >= d for d in dues))


def bar_size(w, done, total):
    """(bar width, filled cells) for the momentum bar, which starts at column BAR_X."""
    bar = max(10, min(30, w - 55))
    return bar, round(bar * done / total) if total else 0


def shimmer(scr, s, f):
    """A glint sweeping along the filled part of the momentum bar, then a short pause off the end."""
    done, total, _, _ = momentum(s, now())
    _, fill = bar_size(scr.getmaxyx()[1], done, total)
    head = 2 * f % (fill + 8)
    for x in range(head - 3, head + 1):
        if 0 <= x < fill:
            put(scr, 1, BAR_X + x, "━", curses.A_BOLD if x == head else C(YELLOW) | curses.A_BOLD)


def draw(scr, s, cur, t, status=None):
    """Render the list; returns the screen row of each task, in cursor order."""
    scr.erase()
    h, w = scr.getmaxyx()
    put(scr, 0, 1, "todoit", C(MAGENTA) | curses.A_BOLD)
    stamp = f"{t:%a %b %-d  %H:%M}"
    put(scr, 0, w - len(stamp) - 1, stamp, curses.A_DIM)
    done, total, today, overdue = momentum(s, t)
    bar, fill = bar_size(w, done, total)
    put(scr, 1, 1, "MOMENTUM", C(MAGENTA) | curses.A_BOLD)
    put(scr, 1, BAR_X, "━" * fill, C(GREEN) | curses.A_BOLD)
    put(scr, 1, BAR_X + fill, "━" * (bar - fill), curses.A_DIM)
    put(scr, 1, BAR_X + bar + 1, f"{done}/{total} today", curses.A_BOLD)
    stats = f"{today} due today · {overdue} overdue"
    put(scr, 1, w - len(stats) - 1, stats, C(RED) | curses.A_BOLD if overdue else curses.A_DIM)
    y, ys = 3, []
    # ponytail: no scrolling; add it when the list outgrows the terminal
    for kind in KINDS:
        tasks = s[kind]
        put(scr, y, 1, "─" * (w - 2), C(CYAN) | curses.A_DIM)
        put(scr, y, 2, f" {kind.upper()} {sum(x['done'] for x in tasks)}/{len(tasks)} ", C(CYAN) | curses.A_BOLD)
        note = {"daily": " resets at midnight ", "weekly": " resets mondays "}.get(kind)
        if note:
            put(scr, y, w - len(note) - 1, note, C(CYAN) | curses.A_DIM)
        y += 1
        if not tasks:
            put(scr, y, 4, f"nothing here, press {dict(daily='r', weekly='w', todo='a')[kind]} to add", curses.A_DIM)
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
    put(scr, h - 1, 1, status or HELP, curses.A_BOLD if status else curses.A_DIM)
    return ys


def edit(scr, ch, text, pos, last_esc):
    """One key of line editing for prompt/pick. Returns (text, pos, last_esc); text None means Esc Esc (cancel)."""
    if ch == "\x1b":
        scr.timeout(0)
        try:
            ch = scr.get_wch()
        except curses.error:
            t = time.monotonic()
            if last_esc is not None and t - last_esc <= DOUBLE_ESC:
                return None, 0, None
            return "", 0, t
        else:
            if ch in ("\x7f", "\b", curses.KEY_BACKSPACE):
                left = re.sub(r"\w*\W*$", "", text[:pos])
                return left + text[pos:], len(left), None
            ch = {"b": b"kLFT3", "f": b"kRIT3"}.get(ch)
        finally:
            scr.timeout(-1)
    if ch in ("\x7f", "\b", curses.KEY_BACKSPACE):
        if pos:
            text, pos = text[:pos - 1] + text[pos:], pos - 1
    elif ch == curses.KEY_LEFT:
        pos = max(0, pos - 1)
    elif ch == curses.KEY_RIGHT:
        pos = min(len(text), pos + 1)
    elif isinstance(ch, str) and ch.isprintable():
        text, pos = text[:pos] + ch + text[pos:], pos + 1
    else:
        name = curses.keyname(ch) if isinstance(ch, int) else ch
        if name == b"kLFT3":
            pos = len(re.sub(r"\w*\W*$", "", text[:pos]))
        elif name == b"kRIT3":
            pos += re.match(r"\W*\w*", text[pos:]).end()
    return text, pos, None


def prompt(scr, label_, text="", keep=None, nav=False, esc_empty=False, on_text=None):
    """Enter submits stripped text, Esc clears, Esc Esc cancels (esc_empty cancels when empty).
    With nav, ← at start returns BACK (saving keep), → at end submits; on_text(text) runs before each draw."""
    curses.curs_set(1)
    scr.timeout(-1)
    pos, offset, last_esc = len(text), 0, None
    try:
        while True:
            if on_text is not None:
                on_text(text)
            h, w = scr.getmaxyx()
            scr.move(h - 1, 0)
            scr.clrtoeol()
            shown_label = fit(label_, max(0, w - 20))  # always leave room to type
            put(scr, h - 1, 1, shown_label, C(YELLOW) | curses.A_BOLD)
            x = min(len(shown_label) + 1, w - 1)
            avail = max(1, w - x)
            offset = min(max(offset, pos - avail + 1), pos)  # scroll only when the cursor would leave the window
            shown = text[offset:offset + avail]
            put(scr, h - 1, x, shown)
            scr.move(h - 1, min(x + pos - offset, w - 1))
            ch = scr.get_wch()
            if ch in ("\n", "\r", curses.KEY_ENTER) or (nav and ch == curses.KEY_RIGHT and pos == len(text)):
                return text.strip()
            if nav and ch == curses.KEY_LEFT and pos == 0:
                if keep is not None:
                    keep[:] = [text]
                return BACK
            was = text
            text, pos, last_esc = edit(scr, ch, text, pos, last_esc)
            if text is None or (esc_empty and last_esc is not None and not was):
                return None
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
    """Prompt for (title, due); re-asks until the due parses and a new todo due is in the future.
    Esc on an empty title cancels; on an empty due goes back to the title. None if cancelled."""
    title, raw = (task["title"], task["due"]) if task else ("", "")
    while True:
        title = prompt(scr, "title: ", title, esc_empty=True)
        if not title:
            return None
        hint = HINT[kind]
        while True:
            raw = prompt(scr, f"{hint}: ", raw, esc_empty=True)
            if raw is None:
                raw = ""
                break
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
            shimmer(scr, s, f)
            fx(f, ps)
            if scr.getch() != -1:
                break
    finally:
        scr.timeout(1000)


def celebrate(scr, s, cur, kind, i, y):
    h, w = scr.getmaxyx()
    if all(x["done"] for x in s[kind]):
        text = {"daily": " ✦ ALL DAILIES DONE ✦ ", "weekly": " ✦ ALL WEEKLIES DONE ✦ ",
                "todo": " ✦ TODO LIST CLEARED ✦ "}[kind]

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


def draw_panel(scr, s, cur, title, lines, focus=0):
    draw(scr, s, cur, now())
    h, w = scr.getmaxyx()
    top = max(2, h - max(4, h // 3))
    if top < h:
        scr.move(top, 0)
        scr.clrtobot()
    put(scr, top, 1, "─" * (w - 2), C(CYAN) | curses.A_DIM)
    put(scr, top, 2, f' spawn agent · "{title}" ', C(CYAN) | curses.A_BOLD)
    rows = h - top - 2
    start = max(0, focus - rows + 1)
    # ponytail: no scrolling; long suggestions clip at the panel bottom (drafter is asked for <80 words)
    for y, line in zip(range(top + 1, h - 1), lines[:1] + lines[1 + start:]):
        put(scr, y, 1, line)
    scr.refresh()


def pick(scr, panel, question, items, search=False, default=None):
    """Menu starting on `default` (an index into items). Enter/→ picks, ← returns BACK, ↑/↓ move.
    Without search, digits pick and j/k move; Esc does nothing. With search, typed text filters (every
    space-separated term a case-insensitive substring), Esc clears and Option+Backspace deletes a word.
    Esc Esc cancels in both modes. With search, ←/→ leave only at the text edges."""
    c, q, pos, last_esc = default or 0, "", 0, None
    scr.timeout(-1)
    try:
        while True:
            hits = [i for i, x in enumerate(items) if matches(q, x)]
            c = max(0, min(c, len(hits) - 1))
            panel([question + (f" {q[:pos]}_{q[pos:]}" if search else "")]
                  + [f"{'▸' if n == c else ' '} " + ("" if search else f"{n + 1} ") + items[i] for n, i in enumerate(hits)], c + 1)
            ch = scr.get_wch()
            if ch != "\x1b":
                last_esc = None
            if ch in ("\n", "\r", curses.KEY_ENTER) or (ch == curses.KEY_RIGHT and (not search or pos == len(q))):
                if hits:
                    return hits[c]
            elif ch == curses.KEY_LEFT and (not search or pos == 0):
                return BACK
            elif ch == curses.KEY_DOWN or (not search and ch == "j"):
                c += 1
            elif ch == curses.KEY_UP or (not search and ch == "k"):
                c -= 1
            elif not search and isinstance(ch, str) and ch in "123456789" and int(ch) <= len(hits):
                return hits[int(ch) - 1]
            elif search or ch == "\x1b":
                q, pos, last_esc = edit(scr, ch, q, pos, last_esc)
                if q is None:
                    return None
    finally:
        scr.timeout(1000)


def spawn(scr, s, cur, task):
    title = plain(task["title"])
    if os.environ.get("HERDR_ENV") != "1":
        log.info("spawn: not inside herdr")
        return "✗ needs herdr"

    def panel(lines, focus=0):
        draw_panel(scr, s, cur, title, lines, focus)
    try:
        rows = [(None, w) for w in herdr("workspace", "list")["workspaces"]]
    except (RuntimeError, ValueError, KeyError) as e:
        log.error("herdr workspace list: %s", e)
        return f"✗ {e}"
    rows += remote_workspaces()
    items = [(f'{mc["label"]} · ' if mc else "") + w["label"]
             + (f'  {tilde(w["worktree"]["checkout_path"])}' if w.get("worktree") else "") for mc, w in rows]
    # Steps: workspace, how?, branch:, harness?, model?, model:, prompt. ← goes back (first step stays); Enter/→ advance.
    # Esc clears, Esc Esc exits; blank stays unless accepting a draft. Conditional steps skip both ways; answers kept; drafts cached per cwd.
    a, k, d, drafts, typed = [None] * 6, 0, 1, {}, [""]
    while True:
        mc, ws = rows[a[0]] if a[0] is not None else (None, {})
        wt = ws.get("worktree") or {}
        checkout = wt.get("checkout_path")
        main = bool(checkout and not wt.get("is_linked_worktree"))  # herdr only creates worktrees from the repo's main checkout
        how = a[1] if main else 0
        harness = list(MODELS)[a[3]] if a[3] is not None else None
        if (k == 1 and not main) or (k == 2 and how != 1) or (k == 5 and a[4] != len(MODELS[harness])):
            k += d
            continue
        if k == 0:
            r = pick(scr, panel, "workspace?", items, search=True, default=a[0])
        elif k == 1:
            r = pick(scr, panel, "how?", ["new tab in it", "new worktree off it"], default=a[1])
        elif k == 2:
            r = prompt(scr, "branch: ", a[2] or slug(task["title"]), nav=True)
        elif k == 3:
            r = pick(scr, panel, "harness?", list(MODELS), default=a[3])
        elif k == 4:
            r = pick(scr, panel, "model?", MODELS[harness] + ["other…"], default=a[4])
        elif k == 5:
            r = prompt(scr, "model: ", a[5] or "", nav=True)
        else:
            cwd = str(Path(checkout).parent / a[2]) if how == 1 else checkout or ws["label"]  # worktree: sibling of the checkout; tab: draft context only
            if cwd not in drafts or drafts[cwd][1]:
                panel(["drafting prompt…"])
                drafts[cwd] = draft(task["title"], cwd)
                curses.flushinp()
                if drafts[cwd][1]:
                    log.error("draft: %s", drafts[cwd][1])
            text, error = drafts[cwd]
            panel(textwrap.wrap(text, max(1, scr.getmaxyx()[1] - 2)) if text else [f"✗ {error}"])
            r = prompt(scr, "prompt (enter = suggested): " if text and not typed[0] else "prompt: ", typed[0], keep=typed, nav=True)
            if r is not None and r is not BACK and (r or text):
                p = r or text
                break
        if r is None:                     # Esc Esc: back to the todo list
            return None
        if r is BACK:                     # ←: previous step; the first step stays put
            k, d = max(0, k - 1), -1
        elif r != "":                     # a blank answer stays on the step
            a[k], d = r, 1
            k += 1
    model = MODELS[harness][a[4]] if a[4] < len(MODELS[harness]) else a[5]
    argv = [harness, "--model", model, "--", p]  # `--` so a prompt starting with `-` is never parsed as a flag
    where = f'{mc["label"]} · ' if mc else ""
    panel([f"spawning {harness} · {model} · {where}{cwd}…"])
    # ponytail: blocking; worktree create freezes the clock a few seconds. thread it if that bites
    # ponytail: prompt rides the shell command line (quoted twice over ssh); if >1KB prompts get eaten during
    # shell startup, switch to agent start + agent prompt
    try:
        if how == 1:
            r = herdr("worktree", "create", "--workspace", ws["workspace_id"], "--branch", a[2],
                      "--path", cwd, "--label", a[2], "--no-focus", machine=mc)
        else:
            r = herdr("tab", "create", "--workspace", ws["workspace_id"], *(["--cwd", checkout] if checkout else []),
                      "--label", title, "--no-focus", machine=mc)
        herdr("pane", "run", r["root_pane"]["pane_id"], shlex.join(argv), machine=mc)
    except (RuntimeError, ValueError, KeyError) as e:
        log.error("spawn in %s%s failed: %s", where, cwd, e)
        return f"✗ {e}"
    finally:
        curses.flushinp()
    log.info("spawned %s %s in %s%s: %s", harness, model, where, cwd, p)
    return f"→ {harness} · {model} · {where}" + (tilde(cwd) if how == 1 else f'{ws["label"]} (new tab)')


def section_start(rows, cur, down):
    """Next section's start going down; current/previous start going up."""
    starts = [n for n, (_, i) in enumerate(rows) if i == 0]
    if down:
        return next((n for n in starts if n > cur), max(0, len(rows) - 1))
    return next((n for n in reversed(starts) if n < cur), 0)


def find(s, rows, cur, q):
    """Next matching task, wrapping once; the current row is checked last."""
    for d in range(1, len(rows) + 1):
        n = (cur + d) % len(rows)
        kind, i = rows[n]
        if matches(q, plain(s[kind][i]["title"])):
            return n


def tui(scr):
    curses.curs_set(0)
    curses.set_escdelay(25)
    curses.use_default_colors()
    for n, color in enumerate((curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_GREEN,
                               curses.COLOR_CYAN, curses.COLOR_MAGENTA, curses.COLOR_BLUE), 1):
        curses.init_pair(n, color, -1)
    scr.timeout(1000)  # tick every second: clock, due colors, midnight rollover
    cur, msg, query = 0, None, ""
    while True:
        s = load()
        rows = [(k, i) for k in KINDS for i in range(len(s[k]))]
        cur = max(0, min(cur, len(rows) - 1))
        ys = draw(scr, s, cur, now(), msg)
        ch = scr.getch()
        if ch not in (-1, curses.KEY_RESIZE):
            msg = None
        # every write reloads first so it doesn't clobber what the notifier just saved
        if ch == ord("q"):
            return
        if ch in (ord("j"), curses.KEY_DOWN):
            cur += 1
        elif ch in (ord("k"), curses.KEY_UP):
            cur -= 1
        elif ch in (curses.KEY_SF, curses.KEY_SR):
            cur = section_start(rows, cur, ch == curses.KEY_SF)
        elif ch in (ord("/"), ord("n")):
            # ponytail: search keeps a snapshot while typing; rollover can reshuffle it on the next load.
            # reload s/rows during the prompt if it bites
            start = cur
            if ch == ord("/"):
                def on_text(q):
                    nonlocal cur
                    hit = find(s, rows, start, q) if q.strip() else None
                    cur = start if hit is None else hit
                    draw(scr, s, cur, now())
                q = prompt(scr, "search: ", esc_empty=True, on_text=on_text)
            else:
                q = query
            cur = start
            if q:
                query = q
                hit = find(s, rows, start, query)
                if hit is None:
                    msg = f"✗ no match: {query}"
                else:
                    cur = hit
        elif ch in (ord("a"), ord("r"), ord("w")):
            kind = {ord("a"): "todo", ord("r"): "daily", ord("w"): "weekly"}[ch]
            got = ask_task(scr, kind)
            if got:
                s = load()
                s[kind].append({"title": got[0], "due": got[1], "done": False, "sent": fresh_sent(kind, got[1], now())})
                save(s)
                log.info("added %s: %s (due %s)", kind, *got)
                cur = sum(len(s[k]) for k in KINDS[:KINDS.index(kind) + 1]) - 1
        elif rows:
            kind, i = rows[cur]
            task = s[kind][i]
            if ch in (ord(" "), ord("x"), 10):
                s = reload(s)
                if s:
                    task = s[kind][i]
                    task["done"] = not task["done"]
                    if task["done"]:
                        task["done_on"] = f"{now():%Y-%m-%d %H:%M}"
                    save(s)
                    log.info("%s: %s", "done" if task["done"] else "undone", task["title"])
                    if task["done"]:
                        celebrate(scr, s, cur, kind, i, ys[cur])
            elif ch == ord("c"):
                msg = spawn(scr, s, cur, task)
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
