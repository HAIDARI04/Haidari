import argparse
import hashlib
import json
import math
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


def verify_source_catalog(database_path: Path, raw_data_dir: Path) -> list[dict]:
    """Verify that every cataloged raw file still matches its immutable record."""
    source_uri = f"{database_path.as_uri()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"Source database integrity check failed: {integrity}")
        rows = connection.execute(
            """SELECT file_id, original_filename, size_bytes, sha256, storage_path
               FROM imported_files ORDER BY file_id"""
        ).fetchall()
        invalid_analysis_provenance = connection.execute(
            """
            SELECT COUNT(*)
            FROM analysis_runs
            LEFT JOIN imported_files USING (file_id)
            WHERE imported_files.file_id IS NULL
               OR analysis_runs.raw_sha256 <> imported_files.sha256
            """
        ).fetchone()[0]
        if invalid_analysis_provenance:
            raise ValueError(
                f"Analysis provenance verification failed for {invalid_analysis_provenance} run(s)"
            )
        invalid_json_records = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM imported_files
                 WHERE extended_metadata IS NOT NULL AND
                     CASE WHEN json_valid(extended_metadata)
                          THEN json_type(extended_metadata) ELSE NULL END IS NOT 'object')
              + (SELECT COUNT(*) FROM presets
                 WHERE CASE WHEN json_valid(config)
                            THEN json_type(config) ELSE NULL END IS NOT 'object')
              + (SELECT COUNT(*) FROM analysis_recipes
                 WHERE CASE WHEN json_valid(config)
                            THEN json_type(config) ELSE NULL END IS NOT 'object')
              + (SELECT COUNT(*) FROM analysis_runs
                 WHERE CASE WHEN json_valid(processing_config)
                            THEN json_type(processing_config) ELSE NULL END IS NOT 'object'
                    OR CASE WHEN json_valid(result)
                            THEN json_type(result) ELSE NULL END IS NOT 'object')
            """
        ).fetchone()[0]
        if invalid_json_records:
            raise ValueError(
                f"Structured-data verification failed for {invalid_json_records} record(s)"
            )
        analysis_rows = connection.execute(
            """SELECT run_id, file_id, raw_sha256, derived_sha256,
                      derived_trace, processing_config, result,
                      app_version, created_at
               FROM analysis_runs"""
        ).fetchall()
        for (
            run_id, file_id, raw_sha256, derived_sha256, derived_trace,
            processing_config, result, app_version, created_at,
        ) in analysis_rows:
            config = json.loads(processing_config)
            decoded_result = json.loads(result)
            expected_fields = {
                "run_id": run_id,
                "file_id": file_id,
                "sha256": raw_sha256,
                "derived_sha256": derived_sha256,
                "app_version": app_version,
                "analyzed_at": created_at,
                "analysis_input": "processed trace",
                "raw_data_modified": False,
                "processing_config": config,
            }
            if any(decoded_result.get(key) != value for key, value in expected_fields.items()):
                raise ValueError(f"Analysis run integrity verification failed: {run_id}")
            if derived_trace is None:  # legacy run: no retained trace to verify
                continue
            trace = json.loads(derived_trace)
            x_values, y_values = trace.get("x"), trace.get("y")
            if (
                not isinstance(x_values, list)
                or not isinstance(y_values, list)
                or not 3 <= len(x_values) == len(y_values) <= 500000
                or any(
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    for value in x_values + y_values
                )
                or not all(math.isfinite(value) for value in x_values + y_values)
                or any(right <= left for left, right in zip(x_values, x_values[1:]))
            ):
                raise ValueError(f"Analysis run integrity verification failed: {run_id}")
            canonical_trace = json.dumps(
                {"x": x_values, "y": y_values},
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if hashlib.sha256(canonical_trace).hexdigest() != derived_sha256:
                raise ValueError(f"Analysis run integrity verification failed: {run_id}")

    verified = []
    for file_id, original_filename, expected_size, expected_sha256, stored_path in rows:
        path = Path(stored_path).resolve()
        try:
            relative_path = path.relative_to(raw_data_dir)
        except ValueError as error:
            raise ValueError(f"Cataloged raw file is outside the raw-data directory: {file_id}") from error
        if not path.is_file():
            raise FileNotFoundError(f"Cataloged raw file is missing: {file_id} ({original_filename})")
        actual_size = path.stat().st_size
        if actual_size != expected_size:
            raise ValueError(f"Cataloged raw file size mismatch: {file_id} ({original_filename})")
        actual_sha256 = sha256_file(path)
        if actual_sha256 != expected_sha256:
            raise ValueError(f"Cataloged raw file checksum mismatch: {file_id} ({original_filename})")
        verified.append({
            "file_id": file_id,
            "original_filename": original_filename,
            "path": (Path("raw") / relative_path).as_posix(),
            "size_bytes": actual_size,
            "sha256": actual_sha256,
        })
    return verified


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

    verified_catalog = verify_source_catalog(database_path, raw_data_dir)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_name = f"materials-data-copilot-{timestamp}"
    backup_directory = destination_root / backup_name
    partial_backup_directory = destination_root / f".{backup_name}.partial"
    partial_backup_directory.mkdir(parents=True, exist_ok=False)

    try:
        backup_database = partial_backup_directory / database_path.name
        source_uri = f"{database_path.as_uri()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
            with closing(sqlite3.connect(backup_database)) as backup_connection:
                source_connection.backup(backup_connection)

        with closing(sqlite3.connect(backup_database)) as backup_connection:
            backup_integrity = backup_connection.execute("PRAGMA integrity_check").fetchone()[0]
        if backup_integrity != "ok":
            raise ValueError(f"Backup database integrity check failed: {backup_integrity}")

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
        copied_by_path = {item["path"]: item for item in raw_files}
        for item in verified_catalog:
            copied = copied_by_path.get(item["path"])
            if copied is None or copied["size_bytes"] != item["size_bytes"] or copied["sha256"] != item["sha256"]:
                raise ValueError(f"Backup copy verification failed: {item['file_id']} ({item['original_filename']})")

        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": backup_database.name,
            "database_sha256": sha256_file(backup_database),
            "database_integrity": backup_integrity,
            "catalog_record_count": len(verified_catalog),
            "catalog_files_verified": verified_catalog,
            "raw_file_count": len(raw_files),
            "orphan_raw_file_count": len(raw_files) - len(verified_catalog),
            "raw_files": raw_files,
        }
        (partial_backup_directory / "manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )
        partial_backup_directory.rename(backup_directory)
    except Exception:
        shutil.rmtree(partial_backup_directory, ignore_errors=True)
        raise
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
