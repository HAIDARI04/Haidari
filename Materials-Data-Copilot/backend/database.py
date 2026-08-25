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
    notes
"""


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
                notes TEXT
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


def list_imported_files() -> list[dict]:
    with connect_database() as connection:
        records = connection.execute(
            f"""
            SELECT {IMPORTED_FILE_COLUMNS}
            FROM imported_files
            ORDER BY imported_at DESC, file_id DESC
            """
        ).fetchall()

    return [dict(record) for record in records]


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

    return dict(record) if record is not None else None


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
    return dict(record) if record is not None else None


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
            notes
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
            :notes
        )
        """,
        metadata,
    )


if __name__ == "__main__":
    initialize_database()
    print(f"Database initialized at: {DATABASE_PATH}")
