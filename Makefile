# Local entry points; CI (owner-managed, outside this repo's plans) calls these.
PY ?= python3
PY39 ?= python3.9
RUFF ?= ruff
PYRIGHT ?= pyright

.PHONY: test test-3.9 lint fmt typecheck

test:
	$(PY) -m pytest

# The 3.9 runtime floor is checked by running the suite on a 3.9 interpreter,
# not by convention. Point PY39 at one (with pytest available) if it is not on PATH.
test-3.9:
	@if command -v $(PY39) >/dev/null 2>&1; then \
		$(PY39) -m pytest; \
	else \
		echo "SKIP: no Python 3.9 interpreter found — the 3.9 floor was NOT checked."; \
		echo "      Install one and re-run, e.g.: make test-3.9 PY39=/path/to/python3.9"; \
	fi

lint:
	$(RUFF) check .
	$(RUFF) format --check .

# Static type check (dev-only; the runtime stays stdlib-only). Scoped to the annotated
# store surface via [tool.pyright] in pyproject.toml. Point PYRIGHT at a binary if it is
# not on PATH.
typecheck:
	$(PYRIGHT)

fmt:
	$(RUFF) format .
