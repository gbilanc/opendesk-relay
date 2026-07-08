# =============================================================================
# OpenDesk Relay Server — Makefile
# =============================================================================
# Targets:
#   build       — Build a pip wheel in dist/
#   install     — Install wheel + systemd service + config + logrotate
#   uninstall   — Remove all installed files
#   reinstall   — Uninstall + install (handy for dev)
#   clean       — Remove build artifacts
#   dist        — Build wheel and pack into a relocatable tarball
#   distclean   — clean + remove dist/
#   test        — Run test suite
#   lint        — Run ruff & mypy
# =============================================================================

SHELL := /bin/bash

# ── Project metadata ────────────────────────────────────────────────────────
NAME        := opendesk-relay-server
VERSION     := $(shell grep '^version' pyproject.toml | head -1 | sed 's/.*"\(.*\)".*/\1/')
PYTHON      ?= python3
PIP         ?= pip3
BUILD_DIR   := dist

# ── Install paths ───────────────────────────────────────────────────────────
DESTDIR     ?=
PREFIX      ?= /usr/local
SYSCONFDIR  ?= /etc
SYSTEMD_DIR ?= $(DESTDIR)/etc/systemd/system
DEFAULT_DIR ?= $(DESTDIR)/etc/default
LOGROTATE_DIR ?= $(DESTDIR)/etc/logrotate.d
RELAY_CONF_DIR  ?= $(DESTDIR)/etc/opendesk-relay
RELAY_LOG_DIR   ?= $(DESTDIR)/var/log/opendesk-relay
RELAY_DATA_DIR  ?= $(DESTDIR)/var/lib/opendesk-relay

# ── Colors ──────────────────────────────────────────────────────────────────
BLUE  := \033[1;34m
GREEN := \033[1;32m
YELLOW:= \033[1;33m
NC    := \033[0m

.PHONY: all build install uninstall reinstall clean dist distclean test lint help

all: build

# ═══════════════════════════════════════════════════════════════════════════
# build  —  Create a pip wheel
# ═══════════════════════════════════════════════════════════════════════════
build:
	@printf "$(BLUE)► Building wheel $(NAME) v$(VERSION)...$(NC)\n"
	@if command -v uv &>/dev/null; then \
		uv build --wheel; \
	else \
		$(PYTHON) -m build --wheel; \
	fi
	@printf "$(GREEN)✔ Wheel created:$(NC)\n"
	@ls -lh $(BUILD_DIR)/*.whl

# ═══════════════════════════════════════════════════════════════════════════
# install  —  Install wheel + system files (run as root)
# ═══════════════════════════════════════════════════════════════════════════
install: build
	@printf "$(BLUE)► Installing $(NAME) v$(VERSION)...$(NC)\n\n"

# 1. Install the Python wheel into a dedicated venv
	@printf "  → Creating virtual environment...\n"
	@install -d -m 0755 "$(RELAY_DATA_DIR)"
	@uv venv "$(RELAY_DATA_DIR)/venv" 2>/dev/null || true
	@printf "  → Installing wheel into venv...\n"
	@uv pip install --python "$(RELAY_DATA_DIR)/venv/bin/python" $(BUILD_DIR)/*.whl
	@printf "  → Symlinking binary...\n"
	@install -d -m 0755 "$(BINDIR)"
	@ln -sf "$(RELAY_DATA_DIR)/venv/bin/relay-server" "$(BINDIR)/relay-server"
	@printf "  $(GREEN)✔$(NC) Package installed into venv: $(RELAY_DATA_DIR)/venv\n"

# 2. Create directories
	@install -d -m 0755 "$(RELAY_CONF_DIR)"
	@install -d -m 0755 "$(RELAY_LOG_DIR)"
	@install -d -m 0755 "$(RELAY_DATA_DIR)"
	@install -d -m 0750 "$(SYSTEMD_DIR)"
	@install -d -m 0755 "$(DEFAULT_DIR)"
	@install -d -m 0755 "$(LOGROTATE_DIR)"
	@printf "  $(GREEN)✔$(NC) Directories created\n\n"

# 3. Install config
	@if [ ! -f "$(RELAY_CONF_DIR)/relay-config.yaml" ]; then \
		install -m 0644 relay-config.yaml "$(RELAY_CONF_DIR)/relay-config.yaml"; \
		printf "  $(GREEN)✔$(NC) Config installed: $(RELAY_CONF_DIR)/relay-config.yaml\n"; \
	else \
		install -m 0644 relay-config.yaml "$(RELAY_CONF_DIR)/relay-config.yaml.example"; \
		printf "  $(YELLOW)⚠$(NC) Config exists, installed as example: $(RELAY_CONF_DIR)/relay-config.yaml.example\n"; \
	fi
	@printf "\n"

# 4. Install systemd service
	@install -m 0644 deploy/opendesk-relay.service "$(SYSTEMD_DIR)/opendesk-relay.service"
	@printf "  $(GREEN)✔$(NC) systemd unit: $(SYSTEMD_DIR)/opendesk-relay.service\n\n"

# 5. Install default environment file
	@install -m 0644 deploy/opendesk-relay.default "$(DEFAULT_DIR)/opendesk-relay"
	@printf "  $(GREEN)✔$(NC) Defaults file: $(DEFAULT_DIR)/opendesk-relay\n\n"

# 6. Install logrotate config
	@install -m 0644 deploy/opendesk-relay.logrotate "$(LOGROTATE_DIR)/opendesk-relay"
	@printf "  $(GREEN)✔$(NC) Logrotate config: $(LOGROTATE_DIR)/opendesk-relay\n\n"

# 7. Reload systemd
	@systemctl daemon-reload 2>/dev/null || true
	@printf "  $(GREEN)✔$(NC) systemd reloaded\n\n"

	@printf "$(GREEN)═══════════════════════════════════════════════════════════════$(NC)\n"
	@printf "$(GREEN)✔ Installation complete!$(NC)\n"
	@printf "$(GREEN)═══════════════════════════════════════════════════════════════$(NC)\n"
	@printf "\n"
	@printf "  Next steps:\n"
	@printf "\n"
	@printf "  1. Edit config:    nano $(RELAY_CONF_DIR)/relay-config.yaml\n"
	@printf "  2. Start service:  sudo systemctl enable --now opendesk-relay\n"
	@printf "  3. Check status:   sudo systemctl status opendesk-relay\n"
	@printf "  4. View logs:      sudo journalctl -u opendesk-relay -f\n"
	@printf "\n"

# ═══════════════════════════════════════════════════════════════════════════
# uninstall  —  Remove all installed files (run as root)
# ═══════════════════════════════════════════════════════════════════════════
uninstall:
	@printf "$(BLUE)► Uninstalling $(NAME)...$(NC)\n\n"

# 1. Stop & disable service
	@if systemctl is-active --quiet opendesk-relay 2>/dev/null; then \
		systemctl stop opendesk-relay; \
		printf "  $(GREEN)✔$(NC) Service stopped\n"; \
	fi
	@if systemctl is-enabled --quiet opendesk-relay 2>/dev/null; then \
		systemctl disable opendesk-relay; \
		printf "  $(GREEN)✔$(NC) Service disabled\n"; \
	fi

# 2. Remove system files
	@rm -f "$(SYSTEMD_DIR)/opendesk-relay.service"
	@rm -f "$(DEFAULT_DIR)/opendesk-relay"
	@rm -f "$(LOGROTATE_DIR)/opendesk-relay"
	@printf "  $(GREEN)✔$(NC) System files removed\n\n"

# 3. Remove config
	@if [ -f "$(RELAY_CONF_DIR)/relay-config.yaml" ]; then \
		rm -f "$(RELAY_CONF_DIR)/relay-config.yaml"; \
		printf "  $(GREEN)✔$(NC) Config removed\n"; \
	fi
	@rm -f "$(RELAY_CONF_DIR)/relay-config.yaml.example"
	@rmdir "$(RELAY_CONF_DIR)" 2>/dev/null || true

# 4. Remove log dir (keep data)
	@rmdir "$(RELAY_LOG_DIR)" 2>/dev/null || true

# 5. Remove venv and symlink
	@rm -f "$(BINDIR)/relay-server"
	@rm -rf "$(RELAY_DATA_DIR)/venv"
	@printf "  $(GREEN)✔$(NC) Virtual environment and symlink removed\n\n"

# 6. Reload systemd
	@systemctl daemon-reload 2>/dev/null || true
	@printf "  $(GREEN)✔$(NC) systemd reloaded\n\n"

	@printf "$(GREEN)✔ Uninstall complete.$(NC) Data dir left intact: $(RELAY_DATA_DIR)\n"

# ═══════════════════════════════════════════════════════════════════════════
# reinstall  —  Uninstall + install
# ═══════════════════════════════════════════════════════════════════════════
reinstall: uninstall install

# ═══════════════════════════════════════════════════════════════════════════
# clean  —  Remove build/temp files
# ═══════════════════════════════════════════════════════════════════════════
clean:
	@rm -rf *.egg-info build __pycache__
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name '*.pyc' -delete
	@printf "$(GREEN)✔ Cleaned build artifacts$(NC)\n"

# ═══════════════════════════════════════════════════════════════════════════
# distclean  —  clean + remove wheel
# ═══════════════════════════════════════════════════════════════════════════
distclean: clean
	@rm -rf $(BUILD_DIR)
	@printf "$(GREEN)✔ Removed $(BUILD_DIR)/$(NC)\n"

# ═══════════════════════════════════════════════════════════════════════════
# dist  —  Build wheel + tarball for distribution
# ═══════════════════════════════════════════════════════════════════════════
dist: build
	@printf "$(BLUE)► Creating distribution tarball...$(NC)\n"
	@TARDIR="$(NAME)-$(VERSION)"; \
	TARFILE="$(CURDIR)/$(BUILD_DIR)/$${TARDIR}.tar.gz"; \
	rm -rf "/tmp/$${TARDIR}"; \
	mkdir -p "/tmp/$${TARDIR}/deploy" "/tmp/$${TARDIR}/dist"; \
	cp $(BUILD_DIR)/*.whl "/tmp/$${TARDIR}/dist/"; \
	cp deploy/* "/tmp/$${TARDIR}/deploy/"; \
	cp relay-config.yaml Makefile install-relay.sh restart-relay.sh README.md pyproject.toml MANIFEST.in .gitignore "/tmp/$${TARDIR}/"; \
	cd /tmp && tar czf "$${TARFILE}" "$${TARDIR}"; \
	rm -rf "/tmp/$${TARDIR}"; \
	printf "$(GREEN)✔ Tarball created:$(NC)\n"; \
	ls -lh "$${TARFILE}"

# ═══════════════════════════════════════════════════════════════════════════
# test  —  Run the test suite
# ═══════════════════════════════════════════════════════════════════════════
test:
	@printf "$(BLUE)► Running tests...$(NC)\n"
	@$(PYTHON) -m pytest tests/ -v --tb=short $(ARGS)
	@printf "$(GREEN)✔ Tests passed$(NC)\n"

# ═══════════════════════════════════════════════════════════════════════════
# lint  —  Run linters
# ═══════════════════════════════════════════════════════════════════════════
lint:
	@printf "$(BLUE)► Running ruff...$(NC)\n"
	@$(PYTHON) -m ruff check src/ tests/
	@printf "$(BLUE)► Running mypy...$(NC)\n"
	@$(PYTHON) -m mypy src/ --no-strict-optional
	@printf "$(GREEN)✔ Lint passed$(NC)\n"

# ═══════════════════════════════════════════════════════════════════════════
# help  —  Show this help
# ═══════════════════════════════════════════════════════════════════════════
help:
	@printf "$(BLUE)OpenDesk Relay Server — Makefile$(NC)\n"
	@printf "$(BLUE)=============================$(NC)\n\n"
	@printf "  $(GREEN)build$(NC)       Build pip wheel in dist/\n"
	@printf "  $(GREEN)install$(NC)     Install wheel + system files (as root)\n"
	@printf "  $(GREEN)uninstall$(NC)   Remove all installed files\n"
	@printf "  $(GREEN)reinstall$(NC)   Uninstall + install\n"
	@printf "  $(GREEN)clean$(NC)       Remove build artifacts\n"
	@printf "  $(GREEN)dist$(NC)        Build wheel + distribution tarball\n"
	@printf "  $(GREEN)test$(NC)        Run tests\n"
	@printf "  $(GREEN)lint$(NC)        Run ruff + mypy\n"
	@printf "  $(GREEN)help$(NC)        Show this help\n"
