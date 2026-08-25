import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DATABASE_PATH = Path(__file__).parent / "data" / "materials_data_copilot.db"

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
    except json.JSONDecodeError:
        result["experimental_details"] = {}
    return result


@contextmanager
def connect_database() -> Iterator[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
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
        f"SELECT file_id FROM imported_files WHERE file_id IN ({placeholders})",
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


if __name__ == "__main__":
    initialize_database()
    print(f"Database initialized at: {DATABASE_PATH}")
