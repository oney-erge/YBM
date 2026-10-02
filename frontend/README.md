# YBM Admin Console

React + Vite + TypeScript SPA for YBM's admin console, served by the backend at `/admin`. See
[docs/archive/UI_REWRITE_PLAN.md](../docs/archive/UI_REWRITE_PLAN.md) for the full design and phase-by-phase
build record.

## Development

```powershell
npm install
npm run dev      # Vite dev server with hot reload, proxying /admin/api/* to the backend
```

Or from the repo root on Windows: `.\scripts\ybm.ps1 ui-dev`.

Requires the backend running separately. On Windows use `.\scripts\ybm.ps1 start`, or use
`./backend/.venv/bin/ybm start --no-telegram --no-whatsapp --no-worker --no-scheduler
--no-localdeploy` for a minimal cross-platform process set. The dev server proxies API calls to
`http://127.0.0.1:8765`; it does not serve them itself.

## Build

```powershell
npm run build     # or `.\scripts\ybm.ps1 ui-build` from the repo root on Windows
```

Output lands in `backend/src/agent_control/static/admin/` (see `vite.config.ts`'s
`build.outDir`) - a generated, gitignored artifact that `admin.py` serves directly at `/admin`.
Not committed; built fresh by whoever needs it running.

## Why no CORS

The backend deliberately has no `CORSMiddleware` (see `admin.py`'s `_origin_is_trusted()`) - a
same-origin check exists specifically to stop a malicious local page from driving the agent.
`vite.config.ts`'s dev proxy rewrites both `Host` and `Origin` on the way to the backend so the
request is *actually* same-origin, not a bypass of that check. See
[docs/archive/UI_REWRITE_PLAN.md §4](../docs/archive/UI_REWRITE_PLAN.md) before touching the proxy config.
