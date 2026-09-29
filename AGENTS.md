# Repository Guidelines

## Collaboration Scope

Claude Code is the primary implementation agent for this repository. Use Codex for small, well-bounded tasks, repository inspection, and implementation plans that Claude Code can execute. Avoid duplicating substantial implementation work that Claude Code is already handling; provide clear findings, proposed file-level changes, and verification commands instead.

## Project Structure & Module Organization

This repository packages the `fetchai-automation-agent` Home Assistant add-on. Its Python runtime lives in `fetchai-automation-agent/agent_home/`: put domain logic in `core/`, Home Assistant integrations in `tools/tool_homeassistant/`, HTTP endpoints in `rest/`, and scheduled work in `tasks/`. `proxy.py` serves the ingress API and compiled panel; `run.sh` starts both processes. The Vue 3 panel source is in `panel/src/`, with reusable views in `components/` and stateful UI logic in `composables/`; `panel/dist/` is the shipped build output. Keep development-only HA data generators under `scripts/`.

## Build, Test, and Development Commands

Run commands from `fetchai-automation-agent/` unless noted otherwise.

```bash
python3 -m venv ../venv
../venv/bin/pip install -r requirements-dev.txt
../venv/bin/python -m pytest
cd panel && npm ci && npm run dev
cd panel && npm run build
docker build -t fetchai-automation-agent .
```

The test dependencies deliberately exclude the heavy production stack; `tests/conftest.py` supplies stubs. `npm run build` refreshes the committed `panel/dist/` assets used by the image. Use the Docker build to validate the production packaging and Python 3.12 environment.

## Coding Style & Naming Conventions

Follow the surrounding code: Python uses four-space indentation, snake_case functions/modules, PascalCase classes, and type annotations where they clarify boundaries. Vue uses two-space indentation, PascalCase component files (for example, `ChatPanel.vue`), and camelCase composables such as `useChat.js`. Keep Home Assistant calls isolated in the existing client/tool layers; do not embed credentials or tokens in source, fixtures, or logs. There is no configured formatter or linter—make focused edits and match nearby formatting.

## Testing Guidelines

Add focused pytest cases in `tests/test_<feature>.py`, named `test_<behavior>`. The suite runs offline without Home Assistant, network access, or API keys. Update `tests/test_stubs_sanity.py` when changing import stubs, and run the real-dependency suite (`pip install -r requirements.txt -r requirements-dev.txt && pytest`) for runtime-import-sensitive changes. No coverage threshold is configured.

## Commit & Pull Request Guidelines

Use the established imperative prefixes: `Feat:`, `Fix:`, `Test:`, `Chore:`, or `CI:` (for example, `Fix: Preserve history retention`). Keep commits narrowly scoped. Pull requests should state the user-visible effect, tests run, configuration or migration impact, and include panel screenshots for UI changes. Call out any safety-sensitive change to confirmations, pairing, or Home Assistant service execution.
