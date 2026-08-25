import hashlib
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import database

logger = logging.getLogger(__name__)


RAW_DATA_DIR = Path(__file__).parent / "data" / "raw"
UPLOAD_INTERFACE_PATH = Path(__file__).parent / "static" / "upload.html"
CHUNK_SIZE = 1024 * 1024  # 1 MiB
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MiB
MAX_FILENAME_LENGTH = 200


@asynccontextmanager
async def lifespan(_app: FastAPI):
    database.initialize_database()
    yield


app = FastAPI(
    title="Materials Data Copilot API",
    version="0.2.0",
    lifespan=lifespan,
)


def validation_error(code: str, message: str, status_code: int = 422):
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def extract_original_filename(filename: str | None) -> str:
    if not filename or not filename.strip():
        raise validation_error("invalid_filename", "A filename is required.")

    original_filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if original_filename in {"", ".", ".."}:
        raise validation_error("invalid_filename", "A valid filename is required.")
    return original_filename


def sanitize_filename(original_filename: str) -> str:
    sanitized = re.sub(
        r'[\x00-\x1f<>:"/\\|?*]+',
        "_",
        original_filename,
    )
    sanitized = sanitized.strip(" .")

    if not sanitized or sanitized in {".", ".."}:
        raise validation_error(
            "invalid_filename",
            "The filename does not contain any safe characters.",
        )

    reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    if sanitized.split(".", 1)[0].upper() in reserved_names:
        sanitized = f"_{sanitized}"

    if len(sanitized) > MAX_FILENAME_LENGTH:
        suffix = Path(sanitized).suffix[:20]
        stem_length = MAX_FILENAME_LENGTH - len(suffix)
        sanitized = f"{Path(sanitized).stem[:stem_length]}{suffix}"

    return sanitized


def normalize_required_metadata(value: str, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise validation_error(
            "missing_metadata",
            f"{field_name} is required.",
        )
    if len(normalized) > maximum:
        raise validation_error(
            "metadata_too_long",
            f"{field_name} must be {maximum} characters or fewer.",
        )
    return normalized


def normalize_optional_metadata(
    value: str | None,
    field_name: str,
    maximum: int,
) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    if len(normalized) > maximum:
        raise validation_error(
            "metadata_too_long",
            f"{field_name} must be {maximum} characters or fewer.",
        )
    return normalized


def normalize_measurement_date(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as error:
        raise validation_error(
            "invalid_measurement_date",
            "Measurement date must use YYYY-MM-DD format.",
        ) from error


async def stream_upload_to_staging(
    file: UploadFile,
    staging_path: Path,
) -> tuple[int, str]:
    checksum = hashlib.sha256()
    size_bytes = 0

    with staging_path.open("xb") as output_file:
        while chunk := await file.read(CHUNK_SIZE):
            if size_bytes + len(chunk) > MAX_FILE_SIZE_BYTES:
                raise validation_error(
                    "file_too_large",
                    f"Files must be no larger than {MAX_FILE_SIZE_BYTES} bytes.",
                    status_code=413,
                )
            output_file.write(chunk)
            checksum.update(chunk)
            size_bytes += len(chunk)

    if size_bytes == 0:
        raise validation_error("empty_file", "Empty files cannot be imported.")

    return size_bytes, checksum.hexdigest()


def cleanup_created_destination(
    destination_path: Path,
    destination_directory: Path,
) -> None:
    if destination_path.exists():
        destination_path.unlink()
    if destination_directory.exists():
        destination_directory.rmdir()


@app.get("/")
def read_root():
    return {
        "application": "Materials Data Copilot",
        "status": "running",
        "version": "0.2.0",
    }


@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.get("/upload", response_class=FileResponse)
def upload_interface():
    return FileResponse(UPLOAD_INTERFACE_PATH)


@app.get("/files")
def list_imported_files():
    return database.list_imported_files()


@app.get("/files/{file_id}")
def retrieve_imported_file(file_id: str):
    record = database.get_imported_file(file_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "file_not_found",
                "message": "No imported file has the requested ID.",
            },
        )
    return record


@app.post("/files/import", status_code=201)
async def import_file(
    file: UploadFile = File(...),
    technique: str = Form(...),
    sample_id: str = Form(...),
    measurement_date: str | None = Form(None),
    instrument: str | None = Form(None),
    operator: str | None = Form(None),
    notes: str | None = Form(None),
):
    staging_path = None
    destination_directory = None
    destination_path = None
    destination_created = False

    try:
        original_filename = extract_original_filename(file.filename)
        stored_filename = sanitize_filename(original_filename)
        normalized_technique = normalize_required_metadata(
            technique,
            "Technique",
            100,
        )
        normalized_sample_id = normalize_required_metadata(
            sample_id,
            "Sample ID",
            100,
        )
        normalized_measurement_date = normalize_measurement_date(
            measurement_date
        )
        normalized_instrument = normalize_optional_metadata(
            instrument,
            "Instrument",
            200,
        )
        normalized_operator = normalize_optional_metadata(
            operator,
            "Operator",
            100,
        )
        normalized_notes = normalize_optional_metadata(notes, "Notes", 2000)

        file_id = str(uuid4())
        staging_directory = RAW_DATA_DIR / ".staging"
        staging_directory.mkdir(parents=True, exist_ok=True)
        staging_path = staging_directory / f"{file_id}.upload"

        size_bytes, sha256 = await stream_upload_to_staging(
            file,
            staging_path,
        )
        imported_at = datetime.now(timezone.utc).isoformat()
        content_type = file.content_type

        destination_directory = RAW_DATA_DIR / file_id
        destination_path = destination_directory / stored_filename
        metadata = {
            "file_id": file_id,
            "original_filename": original_filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "storage_path": str(destination_path),
            "imported_at": imported_at,
            "technique": normalized_technique,
            "sample_id": normalized_sample_id,
            "measurement_date": normalized_measurement_date,
            "instrument": normalized_instrument,
            "operator": normalized_operator,
            "notes": normalized_notes,
        }

        with database.connect_database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = database.find_imported_file_by_sha256(
                connection,
                sha256,
            )
            if duplicate is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "duplicate_file",
                        "message": "An identical file has already been imported.",
                        "existing_file_id": duplicate["file_id"],
                    },
                )

            destination_directory.mkdir(parents=True, exist_ok=False)
            destination_created = True
            os.replace(staging_path, destination_path)
            database.insert_imported_file(connection, metadata)

        return metadata
    except HTTPException:
        if destination_created:
            cleanup_created_destination(
                destination_path,
                destination_directory,
            )
        raise
    except (OSError, sqlite3.Error) as error:
        cleanup_failed = False
        if destination_created:
            try:
                cleanup_created_destination(
                    destination_path,
                    destination_directory,
                )
            except OSError:
                cleanup_failed = True
                logger.exception("Failed to roll back an incomplete file import")
        logger.exception("File import failed and was rolled back")
        message = "The import failed; no file or metadata was retained."
        if cleanup_failed:
            message = (
                "The import failed and automatic cleanup was incomplete. "
                "Inspect the raw-data directory before retrying."
            )
        raise HTTPException(
            status_code=500,
            detail={"code": "import_failed", "message": message},
        ) from error
    finally:
        await file.close()
        if staging_path is not None and staging_path.exists():
            try:
                staging_path.unlink()
            except OSError:
                logger.exception("Failed to remove a staged upload")
