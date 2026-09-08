# Local entry points; CI (owner-managed, outside this repo's plans) calls these.
#
# Everything runs through `uv run`, so the interpreter is the one .python-version pins and the
# dependencies are the ones uv.lock pins — the same pair a user's install gets. `make bootstrap`
# is the only target that works before that exists; CI must run it first, or have uv present.
UV ?= uv
RUN ?= $(UV) run

DIST ?= dist
VERSION = $(shell $(RUN) python -c "import sys; sys.path.insert(0, 'src'); \
	import sellee; print(sellee.__version__)")
STAGE = $(DIST)/sellee-$(VERSION)

.PHONY: bootstrap test test-serial lint fmt typecheck dist diagrams

# Provision the toolchain this repo builds against: uv itself if it is missing or too old, the
# pinned interpreter, then the dev dependency set. ./setup does the same thing for a user, from
# the same pin file — this target is the developer's door to it.
bootstrap:
	@./setup --bootstrap-only --with-dev

# Runs tests in parallel.
test:
	$(RUN) python -m pytest -n auto --dist worksteal

# Runs tests serially.
test-serial:
	$(RUN) python -m pytest

# Open the buyer simulator against a daemon started by buyer-daemon below. Runs the CLI from
# this checkout, which matters: the installed release on PATH is a different build.
buyer-sim:
	PYTHONPATH=src $(RUN) python -c "from sellee import cli; cli.main()" buyer

# The daemon the simulator needs, in the foreground, from this checkout.
#
# Two reasons it cannot be `sellee daemon start`. That command runs the *installed* release, which
# will not carry unreleased code; and the simulator is switched on by an environment variable, so
# it has to be set on the daemon process itself rather than on whatever asks it for a page.
# Stop any running daemon first — it holds the HTTP port.
buyer-daemon:
	SELLEE_BUYER_SIM=1 PYTHONPATH=src $(RUN) python -c "from sellee import cli; cli.main()" daemon run

# Regenerate all diagrams (SVG + PNG) under docs/.
diagrams:
	docs/generate-diagrams.sh

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .

# Static type check (dev-only, never shipped). Scoped to the annotated store surface via
# [tool.pyright] in pyproject.toml.
typecheck:
	$(RUN) pyright

fmt:
	$(RUN) ruff format .

# The release artifact: the same tree ./setup stages into versions/<v>, plus the checksum file
# that both `sellee update` and install.sh read the version out of. Publishing is manual
# (`gh release create`) until a cadence justifies automating it.
dist:
	@rm -rf $(STAGE) $(DIST)/sellee-$(VERSION).tar.gz $(DIST)/SHA256SUMS
	@mkdir -p $(STAGE)
	@cp -R bin src $(STAGE)/
	@cp README.md $(STAGE)/ 2>/dev/null || true
# Not conditional: a release artifact without its license terms should fail to build, not ship.
	@cp LICENSE NOTICE $(STAGE)/
	@cp setup $(STAGE)/
# The runtime description travels with the release: without these a version cannot install its
# own dependencies. Kept in step with VERSION_FILES in installer/materialize.py, which a test pins.
	@cp pyproject.toml uv.lock .python-version $(STAGE)/
	@find $(STAGE) -name '__pycache__' -type d -prune -exec rm -rf {} +
	@find $(STAGE) -name '*.py[co]' -delete
# COPYFILE_DISABLE: macOS tar otherwise writes an AppleDouble `._name` entry beside every file
# carrying extended attributes, and those ship inside the published archive.
	@COPYFILE_DISABLE=1 tar -czf $(DIST)/sellee-$(VERSION).tar.gz -C $(DIST) sellee-$(VERSION)
	@rm -rf $(STAGE)
	@cd $(DIST) && { shasum -a 256 sellee-$(VERSION).tar.gz 2>/dev/null \
		|| sha256sum sellee-$(VERSION).tar.gz; } > SHA256SUMS
	@echo "$(DIST)/sellee-$(VERSION).tar.gz"
	@cat $(DIST)/SHA256SUMS
