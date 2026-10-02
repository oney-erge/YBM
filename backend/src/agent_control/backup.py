"""Back up the state that can't be regenerated (docs/archive/UI_UX_AUDIT.md Phase 6):
the task/audit/memory database, config.yaml, .env, and the encrypted secret
vault. Deliberately excludes artifacts/workspaces/logs/caches - those are
task output or regenerable, not "your data" in the sense that losing it
would be a real loss, and including them would make backups large and
slow for no real benefit.

Never prints file contents - .env and the vault carry secrets, and the
project's own logging rule ("never log secret values or place them in
task output") applies here too. Only paths and byte counts are reported.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import zipfile

from agent_control.config import load_settings
from agent_control.storage.database import Database


def run_backup(out_dir: str | None = None) -> int:
    settings = load_settings()
    database = Database(settings.storage.database_url)

    candidates: list[tuple[str, Path]] = [
        ("database", Path(database.path)),
        ("config", Path("config/config.yaml")),
        ("env", Path(".env")),
        ("secret_vault", Path(settings.secrets.path)),
    ]
    included = [(label, path) for label, path in candidates if path.is_file()]
    if not included:
        print("Nothing to back up - no database, config.yaml, .env, or secret vault file found.")
        return 1

    destination_dir = Path(out_dir) if out_dir else Path(".agent_control") / "backups"
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = destination_dir / f"ybm-backup-{timestamp}.zip"

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for label, path in included:
            archive.write(path, arcname=path.name)

    print(f"Backup written to {archive_path} ({archive_path.stat().st_size} bytes)")
    for label, path in included:
        print(f"  - {label}: {path} ({path.stat().st_size} bytes)")
    missing = [label for label, path in candidates if not path.is_file()]
    if missing:
        print(f"Skipped (not found): {', '.join(missing)}")
    print(
        "This archive contains secrets (.env / the secret vault, if included) - "
        "store it somewhere at least as protected as this machine."
    )
    return 0
