# battery-telemetry — Mac setup helpers
# Run `make` or `make help` to list targets.

PYTHON      := .venv/bin/python
PIP         := .venv/bin/pip
MAC_DIR     := mac
SCRIPT      := $(MAC_DIR)/battery_to_mqtt.py
CONFIG      := $(MAC_DIR)/config.json
EXAMPLE     := $(MAC_DIR)/config.example.json
LABEL       := com.battery-telemetry.mac
SERVICE     := gui/$(shell id -u)/$(LABEL)
LOG         := $(MAC_DIR)/battery-telemetry.log

.DEFAULT_GOAL := help

## help: Show this help
.PHONY: help
help:
	@echo "battery-telemetry (mac) — available targets:"
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /'

## venv: Create the virtualenv and install dependencies
.PHONY: venv
venv: $(PYTHON)
$(PYTHON):
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

## deps: Reinstall/upgrade dependencies into the venv
.PHONY: deps
deps: venv
	$(PIP) install --upgrade -r requirements.txt

## config: Create mac/config.json from the example (won't overwrite)
.PHONY: config
config:
	@if [ -f $(CONFIG) ]; then \
		echo "$(CONFIG) already exists — leaving it untouched."; \
	else \
		cp $(EXAMPLE) $(CONFIG); \
		echo "Created $(CONFIG) — edit it with your MQTT broker host/credentials."; \
	fi

## dry-run: Print what would be published (no broker connection)
.PHONY: dry-run
dry-run: venv
	$(PYTHON) $(SCRIPT) --dry-run

## run: Publish battery state to MQTT once
.PHONY: run
run: venv $(CONFIG)
	$(PYTHON) $(SCRIPT)

## prune-legacy-bt: Clear retained topics from the old per-Mac Bluetooth scheme
.PHONY: prune-legacy-bt
prune-legacy-bt: venv $(CONFIG)
	$(PYTHON) $(SCRIPT) --prune-legacy-bt

## install: Install & start the launchd job (runs on interval_seconds)
.PHONY: install
install: venv $(CONFIG)
	$(MAC_DIR)/install.sh

## start: Trigger one run of the installed launchd job now
.PHONY: start
start:
	launchctl kickstart -k $(SERVICE)

## status: Show whether the launchd job is loaded
.PHONY: status
status:
	@launchctl print $(SERVICE) 2>/dev/null | grep -E 'state =|last exit' || echo "$(LABEL) is not loaded."

## logs: Tail the launchd job's log
.PHONY: logs
logs:
	@touch $(LOG); tail -f $(LOG)

## uninstall: Stop and remove the launchd job
.PHONY: uninstall
uninstall:
	-launchctl bootout $(SERVICE) 2>/dev/null
	-rm -f $$HOME/Library/LaunchAgents/$(LABEL).plist
	@echo "Uninstalled $(LABEL)."

## clean: Remove the virtualenv and caches
.PHONY: clean
clean:
	rm -rf .venv $(MAC_DIR)/__pycache__

$(CONFIG):
	@echo "Missing $(CONFIG). Run 'make config' and edit it first." >&2
	@exit 1
