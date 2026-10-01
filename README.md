# todoit

Terminal todo list: daily tasks that reset at midnight, weekly tasks that reset Mondays, ad-hoc tasks below, macOS banners before things are due and when you miss them. Python 3 stdlib only.

```
make test       # unit tests
make run        # open the TUI (same as `todoit` after install)
make install    # symlink ~/.local/bin/todoit + load the launchd notifier (every 5 min)
make uninstall  # stop banners, remove symlink (keeps ~/.todoit/tasks.json)
```

**Keys:** `space`/`x` done · `a` add todo · `r` add daily · `w` add weekly · `e` edit · `d` delete · `o` open link · `c` spawn agent · `j`/`k` move · `q` quit · in prompts: `esc` clear · `esc esc` cancel · `⌥⌫` delete word

**Due formats:** daily `HH:MM`. Weekly `DAY [HH:MM]` (default 17:00). Todo `today|tomorrow|+N|fri|MM-DD|YYYY-MM-DD` plus optional `HH:MM` (default 17:00).

**Banners:** `⏰` 30 min before due, `❌` once it's past due. Each fires once per task; dailies re-arm at midnight, weeklies on Monday, editing a due time re-arms it.
Sent via `osascript`, so macOS lists them under **Script Editor**: System Settings → Notifications → Script Editor → allow, style *Banners*.
Log: `~/.todoit/todoit.log` (banners sent/failed, midnight resets, adds/edits/deletes, agent spawns). Crash tracebacks: `~/.todoit/notify.log`.
Set `TODOIT_HOME` to use a different data dir (re-run `make install` so the notifier follows).

**Agent:** Press `c` on a task to spawn a coding agent on it. Todoit must run inside herdr (`HERDR_ENV=1`), otherwise it shows `✗ needs herdr`. Pick an open herdr workspace (type to filter; todoit never creates workspaces, so open one in herdr first). Workspaces on machines saved with `herdr machine add` (enabled ones) are listed after the local ones, prefixed with the machine label (`mac-mini · arch-world`), so typing the label filters to them; a machine that is asleep or unreachable is skipped after ~3s (logged) and the rest still show. A repo's main-checkout workspace offers a new tab in it or a new worktree off it (branch prefilled from the title, created next to the checkout: `~/vapi/repo/main` → `~/vapi/repo/<branch>`); other worktrees and non-git workspaces get a new tab (herdr only creates worktrees from the repo's main workspace). Then pick a harness (claude/codex), a model (presets or `other…`), and a prompt drafted by `claude -p --model claude-sonnet-5-5 --tools '' --no-session-persistence --strict-mcp-config --setting-sources ''`. The last two flags keep your plugins, hooks and MCP servers out of the call; with them loaded, each turn sends ~230K+ tokens and takes ~9s instead of ~3.5s. Enter or → accepts and moves on, typing replaces the suggestion. ← goes back a step (earlier answers and typed prompt text are kept; the drafted prompt is reused for the same directory); a blank branch/model answer stays put, a blank prompt accepts the suggestion. Esc clears what you typed and Esc Esc at any step returns to the list; nothing is created until the prompt is submitted. The agent starts in the new tab's or worktree's pane with the prompt, on the machine that owns the workspace (herdr runs there via `ssh -n -o BatchMode=yes`, so `herdr` must be on that machine's non-interactive ssh PATH and `claude`/`codex` on its interactive shell PATH; the prompt is always drafted locally); focus stays in todoit.
