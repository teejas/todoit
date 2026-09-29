# todoit

Terminal todo list: daily tasks that reset at midnight, ad-hoc tasks below, macOS banners before things are due and when you miss them. Python 3 stdlib only.

```
make test       # unit tests
make run        # open the TUI (same as `todoit` after install)
make install    # symlink ~/.local/bin/todoit + load the launchd notifier (every 5 min)
make uninstall  # stop banners, remove symlink (keeps ~/.todoit/tasks.json)
```

**Keys:** `space`/`x` done · `a` add todo · `r` add daily · `e` edit · `d` delete · `o` open link · `j`/`k` move · `q` quit

**Due formats:** daily `HH:MM`. Todo `today|tomorrow|+N|fri|MM-DD|YYYY-MM-DD` plus optional `HH:MM` (default 17:00).

**Banners:** `⏰` 30 min before due, `❌` once it's past due. Each fires once per task; dailies re-arm at midnight, editing a due time re-arms it.
Sent via `osascript`, so macOS lists them under **Script Editor**: System Settings → Notifications → Script Editor → allow, style *Banners*.
Log: `~/.todoit/todoit.log` (banners sent/failed, midnight resets, adds/edits/deletes). Crash tracebacks: `~/.todoit/notify.log`.
Set `TODOIT_HOME` to use a different data dir (re-run `make install` so the notifier follows).
