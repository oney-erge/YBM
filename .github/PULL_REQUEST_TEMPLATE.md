## What changed and why

## Verification

- [ ] `uv sync --frozen --extra test --extra dev` then `uv run --frozen pytest` (from `backend/`)
- [ ] `uv run --frozen ruff check .` (from `backend/`)
- [ ] `npm run compile` (from `vscode-extension/`, if touched)
- [ ] Scenario fixtures re-recorded if this touches prompts, tool schemas, or workspace layout (see `docs/archive/HISTORY.md`)

## Does this touch a security-sensitive area?

(capability policy, approval gates, secret handling, redaction, default
access modes) If yes, explain what changed and why it's still secure by
default.

## Anything reviewers should pay special attention to
