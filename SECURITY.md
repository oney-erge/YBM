# Security Policy

## Supported versions

YBM is pre-1.0. Security fixes are applied to the latest commit on `main`;
older revisions are not maintained.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability or include
secrets, exploit details, private logs, screenshots, or configuration in a
public discussion.

Use GitHub's private vulnerability reporting for this repository when it is
available. If that option is unavailable, contact the repository owner through
the contact method on the [oney-erge GitHub profile](https://github.com/oney-erge)
and request a private reporting channel.

Include:

- the affected revision and platform;
- the capability and access mode involved;
- minimal reproduction steps;
- expected impact; and
- whether credentials or third-party systems may have been exposed.

You should receive an acknowledgment within seven days. Please allow time for a
fix and coordinated disclosure before publishing details.

## Deployment assumptions

YBM is a local, single-operator application. It is not designed as a
multi-tenant service or as an Internet-facing control plane. Keep the backend,
admin UI, VS Code bridge, model endpoints, and generated preview servers bound
to loopback unless you have added an authenticated network boundary.

Start from `config/config.example.yaml`. It disables high-impact capabilities
by default. Treat `.env`, `config/config.yaml`, `.agent_control/agent_control.db`, logs,
screenshots, workspaces, and all of `.agent_control/` as private runtime data.

The detailed security boundaries, guarantees, and known limitations are in
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).
