import argparse
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from database import DATABASE_PATH

RAW_DATA_DIR = Path(__file__).parent / "data" / "raw"


def sha256_file(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def create_backup(
    destination_root: Path,
    database_path: Path = DATABASE_PATH,
    raw_data_dir: Path = RAW_DATA_DIR,
) -> Path:
    database_path = database_path.resolve()
    raw_data_dir = raw_data_dir.resolve()
    destination_root = destination_root.resolve()

    if not database_path.is_file():
        raise FileNotFoundError(f"Database not found: {database_path}")

    data_directory = database_path.parent.resolve()
    if destination_root == data_directory or data_directory in destination_root.parents:
        raise ValueError("Choose a backup destination outside backend/data.")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_name = f"materials-data-copilot-{timestamp}"
    backup_directory = destination_root / backup_name
    partial_backup_directory = destination_root / f".{backup_name}.partial"
    partial_backup_directory.mkdir(parents=True, exist_ok=False)

    backup_database = partial_backup_directory / database_path.name
    source_uri = f"{database_path.as_uri()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
        with closing(sqlite3.connect(backup_database)) as backup_connection:
            source_connection.backup(backup_connection)

    backup_raw_directory = partial_backup_directory / "raw"
    if raw_data_dir.is_dir():
        shutil.copytree(
            raw_data_dir,
            backup_raw_directory,
            ignore=shutil.ignore_patterns(".staging"),
        )
    else:
        backup_raw_directory.mkdir()

    raw_files = []
    for path in sorted(backup_raw_directory.rglob("*")):
        if path.is_file():
            raw_files.append(
                {
                    "path": path.relative_to(partial_backup_directory).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": backup_database.name,
        "database_sha256": sha256_file(backup_database),
        "raw_file_count": len(raw_files),
        "raw_files": raw_files,
    }
    (partial_backup_directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    partial_backup_directory.rename(backup_directory)
    return backup_directory


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a local SQLite and raw-file backup.",
    )
    parser.add_argument(
        "destination",
        type=Path,
        help="Directory in which a timestamped backup will be created.",
    )
    arguments = parser.parse_args()
    backup_directory = create_backup(arguments.destination)
    print(f"Backup created at: {backup_directory}")


if __name__ == "__main__":
    main()
