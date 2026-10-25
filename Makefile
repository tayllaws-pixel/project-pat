PY ?= python3
.PHONY: test lint progress schemas
test:
	$(PY) -m unittest discover -s tests -t . -v
lint:
	@command -v ruff >/dev/null && ruff check . || echo "ruff not installed; skipping"
	@for f in infra/proxmox/*.sh; do bash -n $$f && echo "syntax ok: $$f"; done
	@command -v shellcheck >/dev/null && shellcheck -S warning infra/proxmox/*.sh || echo "shellcheck not installed; skipping"
progress:
	$(PY) tools/progress.py
schemas:
	$(PY) -c "from patshared.schemas import export_json_schemas; export_json_schemas('docs/schemas')"
