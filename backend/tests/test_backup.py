import hashlib
import json
import sqlite3

import database
from backup import create_backup


def test_create_backup_copies_database_raw_bytes_and_manifest(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "source" / "materials.db"
    raw_directory = tmp_path / "source" / "raw"
    destination_root = tmp_path / "backups"
    raw_file = raw_directory / "file-id" / "experiment.bin"
    original_bytes = b"\x00\xff\r\nimmutable experiment"

    monkeypatch.setattr(database, "DATABASE_PATH", database_path)
    database.initialize_database()
    raw_file.parent.mkdir(parents=True)
    raw_file.write_bytes(original_bytes)
    staging_file = raw_directory / ".staging" / "incomplete.upload"
    staging_file.parent.mkdir()
    staging_file.write_bytes(b"do not back up")

    metadata = {
        "file_id": "file-id",
        "original_filename": "experiment.bin",
        "content_type": "application/octet-stream",
        "size_bytes": len(original_bytes),
        "sha256": hashlib.sha256(original_bytes).hexdigest(),
        "storage_path": str(raw_file),
        "imported_at": "2026-07-29T00:00:00+00:00",
        "technique": "Raman",
        "sample_id": "SAMPLE-001",
        "measurement_date": None,
        "instrument": None,
        "operator": None,
        "notes": None,
    }
    with database.connect_database() as connection:
        database.insert_imported_file(connection, metadata)

    backup_directory = create_backup(
        destination_root,
        database_path=database_path,
        raw_data_dir=raw_directory,
    )

    copied_raw_file = backup_directory / "raw" / "file-id" / "experiment.bin"
    assert copied_raw_file.read_bytes() == original_bytes
    assert not (backup_directory / "raw" / ".staging").exists()

    with sqlite3.connect(backup_directory / database_path.name) as connection:
        row_count = connection.execute(
            "SELECT COUNT(*) FROM imported_files"
        ).fetchone()[0]
    assert row_count == 1

    manifest = json.loads(
        (backup_directory / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["database"] == database_path.name
    assert len(manifest["database_sha256"]) == 64
    assert manifest["raw_file_count"] == 1
    assert manifest["raw_files"][0]["path"] == "raw/file-id/experiment.bin"
    assert manifest["raw_files"][0]["size_bytes"] == len(original_bytes)
