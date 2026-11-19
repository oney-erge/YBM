## What changed and why

## Change type

Only for a change that touches `backend/src/agent_control/`. Tick exactly one; the `design-rule` check reads it ([docs/DESIGN.md](../docs/DESIGN.md), sections 3 and 4).

- [ ] Minimal: it fits a home in `docs/DESIGN.md` and adds no module, table, config key, tool or tool operation, prompt file or pattern that matches wording; at most 150 lines
- [ ] Design: it has an entry in `docs/DESIGN.md`, deletes what it replaces, and compares the same live cases before and after

## Verification

- [ ] `uv sync --frozen --extra test --extra dev` then `uv run --frozen pytest -n auto --dist loadfile` (from `backend/`)
- [ ] `uv run --frozen ruff check .` (from `backend/`)
- [ ] `npm run compile` (from `vscode-extension/`, if touched)
- [ ] Scenario fixtures re-recorded if this touches prompts, tool schemas, or workspace layout (see `docs/archive/HISTORY.md`)

## Does this touch a security-sensitive area?

(capability policy, approval gates, secret handling, redaction, default
access modes) If yes, explain what changed and why it's still secure by
default.

## Anything reviewers should pay special attention to
