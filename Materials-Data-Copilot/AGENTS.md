# Repository Guidelines

## Project Structure & Module Organization

The active application is the FastAPI backend in `backend/`. API routes and file-import logic live in `backend/main.py`; SQLite connection and schema setup live in `backend/database.py`. Backend tests are under `backend/tests/` and follow the source module names. `frontend/`, `docs/`, and the root `tests/` directory are currently placeholders for later phases. Runtime databases and imported files belong under `backend/data/` and are ignored by Git.

## Build, Test, and Development Commands

Run commands from the repository root on Windows:

- `backend\.venv\Scripts\python.exe -m pytest -vv` runs the complete test suite using `pytest.ini`.
- `backend\.venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --reload` starts the API with automatic reload.
- `backend\.venv\Scripts\python.exe backend\database.py` initializes the SQLite schema and prints the database location.

Dependencies are pinned in `backend/requirements.txt`. Ask before installing or materially upgrading dependencies.

## Coding Style & Naming Conventions

Use four-space indentation and standard PEP 8 conventions. Name modules, functions, fixtures, and variables with `snake_case`; use `UPPER_CASE` for constants such as `RAW_DATA_DIR`. Add type hints when they improve API or database boundaries. Keep route handlers focused and move reusable persistence logic into `database.py` or a dedicated module. No formatter or linter is currently configured, so keep imports grouped and changes consistent with nearby code.

## Testing Guidelines

Use pytest and FastAPI's `TestClient`. Name files `test_*.py` and tests `test_<behavior>`. Every import-related change must verify exact saved bytes, SHA-256, byte count, filename, content type, storage path, and timestamp through a fresh SQLite connection. Use `tmp_path` and `monkeypatch` so tests never alter real experimental files or the production database. There is no formal coverage threshold; cover success paths and relevant failure cases.

## Commit & Pull Request Guidelines

History uses short, imperative subjects such as `Add backend endpoint tests`. Keep each commit focused and avoid committing databases, virtual environments, caches, or raw data. Pull requests should explain behavior changes, list tests run, link related issues, and call out schema or provenance effects. Include screenshots only for user-interface changes.

## Data Integrity & Security

Treat imported experiments as immutable. Never normalize, decode, rewrite, or delete raw files during processing. Store derived outputs separately and retain traceable metadata and checksums. Do not commit credentials or local `.env` files.
