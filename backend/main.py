import hashlib
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, UploadFile

app = FastAPI(
    title="Materials Data Copilot API",
    version="0.1.0",
)

RAW_DATA_DIR = Path(__file__).parent / "data" / "raw"
CHUNK_SIZE = 1024 * 1024  # 1 MiB


@app.get("/")
def read_root():
    return {
        "application": "Materials Data Copilot",
        "status": "running",
        "version": "0.1.0",
    }


@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.post("/files/import", status_code=201)
async def import_file(file: UploadFile = File(...)):
    original_filename = Path(file.filename or "unnamed-file").name
    file_id = str(uuid4())

    destination_directory = RAW_DATA_DIR / file_id
    destination_directory.mkdir(parents=True, exist_ok=False)

    destination_path = destination_directory / original_filename
    checksum = hashlib.sha256()
    size_bytes = 0

    with destination_path.open("wb") as output_file:
        while chunk := await file.read(CHUNK_SIZE):
            output_file.write(chunk)
            checksum.update(chunk)
            size_bytes += len(chunk)

    await file.close()

    return {
        "file_id": file_id,
        "original_filename": original_filename,
        "content_type": file.content_type,
        "size_bytes": size_bytes,
        "sha256": checksum.hexdigest(),
    }