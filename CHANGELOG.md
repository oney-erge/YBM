# Changelog

Notable changes to YBM. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.3] - 2026-08-11

### Fixed

- The publish-only release job now targets `oney-erge/YBM` explicitly after
  downloading artifacts, so it does not require a local Git checkout to create
  the release page.
- Artifact upload/download actions use their current Node.js 24-based releases,
  removing the hosted-runner Node.js 20 deprecation warning.

## [0.1.2] - 2026-08-11

### Changed

- The one-command Windows and macOS/Linux installers now download the complete
  latest release instead of source archives, so the admin console works without
  Node.js. Re-running an installer refreshes application files while preserving
  local state.
- Release archives use stable `YBM-windows.zip` and `YBM-unix.tar.gz` names so
  installers and people can download the latest build directly.
- Installation is presented as two three-step Windows choices and one
  three-step macOS/Linux path; advanced and development routes moved out of
  the README's critical path.
- The MSI now has a visible minimal install UI and a final **Launch YBM now**
  action. Release CI installs the MSI, provisions its runtime, checks backend
  health, stops it, and uninstalls it instead of treating a successful build as
  proof that installation works.
- Windows releases include `Install-YBM.bat` for a no-MSI double-click path and
  `Install-YBM.ps1` for direct inspection or terminal use. Downloaded archives
  are checked against the release's SHA256 list before extraction.
- Tagged releases publish the tested headless image as
  `ghcr.io/oney-erge/ybm:<version>` and `ghcr.io/oney-erge/ybm:latest`.

### Removed

- Superseded planning archives, stale first-run screenshots, and unused logo
  explorations. Git history remains the record for those design iterations.

## [0.1.1] - 2026-08-11

### Added

- **Containerised headless profile.** `Dockerfile`, `docker-compose.yml` and
  `.dockerignore`. Telegram/WhatsApp intake, the operator loop, the code
  interpreter, MCP and the admin console all run in a container; desktop
  control, screenshots and the VS Code bridge cannot, and `ybm doctor` now
  reports them as unavailable rather than failing at call time.
- `ybm start --foreground`, which supervises until a service exits or a signal
  arrives. `start_all` spawns detached children and returns - correct for an
  interactive start, and an immediate exit to a container or systemd.
- **A signed-off install path on every platform.** `.github/workflows/release.yml`
  builds the admin console, stages a runtime payload
  (`scripts/package_release.py`), compiles a per-user MSI
  (`packaging/windows/ybm.wxs`, WiX v5), and publishes the installer plus a
  `.zip` and a `.tar.gz` with checksums and Sigstore build provenance on a tag.
  None of them needs administrator rights or Node.js, because the console ships
  prebuilt - previously a source install had no console at all until the user
  installed Node 22.22+ themselves.
- **`ybm.sh`**, the macOS/Linux counterpart to `YBM.bat`. One file that installs
  whatever is missing - `uv` included - then starts the stack and opens the
  console, idempotently. macOS and Linux previously had no equivalent: the
  documented path was `scripts/install.sh` followed by
  `./backend/.venv/bin/ybm start --open`, two commands where one only works
  after the other. The Python CLI cannot fill that gap because it lives inside
  the virtualenv it would have to create.
- winget manifest templates (`packaging/winget/`) plus
  `scripts/render_winget_manifests.ps1`, which hashes the published installer
  rather than trusting a number copied out of a build log.
- `--dry-run`, `--verify`, `--no-prompt` and `--install-dir` on both installers.
- Scheduled daily dependency audit (`.github/workflows/security-audit.yml`)
  covering the Python and all Node lockfiles; it opens an issue rather than a
  pull request.
- Optional `.pre-commit-config.yaml` running the same ruff and gitleaks checks
  CI runs.
- CI coverage for the frontend, WhatsApp sidecar, packaged container assets,
  and Node dependency audits.
- `error_text.describe_exception`, so an error a human reads is never empty.
- `harness.assert_rejected`, which refuses to let a replay miss pass as a
  policy refusal.

### Changed

- **One file to double-click, first run and every run.** `ybm setup` installs
  `uv` when it is missing (`Install-YbmUv` in `scripts/lib/common.ps1`) instead
  of refusing to continue, so `YBM.bat` handles a cold machine on its own. That
  refusal was the entire reason a separate first-run entry point had to exist.
  `scripts/install.ps1` no longer bootstraps uv either; it fetches the source
  and hands off, leaving one implementation rather than two that can drift.
- **`scripts/install.sh` hands off to `./ybm.sh`**, the way `install.ps1` hands
  off to `ybm.ps1 run`. Both installers now do the same small job - get the code
  onto the machine - and the launcher owns uv, the virtualenv, setup, and start.
  install.sh also stopped installing the developer extras (pytest, telethon,
  ruff) for people who only wanted to run YBM; that is the documented
  `uv sync --extra test --extra dev` line, as on Windows.
- **Installers require nothing preinstalled.** The Python 3.12+ gate is gone -
  `uv` is a standalone binary and provides the interpreter. git is optional,
  with an archive fallback. The uv installer URL is pinned to a version.
- `.mcp.json` launches the MCP server through `uv run` instead of a bare
  `python` with a relative `PYTHONPATH`, which only resolved on a machine that
  happened to have a system Python carrying the dependencies.
- The first-run wizard preselects a recommended Ollama model, and distinguishes
  "Ollama running with nothing pulled" from "no Ollama" - previously identical
  states, and the only point in onboarding that sent the user elsewhere.
- Scenario fixtures are rebuilt rather than merged when re-recording, dropping
  roughly 4,500 lines of unreachable keys.
- The headless image now packages the WhatsApp Node runtime, production
  dependencies, bundled starter skills, and project license.
- Container base images and the optional Ollama service are pinned to reviewed
  manifest digests.

### Removed

- `YBM-Setup.cmd`. `YBM.bat` now covers the first run too, so a second
  double-clickable file with a different name was one more thing to explain and
  one more way to pick the wrong one.

### Fixed

- **Credential redaction missed two shapes.** A quoted key (`"api_key": "…"`,
  i.e. any JSON config) was never matched, and an unquoted value stopped at the
  first space, so a passphrase redacted to `*** horse battery staple`. The
  scrubbed answer is now written to the task row as well as the audit sink and
  the outbound message.
- Two harness defects that made every negative scenario test pass vacuously: a
  pytest `tmp_path` counter that changed each run so recorded keys could never
  be hit again, and `sort_keys=True` reordering recorded payloads so replay fed
  tools a differently-ordered dict than recording did.
- Workspace recovery from a user's message only ever matched Windows drive
  letters, so it was dead code on Linux and macOS.
- A test set `os.name` on the real `os` module, flipping `pathlib` to the
  Windows flavour process-wide and breaking every later `Path()` on POSIX.
- The anti-fabrication guard was disabled task-wide by any earlier write; it now
  compares claimed filenames against recorded ones.
- `filesystem.manage`'s desktop alias enumerated a directory that was not an
  allowed root - the one operation bypassing `_safe_path`.
- Admin token comparison is constant-time; scope matching refuses a `..`
  segment; three type-narrowing `assert`s that `python -O` strips became real
  raises.
- `pypdf` 6.14.2 → 6.15.0 (CVE-2026-71852, CVE-2026-71870).
- Artifact downloads no longer put the long-lived admin token in a URL. They
  use short-lived artifact-scoped grants, and active HTML/SVG content is forced
  to download instead of executing on the admin origin.
- Browser responses now carry a restrictive content security policy and
  framing, referrer, permissions, opener, resource, and MIME-sniffing headers.
- Frontend `nanoid` was updated past GHSA-2v37-7h3g-55p8.
- Onboarding now supports backward navigation, describes the default capability
  policy accurately, and avoids duplicated setup/safety banners on small screens.

### Known issues

See `docs/GAPS.md`.
