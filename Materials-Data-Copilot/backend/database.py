import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DATABASE_PATH = Path(__file__).parent / "data" / "materials_data_copilot.db"


class StoredDataIntegrityError(ValueError):
    """Raised when persisted structured data cannot be decoded safely."""

IMPORTED_FILE_COLUMNS = """
    file_id,
    original_filename,
    content_type,
    size_bytes,
    sha256,
    storage_path,
    imported_at,
    technique,
    sample_id,
    measurement_date,
    instrument,
    operator,
    notes,
    material_system,
    extended_metadata
"""


def deserialize_imported_file(record: sqlite3.Row) -> dict:
    result = dict(record)
    serialized = result.pop("extended_metadata", None)
    try:
        result["experimental_details"] = json.loads(serialized) if serialized else {}
    except (json.JSONDecodeError, TypeError) as error:
        raise StoredDataIntegrityError(
            f"Imported file {result.get('file_id', '<unknown>')} has invalid extended metadata."
        ) from error
    return result


@contextmanager
def connect_database() -> Iterator[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(DATABASE_PATH, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


def initialize_database() -> None:
    with connect_database() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS imported_files (
                file_id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                sha256 TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                technique TEXT,
                sample_id TEXT,
                measurement_date TEXT,
                instrument TEXT,
                operator TEXT,
                notes TEXT,
                material_system TEXT,
                extended_metadata TEXT
            )
            """
        )

        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(imported_files)")
        }
        metadata_columns = {
            "technique": "TEXT",
            "sample_id": "TEXT",
            "measurement_date": "TEXT",
            "instrument": "TEXT",
            "operator": "TEXT",
            "notes": "TEXT",
            "material_system": "TEXT",
            "extended_metadata": "TEXT",
        }
        for column_name, column_type in metadata_columns.items():
            if column_name not in existing_columns:
                connection.execute(
                    f"ALTER TABLE imported_files "
                    f"ADD COLUMN {column_name} {column_type}"
                )

        duplicate_checksum = connection.execute(
            """
            SELECT sha256
            FROM imported_files
            GROUP BY sha256
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        if duplicate_checksum is None:
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_imported_files_sha256
                ON imported_files (sha256)
                """
            )

        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS
                reject_duplicate_imported_file_sha256
            BEFORE INSERT ON imported_files
            WHEN EXISTS (
                SELECT 1
                FROM imported_files
                WHERE sha256 = NEW.sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'duplicate sha256');
            END
            """
        )
        # Samples table for selecting/creating reusable samples
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS samples (
                sample_id TEXT PRIMARY KEY,
                material TEXT,
                substrate TEXT,
                project TEXT,
                created_at TEXT,
                notes TEXT
            )
            """
        )
        # Presets table for instrument/measurement presets (stored as JSON blob)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS presets (
                preset_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                config TEXT NOT NULL,
                created_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS archived_imports (
                file_id TEXT PRIMARY KEY,
                archived_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_recipes (
                recipe_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                config TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS substrate_peak_feedback (
                feedback_id TEXT PRIMARY KEY,
                source_file_id TEXT NOT NULL,
                material_system TEXT,
                center REAL NOT NULL,
                half_width REAL NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_runs (
                run_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                derived_sha256 TEXT NOT NULL,
                derived_trace TEXT NOT NULL,
                processing_config TEXT NOT NULL,
                result TEXT NOT NULL,
                app_version TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        analysis_run_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(analysis_runs)")
        }
        if "derived_trace" not in analysis_run_columns:
            # Legacy runs did not retain their processed X/Y arrays. They stay
            # readable but are reported as unverifiable; every new run must
            # provide an immutable trace through the INSERT trigger below.
            connection.execute("ALTER TABLE analysis_runs ADD COLUMN derived_trace TEXT")
        json_object_columns = (
            ("imported_files", "extended_metadata", True),
            ("presets", "config", False),
            ("analysis_recipes", "config", False),
            ("analysis_runs", "processing_config", False),
            ("analysis_runs", "derived_trace", False),
            ("analysis_runs", "result", False),
        )
        for table_name, column_name, nullable in json_object_columns:
            null_clause = f"NEW.{column_name} IS NOT NULL AND " if nullable else ""
            validity_expression = (
                f"CASE WHEN json_valid(NEW.{column_name}) "
                f"THEN json_type(NEW.{column_name}) ELSE NULL END IS NOT 'object'"
            )
            for operation in ("INSERT", "UPDATE"):
                trigger_name = f"validate_{table_name}_{column_name}_{operation.lower()}"
                connection.execute(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS {trigger_name}
                    BEFORE {operation} ON {table_name}
                    WHEN {null_clause}({validity_expression})
                    BEGIN
                        SELECT RAISE(ABORT, '{table_name}.{column_name} must be a valid JSON object');
                    END
                    """
                )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS validate_analysis_run_trace_shape
            BEFORE INSERT ON analysis_runs
            WHEN CASE WHEN json_valid(NEW.derived_trace)
                      THEN json_type(NEW.derived_trace, '$.x') ELSE NULL END IS NOT 'array'
              OR CASE WHEN json_valid(NEW.derived_trace)
                      THEN json_type(NEW.derived_trace, '$.y') ELSE NULL END IS NOT 'array'
              OR json_array_length(NEW.derived_trace, '$.x') < 3
              OR json_array_length(NEW.derived_trace, '$.x')
                   <> json_array_length(NEW.derived_trace, '$.y')
            BEGIN
                SELECT RAISE(ABORT, 'analysis run derived trace must contain equal x/y arrays with at least three points');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS validate_analysis_run_result_identity
            BEFORE INSERT ON analysis_runs
            WHEN CASE WHEN json_valid(NEW.result)
                      THEN json_type(NEW.result) ELSE NULL END IS 'object'
             AND (json_extract(NEW.result, '$.run_id') IS NOT NEW.run_id
              OR json_extract(NEW.result, '$.file_id') IS NOT NEW.file_id
              OR json_extract(NEW.result, '$.sha256') IS NOT NEW.raw_sha256
              OR json_extract(NEW.result, '$.derived_sha256') IS NOT NEW.derived_sha256
              OR json_extract(NEW.result, '$.app_version') IS NOT NEW.app_version
              OR json_extract(NEW.result, '$.analyzed_at') IS NOT NEW.created_at
              OR json_extract(NEW.result, '$.analysis_input') IS NOT 'processed trace'
              OR json_extract(NEW.result, '$.raw_data_modified') IS NOT 0
              OR json(NEW.processing_config)
                   <> json(json_extract(NEW.result, '$.processing_config')))
            BEGIN
                SELECT RAISE(ABORT, 'analysis run result provenance does not match its immutable columns');
            END
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS analysis_runs_file_created
            ON analysis_runs (file_id, created_at DESC)
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS reject_orphan_analysis_run
            BEFORE INSERT ON analysis_runs
            WHEN NOT EXISTS (
                SELECT 1 FROM imported_files WHERE file_id = NEW.file_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'analysis run source file does not exist');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS reject_analysis_run_raw_sha_mismatch
            BEFORE INSERT ON analysis_runs
            WHEN EXISTS (
                SELECT 1
                FROM imported_files
                WHERE file_id = NEW.file_id
                  AND sha256 <> NEW.raw_sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'analysis run raw checksum does not match source file');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS reject_analysis_run_update
            BEFORE UPDATE ON analysis_runs
            BEGIN
                SELECT RAISE(ABORT, 'analysis runs are immutable');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS reject_analysis_run_delete
            BEFORE DELETE ON analysis_runs
            BEGIN
                SELECT RAISE(ABORT, 'analysis runs are immutable');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS protect_analyzed_import_identity
            BEFORE UPDATE OF file_id, sha256 ON imported_files
            WHEN (OLD.file_id <> NEW.file_id OR OLD.sha256 <> NEW.sha256)
              AND EXISTS (
                  SELECT 1 FROM analysis_runs WHERE file_id = OLD.file_id
              )
            BEGIN
                SELECT RAISE(ABORT, 'analyzed source identity and checksum are immutable');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS protect_analyzed_import_delete
            BEFORE DELETE ON imported_files
            WHEN EXISTS (
                SELECT 1 FROM analysis_runs WHERE file_id = OLD.file_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'analyzed source records cannot be deleted');
            END
            """
        )


def list_imported_files() -> list[dict]:
    with connect_database() as connection:
        records = connection.execute(
            f"""
            SELECT {IMPORTED_FILE_COLUMNS}
            FROM imported_files
            WHERE NOT EXISTS (
                SELECT 1 FROM archived_imports
                WHERE archived_imports.file_id = imported_files.file_id
            )
            ORDER BY imported_at DESC, file_id DESC
            """
        ).fetchall()

    return [deserialize_imported_file(record) for record in records]


def list_operators() -> list[str]:
    """Return known operators with the most recently used name first."""
    with connect_database() as connection:
        records = connection.execute(
            """
            SELECT operator, MAX(imported_at) AS last_used_at
            FROM imported_files
            WHERE operator IS NOT NULL AND TRIM(operator) <> ''
            GROUP BY operator
            ORDER BY last_used_at DESC, operator COLLATE NOCASE
            """
        ).fetchall()
    return [record["operator"] for record in records]


def list_samples() -> list[dict]:
    with connect_database() as connection:
        records = connection.execute(
            """
            SELECT sample_id, material, substrate, project, created_at, notes
            FROM samples
            ORDER BY created_at DESC, sample_id DESC
            """
        ).fetchall()

    return [dict(record) for record in records]


def search_samples(q: str) -> list[dict]:
    with connect_database() as connection:
        pattern = f"%{q}%"
        records = connection.execute(
            """
            SELECT sample_id, material, substrate, project, created_at, notes
            FROM samples
            WHERE sample_id LIKE ? OR material LIKE ?
            ORDER BY created_at DESC, sample_id DESC
            LIMIT 20
            """,
            (pattern, pattern),
        ).fetchall()
    return [dict(record) for record in records]


def create_sample(connection: sqlite3.Connection, sample: dict) -> None:
    connection.execute(
        """
        INSERT INTO samples (
            sample_id, material, substrate, project, created_at, notes
        ) VALUES (
            :sample_id, :material, :substrate, :project, :created_at, :notes
        )
        """,
        sample,
    )


def list_presets() -> list[dict]:
    with connect_database() as connection:
        records = connection.execute(
            """
            SELECT preset_id, name, config, created_at
            FROM presets
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [dict(record) for record in records]


def create_preset(connection: sqlite3.Connection, preset: dict) -> None:
    connection.execute(
        """
        INSERT INTO presets (
            preset_id, name, config, created_at
        ) VALUES (
            :preset_id, :name, :config, :created_at
        )
        """,
        preset,
    )


def get_preset(preset_id: str) -> dict | None:
    with connect_database() as connection:
        record = connection.execute(
            """
            SELECT preset_id, name, config, created_at
            FROM presets
            WHERE preset_id = ?
            """,
            (preset_id,)
        ).fetchone()
    return deserialize_imported_file(record) if record is not None else None


def update_preset(connection: sqlite3.Connection, preset_id: str, name: str, config: str) -> None:
    connection.execute(
        """
        UPDATE presets
        SET name = :name, config = :config
        WHERE preset_id = :preset_id
        """,
        {
            "preset_id": preset_id,
            "name": name,
            "config": config,
        },
    )


def get_imported_file(file_id: str) -> dict | None:
    with connect_database() as connection:
        record = connection.execute(
            f"""
            SELECT {IMPORTED_FILE_COLUMNS}
            FROM imported_files
            WHERE file_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM archived_imports
                  WHERE archived_imports.file_id = imported_files.file_id
              )
            """,
            (file_id,),
        ).fetchone()

    return deserialize_imported_file(record) if record is not None else None


def find_imported_file_by_sha256(
    connection: sqlite3.Connection,
    sha256: str,
) -> dict | None:
    record = connection.execute(
        f"""
        SELECT {IMPORTED_FILE_COLUMNS}
        FROM imported_files
        WHERE sha256 = ?
        """,
        (sha256,),
    ).fetchone()
    return deserialize_imported_file(record) if record is not None else None


def insert_imported_file(
    connection: sqlite3.Connection,
    metadata: dict,
) -> None:
    connection.execute(
        """
        INSERT INTO imported_files (
            file_id,
            original_filename,
            content_type,
            size_bytes,
            sha256,
            storage_path,
            imported_at,
            technique,
            sample_id,
            measurement_date,
            instrument,
            operator,
            notes,
            material_system,
            extended_metadata
        )
        VALUES (
            :file_id,
            :original_filename,
            :content_type,
            :size_bytes,
            :sha256,
            :storage_path,
            :imported_at,
            :technique,
            :sample_id,
            :measurement_date,
            :instrument,
            :operator,
            :notes,
            :material_system,
            :extended_metadata
        )
        """,
        {
            **metadata,
            "material_system": metadata.get("material_system"),
            "extended_metadata": json.dumps(metadata.get("experimental_details", {})),
        },
    )


def update_imported_file_metadata(
    connection: sqlite3.Connection,
    file_id: str,
    metadata: dict,
) -> None:
    connection.execute(
        """
        UPDATE imported_files
        SET technique = :technique,
            sample_id = :sample_id,
            measurement_date = :measurement_date,
            instrument = :instrument,
            operator = :operator,
            notes = :notes,
            material_system = :material_system,
            extended_metadata = :extended_metadata
        WHERE file_id = :file_id
        """,
        {
            "file_id": file_id,
            **metadata,
            "material_system": metadata.get("material_system"),
            "extended_metadata": json.dumps(metadata.get("experimental_details", {})),
        },
    )


def archive_imported_file(
    connection: sqlite3.Connection,
    file_id: str,
    archived_at: str,
) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO archived_imports (file_id, archived_at)
        VALUES (?, ?)
        """,
        (file_id, archived_at),
    )


def archive_imported_files(
    connection: sqlite3.Connection,
    file_ids: list[str],
    archived_at: str,
) -> int:
    placeholders = ",".join("?" for _ in file_ids)
    existing = connection.execute(
        f"""
        SELECT file_id
        FROM imported_files
        WHERE file_id IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1 FROM archived_imports
              WHERE archived_imports.file_id = imported_files.file_id
          )
        """,
        file_ids,
    ).fetchall()
    existing_ids = [record["file_id"] for record in existing]
    connection.executemany(
        "INSERT OR REPLACE INTO archived_imports (file_id, archived_at) VALUES (?, ?)",
        [(file_id, archived_at) for file_id in existing_ids],
    )
    return len(existing_ids)


def is_import_archived(
    connection: sqlite3.Connection,
    file_id: str,
) -> bool:
    return connection.execute(
        "SELECT 1 FROM archived_imports WHERE file_id = ?",
        (file_id,),
    ).fetchone() is not None


def restore_imported_file(
    connection: sqlite3.Connection,
    file_id: str,
    metadata: dict,
) -> None:
    connection.execute(
        """
        UPDATE imported_files
        SET imported_at = :imported_at,
            technique = :technique,
            sample_id = :sample_id,
            measurement_date = :measurement_date,
            instrument = :instrument,
            operator = :operator,
            notes = :notes,
            material_system = :material_system,
            extended_metadata = :extended_metadata
        WHERE file_id = :file_id
        """,
        {
            "file_id": file_id,
            **metadata,
            "material_system": metadata.get("material_system"),
            "extended_metadata": json.dumps(metadata.get("experimental_details", {})),
        },
    )
    connection.execute(
        "DELETE FROM archived_imports WHERE file_id = ?",
        (file_id,),
    )


def list_analysis_recipes() -> list[dict]:
    with connect_database() as connection:
        rows = connection.execute(
            "SELECT recipe_id, name, config, created_at FROM analysis_recipes ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def create_analysis_recipe(connection: sqlite3.Connection, recipe: dict) -> None:
    connection.execute(
        "INSERT INTO analysis_recipes (recipe_id, name, config, created_at) VALUES (:recipe_id, :name, :config, :created_at)",
        recipe,
    )


def create_analysis_run(connection: sqlite3.Connection, run: dict) -> None:
    connection.execute(
        """INSERT INTO analysis_runs
           (run_id, file_id, raw_sha256, derived_sha256, derived_trace, processing_config,
            result, app_version, created_at)
           VALUES (:run_id, :file_id, :raw_sha256, :derived_sha256,
                   :derived_trace, :processing_config, :result, :app_version, :created_at)""",
        run,
    )


def list_analysis_runs(file_id: str) -> list[dict]:
    with connect_database() as connection:
        rows = connection.execute(
            """SELECT run_id, file_id, raw_sha256, derived_sha256, derived_trace,
                      processing_config, result, app_version, created_at
               FROM analysis_runs
               WHERE file_id = ?
               ORDER BY created_at DESC, run_id DESC""",
            (file_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_analysis_run(run_id: str) -> dict | None:
    with connect_database() as connection:
        row = connection.execute(
            """SELECT run_id, file_id, raw_sha256, derived_sha256, derived_trace,
                      processing_config, result, app_version, created_at
               FROM analysis_runs WHERE run_id = ?""",
            (run_id,),
        ).fetchone()
    return dict(row) if row else None


def list_substrate_peak_feedback(material_system: str | None = None) -> list[dict]:
    with connect_database() as connection:
        if material_system is None:
            rows = connection.execute("SELECT * FROM substrate_peak_feedback ORDER BY created_at").fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM substrate_peak_feedback WHERE material_system = ? ORDER BY created_at",
                (material_system,),
            ).fetchall()
    return [dict(row) for row in rows]


def create_substrate_peak_feedback(connection: sqlite3.Connection, feedback: dict) -> None:
    connection.execute(
        """INSERT INTO substrate_peak_feedback
           (feedback_id, source_file_id, material_system, center, half_width, action, created_at)
           VALUES (:feedback_id, :source_file_id, :material_system, :center, :half_width, :action, :created_at)""",
        feedback,
    )


if __name__ == "__main__":
    initialize_database()
    print(f"Database initialized at: {DATABASE_PATH}")
