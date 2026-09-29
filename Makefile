LABEL := com.todoit.notify
PLIST := $(HOME)/Library/LaunchAgents/$(LABEL).plist
BIN   := $(HOME)/.local/bin/todoit
PY    := $(shell python3 -c 'import sys; print(sys.executable)')

define PLIST_XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$(LABEL)</string>
  <key>ProgramArguments</key><array>
    <string>$(PY)</string><string>$(CURDIR)/todoit.py</string><string>notify</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>TODOIT_HOME</key><string>$(or $(TODOIT_HOME),$(HOME)/.todoit)</string>
  </dict>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>$(or $(TODOIT_HOME),$(HOME)/.todoit)/notify.log</string>
</dict></plist>
endef
export PLIST_XML

.PHONY: run test notify install uninstall

run:
	$(PY) todoit.py

test:
	$(PY) -m unittest -v test_todoit

notify:
	$(PY) todoit.py notify

install:
	chmod +x todoit.py
	mkdir -p $(dir $(BIN)) $(or $(TODOIT_HOME),$(HOME)/.todoit) $(dir $(PLIST))
	ln -sf $(CURDIR)/todoit.py $(BIN)
	printf '%s\n' "$$PLIST_XML" > $(PLIST)
	-launchctl bootout gui/$$(id -u)/$(LABEL) 2>/dev/null && sleep 1  # bootout returns before teardown finishes
	launchctl bootstrap gui/$$(id -u) $(PLIST)
	@echo "installed: run 'todoit'. banners every 5 min via launchd ($(LABEL))"

uninstall:
	-launchctl bootout gui/$$(id -u)/$(LABEL)
	rm -f $(PLIST) $(BIN)
	@echo "uninstalled (tasks kept in ~/.todoit)"
