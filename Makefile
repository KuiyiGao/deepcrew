PY ?= python3
PYTEST = $(PY) -B -m pytest -q -p no:cacheprovider

.PHONY: verify lint test e2e mod-check

verify: lint test

lint:
	$(PY) -m ruff check --no-cache .

test:
	$(PYTEST) -m "not e2e"

e2e:
	$(PYTEST) -m e2e

CLAUDE ?= claude
mod-check:
	"$(CLAUDE)" plugin validate mod
	"$(CLAUDE)" plugin test mod
