import hashlib
import json
import sqlite3

import database
import pytest
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
    assert manifest["database_integrity"] == "ok"
    assert manifest["catalog_record_count"] == 1
    assert manifest["catalog_files_verified"][0]["file_id"] == "file-id"
    assert manifest["catalog_files_verified"][0]["sha256"] == hashlib.sha256(original_bytes).hexdigest()
    assert manifest["raw_file_count"] == 1
    assert manifest["orphan_raw_file_count"] == 0
    assert manifest["raw_files"][0]["path"] == "raw/file-id/experiment.bin"
    assert manifest["raw_files"][0]["size_bytes"] == len(original_bytes)


def test_backup_refuses_cataloged_raw_file_with_changed_checksum(tmp_path, monkeypatch):
    database_path = tmp_path / "source" / "materials.db"
    raw_directory = tmp_path / "source" / "raw"
    destination_root = tmp_path / "backups"
    raw_file = raw_directory / "file-id" / "experiment.bin"
    original_bytes = b"original immutable bytes"

    monkeypatch.setattr(database, "DATABASE_PATH", database_path)
    database.initialize_database()
    raw_file.parent.mkdir(parents=True)
    raw_file.write_bytes(original_bytes)
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
    raw_file.write_bytes(b"X" * len(original_bytes))

    try:
        create_backup(destination_root, database_path=database_path, raw_data_dir=raw_directory)
    except ValueError as error:
        assert "checksum mismatch" in str(error)
    else:
        raise AssertionError("Backup unexpectedly accepted a modified raw file")
    assert not destination_root.exists() or not list(destination_root.iterdir())


def test_backup_refuses_analysis_run_with_mismatched_source_checksum(tmp_path, monkeypatch):
    database_path = tmp_path / "source" / "materials.db"
    raw_directory = tmp_path / "source" / "raw"
    destination_root = tmp_path / "backups"
    raw_file = raw_directory / "file-id" / "spectrum.txt"
    original_bytes = b"XYDATA=\n100,1\n101,2\n"
    expected_sha = hashlib.sha256(original_bytes).hexdigest()

    monkeypatch.setattr(database, "DATABASE_PATH", database_path)
    database.initialize_database()
    raw_file.parent.mkdir(parents=True)
    raw_file.write_bytes(original_bytes)
    with database.connect_database() as connection:
        database.insert_imported_file(connection, {
            "file_id": "file-id",
            "original_filename": "spectrum.txt",
            "content_type": "text/plain",
            "size_bytes": len(original_bytes),
            "sha256": expected_sha,
            "storage_path": str(raw_file),
            "imported_at": "2026-08-25T00:00:00+00:00",
            "technique": "Raman",
            "sample_id": "SAMPLE-001",
            "measurement_date": None,
            "instrument": None,
            "operator": None,
            "notes": None,
        })
        connection.execute("DROP TRIGGER reject_analysis_run_raw_sha_mismatch")
        trace = json.dumps(
            {"x": [1.0, 2.0, 3.0], "y": [1.0, 2.0, 3.0]}, separators=(",", ":")
        )
        derived_sha = hashlib.sha256(trace.encode()).hexdigest()
        result = {
            "run_id": "legacy-mismatch",
            "file_id": "file-id",
            "sha256": "0" * 64,
            "derived_sha256": derived_sha,
            "processing_config": {},
            "analysis_input": "processed trace",
            "raw_data_modified": False,
            "app_version": "legacy",
            "analyzed_at": "2026-08-25T00:00:00+00:00",
        }
        database.create_analysis_run(connection, {
            "run_id": "legacy-mismatch",
            "file_id": "file-id",
            "raw_sha256": "0" * 64,
            "derived_sha256": derived_sha,
            "derived_trace": trace,
            "processing_config": "{}",
            "result": json.dumps(result),
            "app_version": "legacy",
            "created_at": "2026-08-25T00:00:00+00:00",
        })

    with pytest.raises(ValueError, match="Analysis provenance verification failed"):
        create_backup(destination_root, database_path=database_path, raw_data_dir=raw_directory)

    assert not destination_root.exists() or not list(destination_root.iterdir())


def test_backup_refuses_corrupted_derived_analysis_trace(tmp_path, monkeypatch):
    database_path = tmp_path / "source" / "materials.db"
    raw_directory = tmp_path / "source" / "raw"
    destination_root = tmp_path / "backups"
    raw_file = raw_directory / "file-id" / "spectrum.txt"
    raw_bytes = b"XYDATA=\n1,1\n2,2\n3,3\n"
    raw_file.parent.mkdir(parents=True)
    raw_file.write_bytes(raw_bytes)
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    monkeypatch.setattr(database, "DATABASE_PATH", database_path)
    database.initialize_database()
    trace = json.dumps(
        {"x": [1.0, 2.0, 3.0], "y": [1.0, 2.0, 3.0]}, separators=(",", ":")
    )
    derived_sha = hashlib.sha256(trace.encode()).hexdigest()
    created_at = "2026-08-25T00:00:00+00:00"
    result = {
        "run_id": "corrupt-derived", "file_id": "file-id", "sha256": raw_sha,
        "derived_sha256": derived_sha, "processing_config": {},
        "analysis_input": "processed trace", "raw_data_modified": False,
        "app_version": "test", "analyzed_at": created_at,
    }
    with database.connect_database() as connection:
        database.insert_imported_file(connection, {
            "file_id": "file-id", "original_filename": "spectrum.txt",
            "content_type": "text/plain", "size_bytes": len(raw_bytes),
            "sha256": raw_sha, "storage_path": str(raw_file), "imported_at": created_at,
            "technique": "Raman", "sample_id": "SAMPLE-001", "measurement_date": None,
            "instrument": None, "operator": None, "notes": None,
        })
        database.create_analysis_run(connection, {
            "run_id": "corrupt-derived", "file_id": "file-id", "raw_sha256": raw_sha,
            "derived_sha256": derived_sha, "derived_trace": trace,
            "processing_config": "{}", "result": json.dumps(result),
            "app_version": "test", "created_at": created_at,
        })
        connection.execute("DROP TRIGGER reject_analysis_run_update")
        connection.execute(
            "UPDATE analysis_runs SET derived_trace = ? WHERE run_id = ?",
            ('{"x":[1.0,2.0,3.0],"y":[1.0,2.0,4.0]}', "corrupt-derived"),
        )

    with pytest.raises(ValueError, match="Analysis run integrity verification failed"):
        create_backup(destination_root, database_path=database_path, raw_data_dir=raw_directory)

    assert not destination_root.exists() or not list(destination_root.iterdir())


def test_backup_refuses_legacy_invalid_structured_json(tmp_path, monkeypatch):
    database_path = tmp_path / "source" / "materials.db"
    raw_directory = tmp_path / "source" / "raw"
    destination_root = tmp_path / "backups"

    monkeypatch.setattr(database, "DATABASE_PATH", database_path)
    database.initialize_database()
    with database.connect_database() as connection:
        connection.execute("DROP TRIGGER validate_presets_config_insert")
        connection.execute(
            "INSERT INTO presets (preset_id, name, config) VALUES (?, ?, ?)",
            ("legacy-invalid", "Legacy invalid", "not-json"),
        )

    with pytest.raises(ValueError, match="Structured-data verification failed"):
        create_backup(destination_root, database_path=database_path, raw_data_dir=raw_directory)

    assert not destination_root.exists() or not list(destination_root.iterdir())
