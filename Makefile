.PHONY: install install-from-source test build release

# One-line install for alpha testers (needs a published release).
install:
	curl -fsSL https://raw.githubusercontent.com/sameer-hoda/Nembu-Alpha-Tester/main/install.sh | bash

# Developer install from this checkout.
install-from-source:
	./install.sh --from-source

build:
	cd bridge && go build ./...

test:
	cd bridge && go vet ./... && go build ./... && go test ./...
	python3 -m py_compile *.py scripts/*.py
	@echo "Running Python tests in isolated processes…"
	@for f in tests/test_*.py; do \
		mod=$$(basename $$f .py); \
		echo "== $$mod"; \
		python3 -m unittest tests.$$mod || exit 1; \
	done
	python3 scripts/preflight.py

release:
	@echo "Create and push a tag to build release assets:"
	@echo "  git tag v1.0.0 && git push origin v1.0.0"
