# Contributing to YBM

Thanks for considering a contribution. YBM is a personal-scale project with a
strong bias toward small, well-verified changes over large speculative ones.

## Before you start

- For anything beyond a small fix, open an issue first to discuss the approach.
  This avoids wasted work on changes that don't fit the project's direction.
- Read [AGENTS.md](AGENTS.md) - it's the canonical guidance for how this
  codebase is organized and the standard this project holds changes to
  (including AI-assisted ones). It applies to human contributions too.
- Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for current components and
  message flow, and [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) before
  touching anything policy- or approval-related.

## Development setup

```powershell
.\scripts\ybm.ps1 setup
.\scripts\ybm.ps1 doctor
```

See [docs/INSTALL.md](docs/INSTALL.md) for full details.

## Making changes

- Keep pull requests focused: one logical change per PR.
- Reuse existing registry, policy, adapter, and supervisor patterns rather
  than introducing parallel mechanisms.
- Preserve capability policy, approval gates, and secure-by-default settings -
  see the "Architecture Boundaries" section of [AGENTS.md](AGENTS.md).
- Don't add speculative abstractions, config flags, or fallbacks for cases
  that can't happen. A small, direct change is preferred over a general one.

## Before opening a pull request

From `backend/`:

```powershell
uv sync --frozen --extra test --extra dev   # first time only (skip if `ybm setup` already ran)
uv run --frozen pytest
uv run --frozen ruff check .
uv run --frozen python ../scripts/check_markdown_links.py
```

From `vscode-extension/`, if you touched the extension:

```powershell
npm run compile
```

From `frontend/`, if you touched the admin console:

```powershell
npm ci
npx tsc -b --noEmit
npm run lint
npm run build
npx playwright install chromium
npm run test:e2e
```

From `whatsapp-bridge/`, if you touched the sidecar:

```powershell
npm ci
npm run check
```

Run `npm audit --audit-level=high` in every changed Node package. Container
changes must also pass `docker build -t ybm-control:local .`; CI verifies that
the resulting image contains the WhatsApp runtime and bundled starter skills.

If your change touches prompt text, tool schemas, or workspace layout, the
affected deterministic scenario fixtures under `backend/tests/scenario/fixtures/`
may need re-recording - see `docs/archive/HISTORY.md` for how that tier works and
`e2e/README.md` before touching anything that needs a live LLM or Telegram
account. Never record fixtures or run live E2E as a side effect of an
unrelated change.

## Reporting bugs

Open a GitHub issue with:
- What you expected vs. what happened
- Steps to reproduce
- Output of `ybm doctor` if relevant

## Reporting security issues

Do **not** open a public issue for a security vulnerability. See
[SECURITY.md](SECURITY.md) for private reporting instructions.

## Commit and PR conventions

- Use clear, descriptive commit messages focused on *why*, not just *what*.
- Do not add AI-assistant attribution, co-author trailers, or session links
  to commits - this applies to both human and AI-assisted contributions,
  consistent with this project's own conventions (see AGENTS.md).
