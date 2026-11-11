# Projects and automations integration test

Run `npm run test:integration` from `frontend` after the repository setup has
installed the backend test environment, frontend packages, and Playwright Chromium.
The command builds the console and starts a temporary FastAPI server on loopback
port 4181. It fails if that port is already occupied. It never attaches to a running
YBM instance or reads its database, config, accounts, or work folders.

This test uses the compiled React app, HTTP API, SQLite, real task worker, policy,
approvals, document adapter, artifact download, and workflow/schedule APIs. It
covers project create/edit, project-specific clarification, approval, output
download, reload, workflow save, invalid cadence feedback, schedule creation,
pause/resume/delete, folder-trigger creation, a real file arriving and settling,
fresh approval and artifact creation for that file, project deletion, and mobile layout. Only the model's
decisions are scripted; no HTTP responses are mocked. The temporary server uses
a fixed test-only admin token and disposable files.

Time-based due execution, prevention of overlap/starvation, failure recovery,
fresh approvals for repeated work, project isolation, and artifact tampering
are covered in `backend/tests/test_projects_automation.py`. The browser test
also runs the real scheduler with a short poll interval for the folder flow.
`backend/tests/test_folder_triggers.py` covers restart recovery, partial writes,
duplicate pollers, same-content changes, revoked read access, output feedback,
bounded scanning, and transaction rollback. Symlink creation is platform-gated.
The browser test does not wait for time-based intervals or test live model choices, account
integrations, desktop control, or Office rendering.

`npm run test:e2e` remains the separate mocked-API UI suite. Run the two suites
sequentially because Playwright clears their shared `test-results` directory.
