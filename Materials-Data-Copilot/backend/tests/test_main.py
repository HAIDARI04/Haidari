import hashlib
import sqlite3

import database
import main
import pytest
from fastapi.testclient import TestClient

from main import app


client = TestClient(app)

DEFAULT_METADATA = {
    "technique": "Raman spectroscopy",
    "sample_id": "SAMPLE-001",
    "measurement_date": "2026-07-29",
    "instrument": "Lab Raman 532 nm",
    "operator": "Test Operator",
    "notes": "First pilot measurement",
}


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    temporary_database = tmp_path / "test.db"
    temporary_raw_directory = tmp_path / "raw"
    monkeypatch.setattr(database, "DATABASE_PATH", temporary_database)
    monkeypatch.setattr(main, "RAW_DATA_DIR", temporary_raw_directory)
    database.initialize_database()
    return temporary_database, temporary_raw_directory


def post_import(
    filename="sample.bin",
    contents=b"experimental bytes",
    metadata=None,
):
    return client.post(
        "/files/import",
        files={"file": (filename, contents, "application/octet-stream")},
        data=DEFAULT_METADATA if metadata is None else metadata,
    )


def assert_no_import_artifacts(raw_directory):
    with database.connect_database() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM imported_files"
        ).fetchone()[0]
    assert count == 0
    if raw_directory.exists():
        assert [path for path in raw_directory.rglob("*") if path.is_file()] == []


def test_read_root():
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "application": "Materials Data Copilot",
        "status": "running",
        "version": "0.2.0",
    }


def test_health_check():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_upload_interface_is_available():
    response = client.get("/upload")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Drop a file here" in response.text
    assert '<input id="file" name="file" type="file">' in response.text
    assert 'name="technique"' in response.text
    assert 'name="sample_id"' in response.text


def test_list_imported_files_returns_empty_list(isolated_storage):
    response = client.get("/files")

    assert response.status_code == 200
    assert response.json() == []


def test_list_and_retrieve_imported_file_metadata(isolated_storage):
    older_file = {
        "file_id": "11111111-1111-1111-1111-111111111111",
        "original_filename": "older.csv",
        "content_type": "text/csv",
        "size_bytes": 12,
        "sha256": "a" * 64,
        "storage_path": "raw/older.csv",
        "imported_at": "2026-07-28T01:02:03+00:00",
        "technique": "Raman",
        "sample_id": "SAMPLE-OLD",
        "measurement_date": "2026-07-27",
        "instrument": None,
        "operator": None,
        "notes": None,
    }
    newer_file = {
        "file_id": "22222222-2222-2222-2222-222222222222",
        "original_filename": "newer.bin",
        "content_type": "application/octet-stream",
        "size_bytes": 256,
        "sha256": "b" * 64,
        "storage_path": "raw/newer.bin",
        "imported_at": "2026-07-29T04:05:06+00:00",
        "technique": "XRD",
        "sample_id": "SAMPLE-NEW",
        "measurement_date": None,
        "instrument": "Diffractometer",
        "operator": "A. Researcher",
        "notes": "Baseline scan",
    }

    with database.connect_database() as connection:
        database.insert_imported_file(connection, newer_file)
        database.insert_imported_file(connection, older_file)

    list_response = client.get("/files")
    retrieve_response = client.get(f"/files/{older_file['file_id']}")

    assert list_response.status_code == 200
    assert list_response.json() == [newer_file, older_file]
    assert retrieve_response.status_code == 200
    assert retrieve_response.json() == older_file


def test_retrieve_imported_file_returns_clear_not_found(isolated_storage):
    response = client.get("/files/missing-id")

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "file_not_found",
        "message": "No imported file has the requested ID.",
    }


def test_import_preserves_bytes_sanitizes_name_and_persists_metadata(
    isolated_storage,
):
    _, temporary_raw_directory = isolated_storage
    original_bytes = (
        bytes(range(256)) * ((1024 * 1024 // 256) + 1)
        + b"\x00\xff\r\nRaman shift,intensity\n380.0,125\n"
    )
    expected_checksum = hashlib.sha256(original_bytes).hexdigest()

    response = post_import("../../unsafe?.csv", original_bytes)

    assert response.status_code == 201
    result = response.json()
    assert result["original_filename"] == "unsafe?.csv"
    assert result["content_type"] == "application/octet-stream"
    assert result["size_bytes"] == len(original_bytes)
    assert result["sha256"] == expected_checksum
    assert result["imported_at"]
    for field_name, value in DEFAULT_METADATA.items():
        assert result[field_name] == value

    stored_file = (
        temporary_raw_directory
        / result["file_id"]
        / "unsafe_.csv"
    )
    assert result["storage_path"] == str(stored_file)
    assert stored_file.read_bytes() == original_bytes

    with database.connect_database() as connection:
        record = connection.execute(
            "SELECT * FROM imported_files WHERE file_id = ?",
            (result["file_id"],),
        ).fetchone()
    assert dict(record) == result
    assert client.get(f"/files/{result['file_id']}").json() == result


def test_duplicate_content_is_rejected_without_overwrite(isolated_storage):
    _, raw_directory = isolated_storage
    original_bytes = b"same immutable experiment"

    first_response = post_import("first.dat", original_bytes)
    duplicate_response = post_import("second.dat", original_bytes)

    assert first_response.status_code == 201
    assert duplicate_response.status_code == 409
    assert duplicate_response.json()["detail"] == {
        "code": "duplicate_file",
        "message": "An identical file has already been imported.",
        "existing_file_id": first_response.json()["file_id"],
    }
    assert database.list_imported_files() == [first_response.json()]
    raw_files = [
        path
        for path in raw_directory.rglob("*")
        if path.is_file() and ".staging" not in path.parts
    ]
    assert len(raw_files) == 1
    assert raw_files[0].read_bytes() == original_bytes
    assert list((raw_directory / ".staging").glob("*")) == []


def test_oversized_file_is_rejected_and_removed(
    isolated_storage,
    monkeypatch,
):
    _, raw_directory = isolated_storage
    monkeypatch.setattr(main, "MAX_FILE_SIZE_BYTES", 10)

    response = post_import(contents=b"01234567890")

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "file_too_large"
    assert_no_import_artifacts(raw_directory)


def test_empty_file_is_rejected_and_removed(isolated_storage):
    _, raw_directory = isolated_storage

    response = post_import(contents=b"")

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "empty_file",
        "message": "Empty files cannot be imported.",
    }
    assert_no_import_artifacts(raw_directory)


@pytest.mark.parametrize(
    ("metadata", "expected_code"),
    [
        ({**DEFAULT_METADATA, "technique": "   "}, "missing_metadata"),
        (
            {**DEFAULT_METADATA, "measurement_date": "29 July 2026"},
            "invalid_measurement_date",
        ),
    ],
)
def test_invalid_metadata_is_rejected(
    isolated_storage,
    metadata,
    expected_code,
):
    _, raw_directory = isolated_storage

    response = post_import(metadata=metadata)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == expected_code
    assert_no_import_artifacts(raw_directory)


def test_database_failure_rolls_back_new_file(
    isolated_storage,
    monkeypatch,
):
    _, raw_directory = isolated_storage

    def fail_insert(_connection, _metadata):
        raise sqlite3.OperationalError("simulated insert failure")

    monkeypatch.setattr(database, "insert_imported_file", fail_insert)

    response = post_import()

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "import_failed"
    assert_no_import_artifacts(raw_directory)


def test_file_publish_failure_rolls_back_database(
    isolated_storage,
    monkeypatch,
):
    _, raw_directory = isolated_storage

    def fail_replace(_source, _destination):
        raise OSError("simulated file publish failure")

    monkeypatch.setattr(main.os, "replace", fail_replace)

    response = post_import()

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "import_failed"
    assert_no_import_artifacts(raw_directory)


def test_initialize_database_migrates_existing_metadata_table(
    tmp_path,
    monkeypatch,
):
    legacy_database = tmp_path / "legacy.db"
    monkeypatch.setattr(database, "DATABASE_PATH", legacy_database)
    with sqlite3.connect(legacy_database) as connection:
        connection.execute(
            """
            CREATE TABLE imported_files (
                file_id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                imported_at TEXT NOT NULL
            )
            """
        )

    database.initialize_database()

    with database.connect_database() as connection:
        column_names = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(imported_files)")
        }
        indexes = {
            row["name"]
            for row in connection.execute("PRAGMA index_list(imported_files)")
        }
    assert {
        "technique",
        "sample_id",
        "measurement_date",
        "instrument",
        "operator",
        "notes",
    }.issubset(column_names)
    assert "idx_imported_files_sha256" in indexes


def test_initialize_database_preserves_legacy_duplicate_records(
    tmp_path,
    monkeypatch,
):
    legacy_database = tmp_path / "legacy-duplicates.db"
    monkeypatch.setattr(database, "DATABASE_PATH", legacy_database)
    with sqlite3.connect(legacy_database) as connection:
        connection.execute(
            """
            CREATE TABLE imported_files (
                file_id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                imported_at TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO imported_files VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("one", "one.dat", None, 1, "a" * 64, "raw/one", "2026-01-01"),
                ("two", "two.dat", None, 1, "a" * 64, "raw/two", "2026-01-02"),
            ],
        )

    database.initialize_database()

    with database.connect_database() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM imported_files"
        ).fetchone()[0]
        trigger = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'trigger'
              AND name = 'reject_duplicate_imported_file_sha256'
            """
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="duplicate sha256"):
            connection.execute(
                """
                INSERT INTO imported_files (
                    file_id,
                    original_filename,
                    size_bytes,
                    sha256,
                    storage_path,
                    imported_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("three", "three.dat", 1, "a" * 64, "raw/three", "2026-01-03"),
            )

    assert count == 2
    assert trigger is not None
