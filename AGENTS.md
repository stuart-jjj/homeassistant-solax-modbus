# AGENTS.md

## Purpose
This file helps AI coding agents work safely and efficiently in this repository.

## Quick Start
- Python: 3.12+
- Install/sync deps: `uv sync --all-groups`
- Lint/format checks: `uv run pre-commit run --all-files`
- Type checks: `uv run mypy custom_components/solax_modbus tests --strict`
- Tests (fast): `uv run pytest -m "not slow"`
- Tests (full): `uv run pytest`
- Convenience script: `./scripts/test.zsh [--full|--cov]`

## Code Map
- Integration hub and modbus I/O: [custom_components/solax_modbus/__init__.py](custom_components/solax_modbus/__init__.py)
- Entity platform loaders:
  - [custom_components/solax_modbus/number.py](custom_components/solax_modbus/number.py)
  - [custom_components/solax_modbus/select.py](custom_components/solax_modbus/select.py)
  - [custom_components/solax_modbus/sensor.py](custom_components/solax_modbus/sensor.py)
- Plugin architecture and entity description types: [custom_components/solax_modbus/const.py](custom_components/solax_modbus/const.py)
- SolaX plugin entities and callbacks: [custom_components/solax_modbus/plugin_solax.py](custom_components/solax_modbus/plugin_solax.py)

## Export Control Logic (Focus: export_duration)
- `export_duration` is a select entity in [custom_components/solax_modbus/plugin_solax.py](custom_components/solax_modbus/plugin_solax.py#L2907):
  - key: `export_duration`
  - write register: `0x9F`
  - options map: seconds -> labels (`4`, `900`, `1800`, `2700`, `3600`, `5400`, `7200`)
- Feedback is read via a sensor with the same key in [custom_components/solax_modbus/plugin_solax.py](custom_components/solax_modbus/plugin_solax.py#L4627):
  - read register: `0x10B`
  - `scale` dict mirrors the select option dict.
- Write path:
  - `async_select_option` reverse-maps label -> integer and writes via hub in [custom_components/solax_modbus/select.py](custom_components/solax_modbus/select.py#L147)
  - low-level conversion/write occurs in [custom_components/solax_modbus/__init__.py](custom_components/solax_modbus/__init__.py#L1202)

## Critical Guardrails for export_duration Changes
- Keep the select `option_dict` and sensor `scale` mapping in sync.
- Do not confuse export-duration with remote-control duration:
  - `export_duration` is separate from `remotecontrol_duration` and `remotecontrol_autorepeat_duration`.
  - See [docs/solax-mode1-modbus-power-control.md](docs/solax-mode1-modbus-power-control.md).
- Keep register roles distinct:
  - command write register: `0x9F`
  - feedback read register: `0x10B`
- Be cautious with automation advice for export-control settings: writable export parameters are generally EEPROM-backed on SolaX and should not be churned frequently.
  - See warning in [docs/solax-G4-operation-modes.md](docs/solax-G4-operation-modes.md).
- Parallel-mode limit adjustments in `localDataCallback` target export limits and remote control limits, not `export_duration`.

## Editing Conventions
- Prefer plugin-level entity descriptions for behavior changes; avoid ad-hoc logic in platform modules unless needed.
- For select entities, keep `option_dict` values unique so reverse mapping remains deterministic.
- Keep changes narrowly scoped by inverter family and `allowedtypes` masks.

## Validation Checklist for export_duration PRs
- Confirm UI option list, register write (`0x9F`), and sensor feedback (`0x10B`) all agree.
- Run: `uv run mypy custom_components/solax_modbus tests --strict`
- Run: `uv run pytest -m "not slow"`
- If touching shared entity plumbing (`select.py`/hub write path), run full tests: `uv run pytest`

## Key Docs (Link, do not duplicate)
- Developer internals: [docs/developer_guide.md](docs/developer_guide.md)
- CI/CD checks and local equivalents: [docs/developer/cicd-pipeline.md](docs/developer/cicd-pipeline.md)
- SolaX operation modes + EEPROM caution: [docs/solax-G4-operation-modes.md](docs/solax-G4-operation-modes.md)
- Mode 1 remote control duration semantics: [docs/solax-mode1-modbus-power-control.md](docs/solax-mode1-modbus-power-control.md)
