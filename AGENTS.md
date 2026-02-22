# Repository Guidelines

## Project Structure & Module Organization
- Core package: `linux_voice_assistant/` (runtime, entities, IPC, audio, wake-word, and daemon entrypoints).
- Companion daemons: `linux_voice_assistant/visd/` and `linux_voice_assistant/frontpaneld/`.
- Tests: `tests/` (Pytest; add new tests here, named `test_*.py`).
- Service/runtime assets: `systemd/`, `sounds/`, `wakewords/`, and runtime config files like `config.json` and `preferences.json`.
- Developer helpers: `script/` (`setup`, `run`, `test`, `lint`, `format`).

## Build, Test, and Development Commands
- `script/setup --dev`: create/update `.venv`, install package editable plus dev tools.
- `script/run`: start the core assistant using `config.json` (CLI flags are intentionally removed).
- `python3 -m linux_voice_assistant.visd`: run vision daemon locally.
- `python3 -m linux_voice_assistant.frontpaneld`: run front-panel daemon locally.
- `script/test`: run `pytest tests`.
- `script/lint`: run `black --check`, `isort --check`, `flake8`, `pylint`, and `mypy`.
- `script/format`: apply `black` and `isort` formatting.

## Coding Style & Naming Conventions
- Python, 4-space indentation, max line length `88` (Black/Flake8 aligned).
- Use `snake_case` for functions/variables/modules, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants.
- Keep imports isort-compatible; run `script/format` before opening a PR.
- Favor typed interfaces where practical; validate with `mypy`.

## Testing Guidelines
- Framework: Pytest (`script/test`).
- Place tests under `tests/` and use file names `test_<feature>.py`.
- Prefer focused unit tests for new logic (state handling, IPC messages, config resolution).
- No strict coverage gate is configured; changes should still include regression tests for bug fixes.

## Commit & Pull Request Guidelines
- Follow existing history style: concise, imperative subjects (examples: `Add ...`, `Fix ...`, `Update ...`, `Integrate ...`).
- Keep commits scoped to one change; avoid mixing refactors with behavior changes.
- PRs should include: purpose, key implementation notes, test/lint evidence (`script/test`, `script/lint`), and linked issue(s) when applicable.
- For behavior changes affecting services, include relevant logs or config snippets (for example `config.json` keys changed).

## Security & Configuration Tips
- Do not commit secrets, tokens, or machine-specific credentials in `config.json`.
- Validate service files in `systemd/` and note any required environment overrides in PR descriptions.
- On this device, this repository is tied to active services: `studio-lva.service` and `lva-gpio-control.service`; after config/daemon changes, verify both are healthy (`systemctl is-active studio-lva.service lva-gpio-control.service`).
