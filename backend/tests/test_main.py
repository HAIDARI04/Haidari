from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


def test_read_root():
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "application": "Materials Data Copilot",
        "status": "running",
        "version": "0.1.0",
    }


def test_health_check():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}

import hashlib


def test_import_file_preserves_bytes_and_returns_checksum(tmp_path, monkeypatch):
    original_bytes = b"Raman shift,intensity\n380.0,125\n381.0,140\n"
    expected_checksum = hashlib.sha256(original_bytes).hexdigest()

    monkeypatch.setattr("main.RAW_DATA_DIR", tmp_path)

    response = client.post(
        "/files/import",
        files={
            "file": (
                "sample_raman.csv",
                original_bytes,
                "text/csv",
            )
        },
    )

    assert response.status_code == 201

    result = response.json()
    assert result["original_filename"] == "sample_raman.csv"
    assert result["content_type"] == "text/csv"
    assert result["size_bytes"] == len(original_bytes)
    assert result["sha256"] == expected_checksum

    stored_file = (
        tmp_path
        / result["file_id"]
        / result["original_filename"]
    )

    assert stored_file.exists()
    assert stored_file.read_bytes() == original_bytes