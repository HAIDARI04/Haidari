# Materials Data Copilot

A local-first application for organizing, processing, analyzing, and documenting materials-science experimental data.

## Initial objectives

- Preserve imported raw data without modification
- Generate and display SHA-256 checksums
- Maintain complete processing provenance
- Perform reproducible Raman analysis
- Export processed data and reviewer-auditable reports
- Support synchronized project storage across multiple computers

## Run locally

From the repository root on Windows:

```powershell
backend\.venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --reload
```

Open `http://127.0.0.1:8000/upload` to import a file with experimental
metadata. The API documentation is available at `http://127.0.0.1:8000/docs`.

Run the complete backend suite with:

```powershell
backend\.venv\Scripts\python.exe -m pytest -vv
```

## Local backups

Stop the API before taking a pilot-data backup so the SQLite snapshot and raw
files represent the same point in time. Choose a destination outside
`backend/data`:

```powershell
backend\.venv\Scripts\python.exe backend\backup.py D:\Materials-Backups
```

The command creates a timestamped directory containing a safe SQLite backup,
an exact copy of imported raw files, and `manifest.json` with sizes and SHA-256
checksums. Copy the backup to a second physical device or approved synchronized
storage and periodically test restoration.

## Development status

Phase 1: Pilot-ready local import, metadata catalog, and backup tooling.
