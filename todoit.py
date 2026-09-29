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
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

DB = Path(os.environ.get("TODOIT_HOME", Path.home() / ".todoit")) / "tasks.json"
CONFIG = DB.parent / "config.json"
# skip user settings (plugins, hooks) and MCP servers: inheriting them is ~230K+ input tokens and ~9s per turn vs ~3.5s
DEFAULT_AGENT = ["claude", "-p", "--model", "claude-sonnet-5-5", "--tools", "", "--no-session-persistence",
                 "--strict-mcp-config", "--setting-sources", ""]
HEADS_UP = timedelta(minutes=30)
DEFAULT_TIME = "17:00"
KINDS = ("daily", "weekly", "todo")
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
URL = re.compile(r"https?://[^\s)]+")
HINT = {"daily": "due HH:MM", "weekly": "due DAY [HH:MM]", "todo": "due today|tomorrow|+N|fri|MM-DD|YYYY-MM-DD [HH:MM]"}
HELP = "space done a/r/w add todo/daily/weekly  e edit  d del  o link  c chat  q quit"
CHEERS = ("nice.", "crushed it.", "one down.", "boom.", "look at you go.", "chef's kiss.", "shipped.", "unstoppable.")
G, DRAG = 0.05, 0.92  # particle gravity (rows/frame^2) and air drag; tune to taste
RED, YELLOW, GREEN, CYAN, MAGENTA, BLUE = range(1, 7)
BAR_X = 10  # momentum bar's first column
log = logging.getLogger("todoit")


def now():
    return datetime.now()


def plain(title):
    return LINK.sub(r"\1", title)


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


# ---------- agent ----------

def agent_argv():
    try:
        config = json.loads(CONFIG.read_text())
    except FileNotFoundError:
        return DEFAULT_AGENT
    argv = config.get("agent", DEFAULT_AGENT) if isinstance(config, dict) else None
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) for x in argv):
        raise ValueError("config agent must be a non-empty argv list")
    return argv


def agent_prompt(s, turns, message):
    tasks = {kind: [{"i": i, "title": x["title"], "due": x["due"], "done": x["done"]}
                    for i, x in enumerate(s[kind])] for kind in KINDS}
    return (
        "You help change a todo list. You have no tools; the app applies changes after user confirmation.\n"
        "daily repeats every day, due HH:MM, and is unchecked at midnight. "
        "weekly repeats every week, due ddd HH:MM (e.g. fri 09:00), and is unchecked on Mondays. "
        "todo is one-off, due YYYY-MM-DD HH:MM (17:00 if the user gives no time), and done todos are purged at midnight. "
        "Titles may contain markdown links [text](url). Indices i refer to the tasks shown.\n"
        "Reply with ONLY a JSON object: "
        '{"reply":"short message to the user","ops":['
        '{"op":"add","kind":"todo|daily|weekly","title":"...","due":"..."},'
        '{"op":"edit","kind":"todo|daily|weekly","i":0,"title":"...","due":"...","done":true},'
        '{"op":"delete","kind":"todo|daily|weekly","i":0}]}. '
        "kind is todo, daily or weekly. For edit, include only fields to change. "
        "Use ops: [] when just answering.\n"
        f"Local time: {now():%Y-%m-%d %H:%M %A}\n"
        f"Tasks: {json.dumps(tasks, ensure_ascii=False)}\n"
        f"Chat so far: {json.dumps(turns, ensure_ascii=False)}\n"
        f"New user message: {message}"
    )


def parse_agent_reply(raw):
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict) or not isinstance(data.get("reply"), str) or not isinstance(data.get("ops"), list):
        raise ValueError("expected reply string and ops list")
    return data


def call_agent(argv, message):
    # ponytail: blocking call, no spinner/cancel; add async execution if 120 seconds becomes painful
    try:
        result = subprocess.run(argv, input=message, capture_output=True, text=True, timeout=120, check=False)
    except OSError as e:
        return None, f"agent failed to start: {e}"
    except subprocess.TimeoutExpired:
        return None, "agent timed out"
    if result.returncode:
        detail = result.stderr.strip().splitlines()
        error = f"agent exited {result.returncode}: {detail[-1] if detail else 'no stderr'}"
        return None, error
    try:
        return parse_agent_reply(result.stdout), None
    except ValueError as e:
        return None, f"invalid agent reply: {e}"


def validate_op(raw, s, t, used):
    if not isinstance(raw, dict):
        raise ValueError("op must be an object")
    action, kind = raw.get("op"), raw.get("kind")
    if action not in ("add", "edit", "delete") or kind not in KINDS:
        raise ValueError("invalid op or kind")
    op = {"op": action, "kind": kind}
    if action != "add":
        i = raw.get("i")
        if type(i) is not int or not 0 <= i < len(s[kind]):
            raise ValueError("index out of range")
        if (kind, i) in used:
            raise ValueError("duplicate task index")
        op["i"] = i
    if action == "edit" and not any(k in raw for k in ("title", "due", "done")):
        raise ValueError("edit needs a field")
    if action == "add" or action == "edit" and "title" in raw:
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title required")
        if not title.isprintable():
            raise ValueError("title has control characters")
        op["title"] = title.strip()
    if action == "add" or action == "edit" and "due" in raw:
        due = raw.get("due")
        if not isinstance(due, str):
            raise ValueError("due must be text")
        due = parse_due(kind, due, t.date())
        if kind == "todo" and (action == "add" or due != s[kind][op["i"]]["due"]) and due_at(kind, {"due": due}, t.date()) <= t:
            raise ValueError("due is in the past")
        op["due"] = due
    if action == "edit" and "done" in raw:
        if type(raw["done"]) is not bool:
            raise ValueError("done must be boolean")
        op["done"] = raw["done"]
    if action != "add":
        used.add((kind, op["i"]))
    return op


def validate_ops(raw_ops, s, t, used):
    valid, invalid = [], []
    for raw in raw_ops:
        try:
            valid.append(validate_op(raw, s, t, used))
        except (ValueError, OverflowError) as e:
            invalid.append((raw, str(e)))
    return valid, invalid


def propose(argv, prompt_text, s, t):
    answer, error = call_agent(argv, prompt_text)
    if error:
        return None, [], [], error
    used = set()
    valid, pending = validate_ops(answer["ops"], s, t, used)
    for n in range(3):
        if not pending:
            break
        errors = [{"op": raw, "error": why} for raw, why in pending]
        log.info("agent retry %d: %s", n + 1, json.dumps(errors, ensure_ascii=False))
        retry = (prompt_text + "\nInvalid ops: " + json.dumps(errors, ensure_ascii=False)
                 + "\nReturn the same JSON shape with corrected replacements for ONLY those invalid ops, in order.")
        fixed, error = call_agent(argv, retry)
        if error:
            break
        replacements = fixed["ops"][:len(pending)]
        added, bad = validate_ops(replacements, s, t, used)
        valid.extend(added)
        pending = bad + pending[len(replacements):]
    return answer["reply"], valid, pending, error


def apply_ops(s, ops):
    fresh = reload(s)
    if fresh is None:
        return None
    for op in (x for x in ops if x["op"] == "edit"):
        task = fresh[op["kind"]][op["i"]]
        if "due" in op and op["due"] != task["due"]:
            task["sent"] = fresh_sent(op["kind"], op["due"], now())
        task.update({k: v for k, v in op.items() if k in ("title", "due", "done")})
        log.info("agent edited %s: %s (due %s)", op["kind"], task["title"], task["due"])
    for op in sorted((x for x in ops if x["op"] == "delete"), key=lambda x: x["i"], reverse=True):
        log.info("agent deleted %s: %s", op["kind"], fresh[op["kind"]].pop(op["i"])["title"])
    for op in ops:
        if op["op"] == "add":
            fresh[op["kind"]].append({"title": op["title"], "due": op["due"], "done": False,
                                      "sent": fresh_sent(op["kind"], op["due"], now())})
            log.info("agent added %s: %s (due %s)", op["kind"], op["title"], op["due"])
    save(fresh)
    return fresh


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
             or (x["done"] and x.get("done_on") == t.date().isoformat())]
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


def draw(scr, s, cur, t):
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


def draw_chat(scr, s, cur, transcript):
    draw(scr, s, cur, now())
    h, w = scr.getmaxyx()
    top = max(2, h - max(4, h // 3))
    if top < h:
        scr.move(top, 0)
        scr.clrtobot()
    put(scr, top, 1, "─" * (w - 2), C(CYAN) | curses.A_DIM)
    put(scr, top, 2, " chat ", C(CYAN) | curses.A_BOLD)
    lines = []
    for role, message in transcript:
        line = f"{role}: {message}" if role else message
        lines.extend(textwrap.wrap(line.replace("\0", ""), width=max(1, w - 2)) or [""])
    for y, line in zip(range(top + 1, h - 1), lines[-(h - top - 2):]):
        put(scr, y, 1, line)


def proposal_line(op, s):
    kind = op["kind"]
    # done=True: label() without the OVERDUE/missed prefix
    show = {"title": plain, "due": lambda d: label(kind, {"due": d, "done": True}, now())[0], "done": str}
    if op["op"] == "add":
        return f'+ {kind} "{plain(op["title"])}" due {show["due"](op["due"])}'
    old = plain(s[kind][op["i"]]["title"])
    if op["op"] == "delete":
        return f'- {kind} "{old}"'
    return f'~ {kind} "{old}": ' + ", ".join(f"{k} → {show[k](op[k])}" for k in ("title", "due", "done") if k in op)


def chat(scr, cur):
    transcript = []
    while True:
        s = load()
        draw_chat(scr, s, cur, transcript)
        message = prompt(scr, "you: ")
        if not message:
            return
        turns = [{"role": role, "text": line} for role, line in transcript if role in ("you", "agent")]
        prompt_text = agent_prompt(s, turns, message)
        transcript.append(("you", message))
        log.info("agent asked: %s", message)
        draw_chat(scr, s, cur, transcript + [(None, "thinking…")])
        scr.refresh()
        try:
            argv = agent_argv()
        except (ValueError, OSError) as e:
            log.error("agent config: %s", e)
            transcript.append((None, f"✗ config: {e}"))
            continue
        reply, ops, skipped, error = propose(argv, prompt_text, s, now())
        curses.flushinp()
        if error:
            log.error("agent: %s", error)
        if reply is None:
            transcript.append((None, f"✗ {error}"))
            continue
        transcript.append(("agent", reply))
        if error:
            transcript.append((None, f"✗ {error}"))
        for op in ops:
            transcript.append((None, proposal_line(op, s)))
        for raw, why in skipped:
            transcript.append((None, f"✗ skipped: {json.dumps(raw, ensure_ascii=False)} ({why})"))
        if ops:
            draw_chat(scr, s, cur, transcript)
            n = f"{len(ops)} change{'s' * (len(ops) != 1)}"
            if confirm(scr, f"apply {n}? y/n"):
                if apply_ops(s, ops) is None:
                    log.info("agent batch not applied: midnight rollover")
                    transcript.append((None, "list changed at midnight, not applied"))
                else:
                    transcript.append((None, f"applied {n}"))
            else:
                log.info("agent batch declined: %s", n)
                transcript.append((None, "not applied"))


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
        elif ch == ord("c"):
            chat(scr, cur)
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
                        task["done_on"] = now().date().isoformat()
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
