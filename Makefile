PYTHON := uv run

.PHONY: install fix precommit format format-check lint lint-fix imports imports-check typecheck dead-code unused-deps security audit test build quality ci run docker-build docker-run clean

install:
	uv sync --group dev
	uv run pre-commit install

precommit: fix

fix: format imports lint-fix

format:
	$(PYTHON) ruff format .

format-check:
	$(PYTHON) ruff format --check .

lint:
	$(PYTHON) ruff check .

lint-fix:
	$(PYTHON) ruff check --fix .

imports:
	$(PYTHON) ruff check --select I --fix .

imports-check:
	$(PYTHON) ruff check --select I .

typecheck:
	$(PYTHON) mypy

dead-code:
	$(PYTHON) vulture src/arrmate tests

unused-deps:
	$(PYTHON) deptry .

security:
	$(PYTHON) bandit -c pyproject.toml -r src/arrmate

audit:
	$(PYTHON) pip-audit

test:
	$(PYTHON) pytest

build:
	uv build

quality: format-check lint typecheck imports-check dead-code unused-deps security audit test build
	@echo "quality gate passed"

ci: quality

run:
	$(PYTHON) uvicorn arrmate.interfaces.api.app:app --host 0.0.0.0 --port 8000

docker-build:
	docker build -t arrmate:local .

docker-run:
	docker run --rm -p 8000:8000 -v arrmate-data:/data arrmate:local

clean:
	rm -rf .mypy_cache .pytest_cache .ruff_cache dist build *.egg-info .venv
