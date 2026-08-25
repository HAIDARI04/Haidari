import hashlib
from bisect import bisect_right
import csv
import logging
import math
import mimetypes
import os
import re
import sqlite3
import statistics
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4
import json

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi import Body
from fastapi import UploadFile as FastAPIUploadFile

import database

logger = logging.getLogger(__name__)


RAW_DATA_DIR = Path(__file__).parent / "data" / "raw"
UPLOAD_INTERFACE_PATH = Path(__file__).parent / "static" / "upload.html"
CHUNK_SIZE = 1024 * 1024  # 1 MiB
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MiB
MAX_FILENAME_LENGTH = 200
RAMAN_HEADER_LIMIT = 100
APP_VERSION = "0.5.0"
SUBSTRATE_MODEL_VERSION = "sio2-si-adaptive-4"
PROCESSING_PIPELINE_ORDER = "baseline > cosmic_rays > rayleigh > substrate_reference > smoothing > normalization"
SIO2_SI_MATERIAL_KEYS = frozenset({
    "sio2si", "sio2onsi", "silicondioxideonsilicon",
})
SPECTRUM_TEXT_EXTENSIONS = frozenset({
    ".asc", ".csv", ".dat", ".log", ".raman", ".tsv", ".txt", ".xy",
})
EXPERIMENTAL_METADATA_FIELDS = (
    "measurement_type", "laser_wavelength", "laser_power", "power_at_sample",
    "objective", "integration_time", "accumulations", "detector",
    "detector_model", "detector_temperature", "spectrometer", "grating",
    "binning_start", "binning_length", "spectral_start", "spectral_range",
    "x_units", "lpf_angle", "bpf_angle", "display_mode", "live_focus",
    "z_position",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    database.initialize_database()
    yield


app = FastAPI(
    title="Materials Data Copilot API",
    version=APP_VERSION,
    lifespan=lifespan,
)


@app.exception_handler(database.StoredDataIntegrityError)
async def stored_data_integrity_error_handler(_request, error):
    return JSONResponse(
        status_code=409,
        content={
            "detail": {
                "code": "stored_data_invalid",
                "message": str(error),
            }
        },
    )


@app.middleware("http")
async def add_browser_security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'self'; form-action 'self'",
    )
    return response


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
    if not isinstance(value, str):
        raise validation_error("invalid_metadata_type", f"{field_name} must be text.")
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
    if value is None:
        return None
    if not isinstance(value, str):
        raise validation_error("invalid_metadata_type", f"{field_name} must be text or null.")
    if not value.strip():
        return None
    normalized = value.strip()
    if len(normalized) > maximum:
        raise validation_error(
            "metadata_too_long",
            f"{field_name} must be {maximum} characters or fewer.",
        )
    return normalized


def normalize_measurement_date(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise validation_error("invalid_measurement_date", "Measurement date must be text in YYYY-MM-DD format.")
    if not value.strip():
        return None
    normalized = value.strip()
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as error:
        raise validation_error(
            "invalid_measurement_date",
            "Measurement date must use YYYY-MM-DD format.",
        ) from error


def serialize_config_object(value, label: str, maximum_bytes: int = 100000) -> str:
    if not isinstance(value, dict):
        raise validation_error(f"invalid_{label}", f"{label.replace('_', ' ')} must be an object.")
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 100:
            raise validation_error(f"invalid_{label}", "Configuration keys must be non-empty text up to 100 characters.")
        if not isinstance(item, (str, bool, int, float)) and item is not None:
            raise validation_error(f"invalid_{label}", "Configuration values must be text, boolean, finite number, or null.")
        if isinstance(item, float) and not math.isfinite(item):
            raise validation_error(f"invalid_{label}", "Configuration numbers must be finite.")
    serialized = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > maximum_bytes:
        raise validation_error(f"{label}_too_large", f"{label.replace('_', ' ')} must be {maximum_bytes // 1000} KB or smaller.")
    return serialized


def parse_stored_json_object(value: str, label: str) -> dict:
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise database.StoredDataIntegrityError(f"Stored {label} is not valid JSON.") from error
    if not isinstance(decoded, dict):
        raise database.StoredDataIntegrityError(f"Stored {label} must be a JSON object.")
    return decoded


def deserialize_analysis_run(run: dict, include_trace: bool = False) -> dict:
    """Validate a saved run's redundant provenance and optional derived trace."""
    decoded = dict(run)
    config = parse_stored_json_object(
        decoded["processing_config"], "analysis processing configuration"
    )
    result = parse_stored_json_object(decoded["result"], "analysis result")
    expected_result_fields = {
        "run_id": decoded["run_id"],
        "file_id": decoded["file_id"],
        "sha256": decoded["raw_sha256"],
        "derived_sha256": decoded["derived_sha256"],
        "app_version": decoded["app_version"],
        "analyzed_at": decoded["created_at"],
        "analysis_input": "processed trace",
        "raw_data_modified": False,
        "processing_config": config,
    }
    if any(result.get(key) != value for key, value in expected_result_fields.items()):
        raise database.StoredDataIntegrityError(
            f"Analysis run {decoded['run_id']} contains inconsistent provenance fields."
        )

    serialized_trace = decoded.pop("derived_trace", None)
    trace = None
    trace_verification = "unavailable_legacy"
    if serialized_trace is not None:
        trace = parse_stored_json_object(serialized_trace, "analysis derived trace")
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
            raise database.StoredDataIntegrityError(
                f"Analysis run {decoded['run_id']} contains an invalid derived trace."
            )
        canonical_trace = json.dumps(
            {"x": x_values, "y": y_values},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        actual_sha256 = hashlib.sha256(canonical_trace).hexdigest()
        if actual_sha256 != decoded["derived_sha256"]:
            raise database.StoredDataIntegrityError(
                f"Analysis run {decoded['run_id']} derived trace checksum does not match."
            )
        trace_verification = "verified"

    decoded["processing_config"] = config
    decoded["result"] = result
    decoded["derived_trace_verification"] = trace_verification
    if include_trace:
        decoded["derived_trace"] = trace
    return decoded


def extract_metadata_suggestions(
    filename: str,
    content: bytes,
    relative_path: str | None = None,
) -> dict[str, str]:
    """Extract only high-confidence metadata from a Raman text header/path."""
    path_hint = (relative_path or filename).replace("\\", "/")
    suggestions: dict[str, str] = {}
    sample_match = re.search(
        r"(?:^|[/_\-])([A-Z]\d{2})(?=$|[/_\-.])",
        path_hint,
        flags=re.IGNORECASE,
    )
    if sample_match:
        suggestions["sample_id"] = sample_match.group(1).upper()
    if re.search(r"(?:^|[/_\-])raman(?:$|[/_\-.])", path_hint, re.IGNORECASE):
        suggestions["technique"] = "Raman spectroscopy"
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return suggestions

    header: dict[str, str] = {}
    for line in text.splitlines()[:RAMAN_HEADER_LIMIT]:
        if line.strip().upper() == "XYDATA=":
            break
        key, separator, value = line.partition("=")
        if separator and key.strip() and value.strip():
            header[key.strip().upper()] = value.strip()

    file_type = header.get("FILETYPE", "").upper()
    if "RAMAN" in file_type or header.get("TYPE", "").lower() == "ramanshift":
        suggestions["technique"] = "Raman spectroscopy"
    if "RAMAN SPECTRUM" in file_type:
        suggestions["measurement_type"] = "Single spectrum"

    timestamp = header.get("DATETIME", "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T].*)?", timestamp):
        suggestions["measurement_date"] = timestamp[:10]

    laser = header.get("LASER", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", laser):
        suggestions["laser_wavelength"] = laser

    integration_time = header.get("IT", "")
    integration_match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(ms|s)",
        integration_time,
        flags=re.IGNORECASE,
    )
    if integration_match:
        value = float(integration_match.group(1))
        if integration_match.group(2).lower() == "ms":
            value /= 1000
        suggestions["integration_time"] = f"{value:g}"

    accumulations = header.get("SPECTRATAKEN", "")
    if accumulations.isdigit() and int(accumulations) > 0:
        suggestions["accumulations"] = accumulations

    instrument_parts = [
        header[key]
        for key in ("MODEL", "SPECTROMETER")
        if header.get(key)
    ]
    if instrument_parts:
        suggestions["instrument"] = " / ".join(dict.fromkeys(instrument_parts))

    explicit_header_fields = {
        "CCD": "detector",
        "CCDMODEL": "detector_model",
        "TEMPERATURE": "detector_temperature",
        "SPECTROMETER": "spectrometer",
        "GRATING": "grating",
        "BINNING_START": "binning_start",
        "BINNING_LENGTH": "binning_length",
        "STARTFROM": "spectral_start",
        "RSRANGE": "spectral_range",
        "LPFANGLE": "lpf_angle",
        "BPFANGLE": "bpf_angle",
        "DISPLAYMODE": "display_mode",
        "LIVEFOCUS": "live_focus",
        "ZPOSITION": "z_position",
        "XUNITS": "x_units",
    }
    for header_name, field_name in explicit_header_fields.items():
        if header.get(header_name):
            suggestions[field_name] = header[header_name]

    # Instrument configuration fields such as STARTFROM/RSRANGE do not always
    # match the calibrated XY data written to the same file. The plotted XY
    # coordinates are authoritative for the measured spectral limits.
    measured_x, _measured_y, _measured_units = parse_spectrum_xy(content)
    if len(measured_x) >= 2:
        measured_min = min(measured_x)
        measured_max = max(measured_x)
        suggestions["spectral_start"] = f"{measured_min:g}"
        suggestions["spectral_range"] = f"{measured_max:g}"

    return suggestions


def resolve_stored_file(record: dict, *, enforce_recorded_size: bool = True) -> Path:
    """Resolve an imported raw file without allowing paths outside storage."""
    storage_path = Path(record["storage_path"])
    try:
        resolved_path = storage_path.resolve(strict=True)
        resolved_path.relative_to(RAW_DATA_DIR.resolve())
    except (OSError, ValueError) as error:
        code = "stored_file_missing" if not storage_path.exists() else "invalid_storage_path"
        status_code = 404 if code == "stored_file_missing" else 409
        message = "The imported raw file is missing." if status_code == 404 else "Stored file path is invalid."
        raise HTTPException(
            status_code=status_code,
            detail={"code": code, "message": message},
        ) from error
    if not resolved_path.is_file():
        raise HTTPException(
            status_code=404,
            detail={"code": "stored_file_missing", "message": "The imported raw file is missing."},
        )
    expected_size = record.get("size_bytes")
    if (
        enforce_recorded_size
        and isinstance(expected_size, int)
        and resolved_path.stat().st_size != expected_size
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "raw_file_size_mismatch",
                "message": "The stored raw file size no longer matches its import record.",
            },
        )
    return resolved_path


def sha256_file(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as input_file:
        while chunk := input_file.read(CHUNK_SIZE):
            checksum.update(chunk)
    return checksum.hexdigest()


def read_verified_stored_file(record: dict) -> bytes:
    """Read raw bytes only when they still match the immutable import record."""
    storage_path = resolve_stored_file(record)
    content = storage_path.read_bytes()
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != record["sha256"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "raw_file_checksum_mismatch",
                "message": (
                    "The stored raw file checksum no longer matches its import record. "
                    "Analysis and restoration are disabled until the integrity issue is resolved."
                ),
            },
        )
    return content


SAFE_INLINE_IMAGE_TYPES = {
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}


def safe_content_media_type(filename: str) -> tuple[str, bool]:
    """Return a trusted content type and whether inline image display is allowed."""
    suffix = Path(filename).suffix.lower()
    if suffix in SAFE_INLINE_IMAGE_TYPES:
        return SAFE_INLINE_IMAGE_TYPES[suffix], True
    guessed_type = mimetypes.guess_type(filename)[0]
    if guessed_type in {"text/plain", "text/csv", "application/json"}:
        return guessed_type, False
    return "application/octet-stream", False


def parse_spectrum_xy(content: bytes) -> tuple[list[float], list[float], str | None]:
    """Parse instrument XYDATA or a conservative plain two-column spectrum."""
    text = content.decode("utf-8-sig", errors="replace")
    x_values: list[float] = []
    y_values: list[float] = []
    plain_pairs: list[tuple[float, float]] = []
    x_units = None
    in_xy_data = False
    found_xy_marker = False

    for line in text.splitlines():
        stripped = line.strip()
        if not in_xy_data:
            if stripped.upper().startswith("XUNITS="):
                x_units = stripped.partition("=")[2].strip() or None
            if stripped.upper() == "XYDATA=":
                in_xy_data = True
                found_xy_marker = True
                continue

            # A large class of spectrometers export headered CSV/TSV files
            # without an XYDATA marker. Accept only rows containing exactly
            # two finite numeric fields; monotonicity is checked below to
            # avoid interpreting arbitrary tables as spectra.
            plain_parts = [part for part in re.split(r"[,;\t\s]+", stripped) if part]
            if len(plain_parts) == 2:
                try:
                    plain_x, plain_y = map(float, plain_parts)
                except ValueError:
                    pass
                else:
                    if math.isfinite(plain_x) and math.isfinite(plain_y):
                        plain_pairs.append((plain_x, plain_y))
            continue

        parts = re.split(r"[,;\t\s]+", stripped)
        if len(parts) < 2:
            continue
        try:
            x_value = float(parts[0])
            y_value = float(parts[1])
        except ValueError:
            continue
        if not math.isfinite(x_value) or not math.isfinite(y_value):
            continue
        x_values.append(x_value)
        y_values.append(y_value)

    if found_xy_marker and len(x_values) >= 2:
        increasing = all(right > left for left, right in zip(x_values, x_values[1:]))
        decreasing = all(right < left for left, right in zip(x_values, x_values[1:]))
        if decreasing:
            x_values.reverse()
            y_values.reverse()
        elif not increasing:
            # Duplicate or reversing coordinates do not define a single-valued
            # spectrum and must not be silently sorted or averaged.
            return [], [], x_units
    elif not found_xy_marker and len(plain_pairs) >= 2:
        increasing = all(right[0] > left[0] for left, right in zip(plain_pairs, plain_pairs[1:]))
        decreasing = all(right[0] < left[0] for left, right in zip(plain_pairs, plain_pairs[1:]))
        if increasing or decreasing:
            ordered_pairs = plain_pairs if increasing else list(reversed(plain_pairs))
            x_values = [pair[0] for pair in ordered_pairs]
            y_values = [pair[1] for pair in ordered_pairs]

            unit_match = re.search(r"(?:raman\s*shift|wavenumber)[^\r\n]*(cm\s*(?:\^?\s*-?1|⁻¹))", text, re.IGNORECASE)
            if unit_match:
                x_units = "cm-1"

    return x_values, y_values, x_units


def measured_spectral_bounds(path: Path) -> tuple[float, float] | None:
    """Scan a complete text spectrum for trustworthy X bounds in constant memory."""
    states = {
        "instrument": {"count": 0, "previous": None, "direction": 0, "valid": True, "minimum": None, "maximum": None},
        "plain": {"count": 0, "previous": None, "direction": 0, "valid": True, "minimum": None, "maximum": None},
    }

    def record_x(state: dict, x_value: float) -> None:
        previous = state["previous"]
        if previous is not None:
            step_direction = 1 if x_value > previous else -1 if x_value < previous else 0
            if step_direction == 0 or (state["direction"] and step_direction != state["direction"]):
                state["valid"] = False
            if not state["direction"]:
                state["direction"] = step_direction
        state["previous"] = x_value
        state["count"] += 1
        state["minimum"] = x_value if state["minimum"] is None else min(state["minimum"], x_value)
        state["maximum"] = x_value if state["maximum"] is None else max(state["maximum"], x_value)

    in_xy_data = False
    found_xy_marker = False
    with path.open("r", encoding="utf-8-sig", errors="replace", newline=None) as source:
        for line in source:
            stripped = line.strip()
            if not in_xy_data and stripped.upper() == "XYDATA=":
                found_xy_marker = True
                in_xy_data = True
                continue
            parts = [part for part in re.split(r"[,;\t\s]+", stripped) if part]
            if len(parts) != 2:
                continue
            try:
                x_value, y_value = map(float, parts)
            except ValueError:
                continue
            if not math.isfinite(x_value) or not math.isfinite(y_value):
                continue
            if in_xy_data:
                record_x(states["instrument"], x_value)
            elif not found_xy_marker:
                record_x(states["plain"], x_value)

    state = states["instrument"] if found_xy_marker else states["plain"]
    if state["count"] < 2 or not state["valid"] or not state["direction"]:
        return None
    return state["minimum"], state["maximum"]


def iter_numeric_matrix_rows(text: str):
    """Yield finite numeric rows from comma, semicolon, tab, or whitespace text."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        delimiter = next((candidate for candidate in ("\t", ";", ",") if candidate in stripped), None)
        if delimiter:
            try:
                fields = next(csv.reader([stripped], delimiter=delimiter, strict=True))
            except csv.Error:
                continue
        else:
            fields = stripped.split()
        try:
            numeric = [float(value.strip()) for value in fields if value.strip()]
        except ValueError:
            continue
        if len(numeric) >= 2 and all(math.isfinite(value) for value in numeric):
            yield numeric


def interpolate_spectrum(source_x, source_y, target_x):
    """Linearly interpolate a valid spectrum, explicitly clamping at its bounds."""
    if len(source_x) != len(source_y) or not source_x:
        raise ValueError("source_x and source_y must be non-empty arrays of equal length")
    pairs = sorted(zip(source_x, source_y))
    if any(right[0] <= left[0] for left, right in zip(pairs, pairs[1:])):
        raise ValueError("source_x coordinates must be unique")
    ordered_x = [pair[0] for pair in pairs]
    ordered_y = [pair[1] for pair in pairs]
    if len(pairs) == 1:
        return [ordered_y[0] for _target in target_x]

    result = []
    for target in target_x:
        if target <= ordered_x[0]:
            result.append(ordered_y[0])
            continue
        if target >= ordered_x[-1]:
            result.append(ordered_y[-1])
            continue
        right_index = bisect_right(ordered_x, target)
        left_index = right_index - 1
        x1, x2 = ordered_x[left_index], ordered_x[right_index]
        y1, y2 = ordered_y[left_index], ordered_y[right_index]
        result.append(y1 + (target - x1) / (x2 - x1) * (y2 - y1))
    return result


def canonical_spectral_units(value: str | None) -> str | None:
    normalized = re.sub(r"[\s_^]", "", (value or "").lower()).replace("−", "-")
    if normalized in {
        "1/cm", "cm-1", "cm⁻¹", "cm⁻1", "ramanshift",
        "reciprocalcentimeter", "reciprocalcentimeters",
    }:
        return "cm-1"
    return normalized or None


def canonical_material_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower().replace("₂", "2"))


def estimate_spectral_noise(values):
    """Robust high-frequency noise estimate using second differences."""
    if len(values) < 3:
        return 0.0
    second_differences = [
        values[index + 1] - 2 * values[index] + values[index - 1]
        for index in range(1, len(values) - 1)
    ]
    center = statistics.median(second_differences)
    mad = statistics.median(abs(value - center) for value in second_differences)
    return mad / (0.6745 * (6 ** 0.5)) if mad else 0.0


def smooth_three_point(values):
    if len(values) < 3:
        return list(values)
    return [
        values[0],
        *(0.25 * values[index - 1] + 0.5 * values[index] + 0.25 * values[index + 1]
          for index in range(1, len(values) - 1)),
        values[-1],
    ]


def moving_average(values, radius):
    if radius <= 0 or len(values) < 3:
        return list(values)
    prefix = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
    result = []
    for index in range(len(values)):
        start = max(0, index - radius)
        end = min(len(values), index + radius + 1)
        result.append((prefix[end] - prefix[start]) / (end - start))
    return result


def moving_minimum(values, radius):
    return [
        min(values[max(0, index - radius):min(len(values), index + radius + 1)])
        for index in range(len(values))
    ]


def moving_maximum(values, radius):
    return [
        max(values[max(0, index - radius):min(len(values), index + radius + 1)])
        for index in range(len(values))
    ]


def rubber_band_baseline(x_values, y_values):
    """Return the piecewise-linear lower convex hull of a spectrum."""
    if len(y_values) < 2:
        return list(y_values)
    hull = []
    for index in range(len(y_values)):
        while len(hull) >= 2:
            first, second = hull[-2], hull[-1]
            cross = (
                (x_values[second] - x_values[first]) * (y_values[index] - y_values[first])
                - (y_values[second] - y_values[first]) * (x_values[index] - x_values[first])
            )
            if cross > 0:
                break
            hull.pop()
        hull.append(index)
    baseline = [0.0] * len(y_values)
    for left, right in zip(hull, hull[1:]):
        span = x_values[right] - x_values[left]
        for index in range(left, right + 1):
            ratio = (x_values[index] - x_values[left]) / span if span else 0.0
            baseline[index] = y_values[left] + ratio * (y_values[right] - y_values[left])
    return baseline


def subtract_baseline_first(x_values, y_values):
    baseline = rubber_band_baseline(x_values, y_values)
    return baseline, [value - baseline[index] for index, value in enumerate(y_values)]


def remove_single_point_cosmic_rays(values):
    """Replace only high-confidence isolated positive impulses after baselining."""
    if len(values) < 5:
        return list(values), []
    residuals = []
    local_medians = [None] * len(values)
    for index in range(2, len(values) - 2):
        neighbors = sorted((values[index - 2], values[index - 1], values[index + 1], values[index + 2]))
        local_median = (neighbors[1] + neighbors[2]) / 2
        local_medians[index] = local_median
        residuals.append(values[index] - local_median)
    center = statistics.median(residuals)
    mad = statistics.median(abs(value - center) for value in residuals)
    intensity_span = max(values) - min(values)
    threshold = max(center + 10 * 1.4826 * mad, 0.03 * intensity_span)
    corrected = list(values)
    removed = []
    for index in range(2, len(values) - 2):
        residual = values[index] - local_medians[index]
        if (
            residual > threshold
            and values[index] > values[index - 1]
            and values[index] > values[index + 1]
            and values[index - 1] <= local_medians[index] + threshold / 3
            and values[index + 1] <= local_medians[index] + threshold / 3
        ):
            corrected[index] = local_medians[index]
            removed.append(index)
    return corrected, removed


def fill_deep_inverse_valleys(values, radius, minimum_depth):
    result = list(values)
    filled = set()
    candidates = []
    for index in range(radius, len(values) - radius):
        if values[index] > values[index - 1] or values[index] > values[index + 1]:
            continue
        left_index, right_index = index - radius, index + radius
        edge_level = min(values[left_index], values[right_index])
        depth = edge_level - values[index]
        if depth >= minimum_depth:
            candidates.append((depth, index))
    selected = []
    for _depth, index in sorted(candidates, reverse=True):
        if all(abs(index - existing) > 2 * radius for existing in selected):
            selected.append(index)
        if len(selected) >= 12:
            break
    for index in selected:
        left_index, right_index = index - radius, index + radius
        for local_index in range(left_index + 1, right_index):
            ratio = (local_index - left_index) / (right_index - left_index)
            bridge = values[left_index] + ratio * (values[right_index] - values[left_index])
            if result[local_index] < bridge:
                result[local_index] = bridge
                filled.add(local_index)
    return result, len(filled)


@app.get("/files/{file_id}/substrate-model")
def model_sio2_si_substrate(file_id: str):
    target_spectrum = retrieve_imported_spectrum(file_id)
    target_record = database.get_imported_file(file_id) or {}
    target_x, raw_target_y = target_spectrum["x"], target_spectrum["y"]
    target_baseline, baseline_corrected_y = subtract_baseline_first(target_x, raw_target_y)
    target_y, target_cosmic_indexes = remove_single_point_cosmic_rays(baseline_corrected_y)
    units = target_spectrum["x_units"]
    target_units = canonical_spectral_units(units)
    target_si_peak = calculate_peak_metrics(target_x, target_y, 500, 540)
    references, reference_names, reference_shifts, reference_records = [], [], [], []
    reference_features = []
    excluded_references = []
    target_span = target_x[-1] - target_x[0]
    for record in database.list_imported_files():
        material = canonical_material_key(record.get("material_system"))
        # Only substrate-only measurements teach the model. A target labelled
        # MoS2/SiO2/Si must never become a reference for another target.
        if record["file_id"] == file_id or material not in SIO2_SI_MATERIAL_KEYS:
            continue
        reference = retrieve_imported_spectrum(record["file_id"])
        reference_units = canonical_spectral_units(reference.get("x_units"))
        if target_units != reference_units and (target_units is not None or reference_units is not None):
            excluded_references.append({
                "filename": record["original_filename"],
                "reason": "Reference and target X-axis units are missing or incompatible.",
                "target_units": target_units,
                "reference_units": reference_units,
            })
            continue
        reference_baseline, reference_corrected = subtract_baseline_first(
            reference["x"], reference["y"]
        )
        reference_cleaned, _reference_cosmic_indexes = remove_single_point_cosmic_rays(
            reference_corrected
        )
        reference_si_peak = calculate_peak_metrics(reference["x"], reference_cleaned, 500, 540)
        shift = (
            target_si_peak["position"] - reference_si_peak["position"]
            if target_si_peak and reference_si_peak else 0.0
        )
        aligned_x = [value + shift for value in reference["x"]]
        overlap_start = max(target_x[0], aligned_x[0])
        overlap_end = min(target_x[-1], aligned_x[-1])
        coverage_fraction = max(0.0, overlap_end - overlap_start) / target_span
        if coverage_fraction < 0.95:
            excluded_references.append({
                "filename": record["original_filename"],
                "reason": "Reference covers less than 95% of the target spectral range after alignment.",
                "range": [round(aligned_x[0], 6), round(aligned_x[-1], 6)],
                "coverage_fraction": round(coverage_fraction, 6),
            })
            continue
        references.append(interpolate_spectrum(aligned_x, reference_cleaned, target_x))
        reference_names.append(record["original_filename"])
        reference_shifts.append(shift)
        reference_records.append(record)
        reference_features.append({
            "file_id": record["file_id"],
            "filename": record["original_filename"],
            "sha256": record["sha256"],
            "silicon_peak_position": reference_si_peak["position"] if reference_si_peak else None,
            "silicon_peak_height": reference_si_peak["height"] if reference_si_peak else None,
            "silicon_peak_area": reference_si_peak["area"] if reference_si_peak else None,
            "silicon_peak_fwhm": reference_si_peak["fwhm"] if reference_si_peak else None,
            "absolute_intensity_min": round(min(reference_cleaned), 6),
            "absolute_intensity_max": round(max(reference_cleaned), 6),
            "noise": round(estimate_spectral_noise(reference_cleaned), 6),
        })
    if not references:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "no_substrate_references",
                "message": "No compatible SiO₂/Si reference spectra are available.",
                "excluded_references": excluded_references,
            },
        )

    low_count = max(3, len(target_y) // 10)
    target_floor = statistics.median(sorted(target_y)[:low_count])
    target_centered = [max(0.0, value - target_floor) for value in target_y]
    basis = []
    for reference in references:
        reference_floor = statistics.median(sorted(reference)[:low_count])
        centered = [max(0.0, value - reference_floor) for value in reference]
        si_peak = max((centered[i] for i, x in enumerate(target_x) if 500 <= x <= 540), default=0.0)
        normalizer = si_peak or max(centered) or 1.0
        basis.append([value / normalizer for value in centered])

    consensus = [statistics.median(values) for values in zip(*basis)]
    consensus_peak = max(consensus) or 1.0
    support_fraction = [
        sum(profile[index] >= 0.02 * consensus_peak for profile in basis) / len(basis)
        for index in range(len(target_x))
    ]
    # Fit where the learned references consistently contain substrate signal.
    # This avoids teaching the fit to remove unrelated target-only Raman peaks.
    anchors = [
        index for index, (x_value, signal) in enumerate(zip(target_x, consensus))
        if x_value >= 80 and support_fraction[index] >= 0.6
        and (signal >= 0.04 * consensus_peak or 500 <= x_value <= 540)
    ]
    if len(anchors) < 3:
        raise validation_error("substrate_overlap_unavailable", "The target does not overlap usable SiO₂/Si reference features.")

    # Non-negative coordinate descent selects a different mixture of reference
    # shapes for every target, rather than subtracting a fixed median graph.
    coefficients = [0.0] * len(basis)
    fitted = [0.0] * len(target_y)
    for _iteration in range(80):
        largest_change = 0.0
        for basis_index, profile in enumerate(basis):
            previous = coefficients[basis_index]
            numerator = sum(
                profile[index] * (target_centered[index] - fitted[index] + previous * profile[index])
                for index in anchors
            )
            denominator = sum(profile[index] ** 2 for index in anchors) or 1.0
            updated = max(0.0, numerator / denominator)
            delta = updated - previous
            if delta:
                coefficients[basis_index] = updated
                for index, value in enumerate(profile):
                    fitted[index] += delta * value
            largest_change = max(largest_change, abs(delta))
        if largest_change < 1e-7:
            break

    substrate_y = [max(0.0, value) for value in fitted]
    local_adjustments = []
    for region_start, region_end in ((230.0, 375.0),):
        region_indexes = [
            index for index, value in enumerate(target_x)
            if region_start <= value <= region_end
        ]
        if len(region_indexes) < 12:
            continue
        best = None
        # This is fitted independently for every target.  The former narrow
        # scale/shift limits were tuned by the T9 spectrum and forced several
        # lower-intensity samples against the same 1.15 boundary, making their
        # supposedly adaptive models look nearly identical.
        for shift_step in range(-20, 21):
            shift = shift_step * 0.25
            shifted = interpolate_spectrum(
                target_x, substrate_y, [target_x[index] - shift for index in region_indexes]
            )
            selected = list(range(len(region_indexes)))
            scale = 1.0
            for _iteration in range(2):
                denominator = sum(shifted[local] ** 2 for local in selected) or 1.0
                scale = max(0.2, min(8.0, sum(
                    shifted[local] * target_y[region_indexes[local]] for local in selected
                ) / denominator))
                residual_order = sorted(
                    selected,
                    key=lambda local: abs(target_y[region_indexes[local]] - scale * shifted[local]),
                )
                selected = residual_order[:max(8, int(0.8 * len(residual_order)))]
            errors = [
                abs(target_y[region_indexes[local]] - scale * shifted[local])
                for local in selected
            ]
            score = statistics.median(errors) if errors else float("inf")
            if best is None or score < best[0]:
                best = (score, shift, scale, shifted)
        if best is None:
            continue
        score, shift, scale, shifted = best
        taper_width = 12.0
        for local, index in enumerate(region_indexes):
            distance_to_edge = min(target_x[index] - region_start, region_end - target_x[index])
            blend = min(1.0, max(0.0, distance_to_edge / taper_width))
            adjusted = max(0.0, scale * shifted[local])
            substrate_y[index] = (1 - blend) * substrate_y[index] + blend * adjusted
        local_adjustments.append({
            "start": region_start, "end": region_end,
            "shift": round(shift, 4), "scale": round(scale, 6),
            "median_absolute_error": round(score, 6),
        })
    smoothing_radius = max(5, min(24, len(substrate_y) // 80))
    reliable_reference_signal = [signal >= 0.02 * consensus_peak for signal in consensus]
    # Preserve the complete learned positive reference pattern. Morphological
    # closing is used only for unusually deep inverse valleys introduced by
    # normalization; ordinary peaks and broad substrate bands remain intact.
    inverse_artifact_threshold = max(1.0, 0.005 * (max(substrate_y) or 1.0))
    substrate_y, suppressed_point_count = fill_deep_inverse_valleys(
        substrate_y, smoothing_radius, inverse_artifact_threshold
    )
    # A rolling lower envelope stays below every target point in its window.
    # Unlike pointwise clipping, an isolated noisy dip therefore lowers the
    # surrounding background together instead of creating a reverse peak.
    smooth_target_envelope = moving_minimum(target_y, smoothing_radius)
    low_shift_envelope = moving_minimum(target_y, max(3, smoothing_radius // 4))
    substrate_y = [
        min(max(0.0, envelope * 0.96), max(0.0, target))
        if 50 <= x_value < 150 else value
        for x_value, value, envelope, target in zip(
            target_x, substrate_y, low_shift_envelope, target_y
        )
    ]

    def capped_substrate(values):
        return [
            min(value, max(0.0, target))
            for value, target in zip(values, target_y)
        ]
    target_noise = estimate_spectral_noise(target_y)
    smoothing_passes = 0
    # Reference measurements can be noisier than the target. Smooth only as
    # much as needed to meet the target's measured high-frequency noise floor.
    while estimate_spectral_noise(substrate_y) > target_noise and smoothing_passes < 20:
        substrate_y = smooth_three_point(substrate_y)
        smoothing_passes += 1
    si_amplitude_scale = 1.0
    modeled_si_peak = calculate_peak_metrics(target_x, substrate_y, 500, 540)
    if target_si_peak and modeled_si_peak and modeled_si_peak["height"] > 0:
        si_amplitude_scale = max(0.25, min(4.0, target_si_peak["height"] / modeled_si_peak["height"]))
        # Keep the learned, denoised Si shoulders unchanged. The exact maximum
        # is restored later with a narrow apex correction; broad scaling here
        # would force the intensity cap to copy target noise across this band.
    # A physically subtractable component cannot be more intense than the
    # measured target at the same Raman shift.
    substrate_y = capped_substrate(substrate_y)
    protected_regions = []
    assignments = None
    target_material = canonical_material_key(target_record.get("material_system"))
    if "mos2" in target_material:
        assignments = analyze_raman_spectrum(
            target_x, target_y, target_record.get("material_system")
        )["peaks"]
        for mode, peak in (("MoS2 E mode", assignments["E_mode"]), ("MoS2 A1 mode", assignments["A1_mode"])):
            if not peak:
                continue
            half_width = max(8.0, min(14.0, 1.75 * (peak.get("fwhm") or 6.0)))
            lower, upper = peak["position"] - half_width, peak["position"] + half_width
            inside = [index for index, value in enumerate(target_x) if lower <= value <= upper]
            if not inside or inside[0] == 0 or inside[-1] >= len(target_x) - 1:
                continue
            left_index, right_index = inside[0] - 1, inside[-1] + 1
            left_x, right_x = target_x[left_index], target_x[right_index]
            left_y, right_y = substrate_y[left_index], substrate_y[right_index]
            for index in inside:
                ratio = (target_x[index] - left_x) / (right_x - left_x)
                substrate_y[index] = left_y + ratio * (right_y - left_y)
            protected_regions.append({
                "assignment": mode,
                "center": peak["position"],
                "lower": round(lower, 4),
                "upper": round(upper, 4),
            })
    substrate_y = capped_substrate(substrate_y)
    # Amplitude matching plus pointwise clipping can inherit a little target
    # noise. Finish with constrained smoothing until the learned curve is no
    # noisier than the measurement, reapplying the intensity cap each pass.
    while estimate_spectral_noise(substrate_y) > target_noise and smoothing_passes < 40:
        substrate_y = smooth_three_point(substrate_y)
        substrate_y = capped_substrate(substrate_y)
        smoothing_passes += 1
    # Maintain clearance from the noisy pointwise cap. The Si apex is restored
    # exactly below, but the shoulders remain a learned, smooth curve.
    substrate_y = [value * 0.98 for value in substrate_y]
    quiet_tail = {"applied": False, "start": 1100.0, "meaningful_peak_count": 0}
    tail_indexes = [index for index, value in enumerate(target_x) if value >= 1100]
    if len(tail_indexes) >= 15:
        tail_target = [target_y[index] for index in tail_indexes]
        tail_noise = estimate_spectral_noise(tail_target)
        meaningful_threshold = max(8 * tail_noise, 0.005 * (max(target_y) - min(target_y)))
        meaningful_peaks = []
        for local_index in range(5, len(tail_target) - 5):
            neighbors = tail_target[local_index - 5:local_index] + tail_target[local_index + 1:local_index + 6]
            prominence = tail_target[local_index] - statistics.median(neighbors)
            if (tail_target[local_index] >= tail_target[local_index - 1]
                    and tail_target[local_index] > tail_target[local_index + 1]
                    and prominence >= meaningful_threshold):
                half_level = tail_target[local_index] - prominence / 2
                left = local_index
                right = local_index
                while left > 0 and tail_target[left - 1] >= half_level:
                    left -= 1
                while right + 1 < len(tail_target) and tail_target[right + 1] >= half_level:
                    right += 1
                if right - left + 1 >= 3:
                    meaningful_peaks.append(tail_indexes[local_index])
        quiet_tail["meaningful_peak_count"] = len(meaningful_peaks)
        if not meaningful_peaks:
            tail_substrate = [substrate_y[index] for index in tail_indexes]
            tail_radius = max(5, min(30, len(tail_substrate) // 30))
            for _pass in range(5):
                tail_substrate = moving_average(tail_substrate, tail_radius)
            tail_x = [target_x[index] for index in tail_indexes]
            mean_x = statistics.mean(tail_x)
            mean_y = statistics.mean(tail_substrate)
            denominator = sum((value - mean_x) ** 2 for value in tail_x) or 1.0
            slope = sum(
                (x_value - mean_x) * (y_value - mean_y)
                for x_value, y_value in zip(tail_x, tail_substrate)
            ) / denominator
            preceding_index = tail_indexes[0] - 1
            anchor_y = substrate_y[preceding_index] if preceding_index >= 0 else mean_y
            intercept = anchor_y - slope * tail_x[0]
            # A confirmed featureless tail is represented by its broad linear
            # trend, not by a moving average that can retain slow reference
            # undulations.
            tail_substrate = [max(0.0, intercept + slope * x_value) for x_value in tail_x]
            allowable_scales = [
                max(0.0, target_y[index]) / value
                for index, value in zip(tail_indexes, tail_substrate) if value > 0
            ]
            limiting_scale = min(allowable_scales, default=1.0)
            tail_scale = min(1.0, limiting_scale * 0.98) if limiting_scale < 1.0 else 1.0
            for local_index, index in enumerate(tail_indexes):
                substrate_y[index] = tail_substrate[local_index] * tail_scale
            quiet_tail.update({
                "applied": True,
                "method": "constrained linear quiet-tail baseline",
                "slope": round(slope * tail_scale, 8),
                "target_noise": round(tail_noise, 6),
                "substrate_noise": round(estimate_spectral_noise([substrate_y[index] for index in tail_indexes]), 6),
            })
    # Interior quiet-range smoothing is intentionally disabled until both the
    # target and every learned reference independently confirm a featureless
    # interval. Target-only classification flattened valid substrate patterns.
    quiet_ranges = []
    # Restore the exact measured Si maximum with a smooth local correction.
    # This is applied after denoising, so matching the peak does not amplify
    # reference noise across the rest of the spectrum.
    si_indexes = [index for index, value in enumerate(target_x) if 500 <= value <= 540]
    if target_si_peak and si_indexes:
        target_max_index = max(si_indexes, key=target_y.__getitem__)
        missing_height = max(0.0, target_y[target_max_index] - substrate_y[target_max_index])
        # Keep this apex correction narrower than the learned peak profile;
        # otherwise pointwise clipping would copy target noise across the
        # entire Si band into the displayed reference.
        sigma = max(0.75, (target_si_peak.get("fwhm") or 8.0) / 6.0)
        if missing_height:
            center = target_x[target_max_index]
            substrate_y = [
                min(
                    max(0.0, target),
                    value + missing_height * (2.718281828459045 ** (-0.5 * ((x_value - center) / sigma) ** 2)),
                )
                for x_value, value, target in zip(target_x, substrate_y, target_y)
            ]
    learned_feedback = [
        item for item in database.list_substrate_peak_feedback(target_material)
        if item["action"] == "remove"
    ]
    for item in learned_feedback:
        center = float(item["center"])
        half_width = max(2.0, float(item["half_width"]))
        if 500 <= center <= 550:
            half_width = max(half_width, 25.0)
        indexes = [index for index, value in enumerate(target_x) if abs(value - center) <= half_width]
        if not indexes:
            continue
        peak_index = max(indexes, key=lambda index: target_y[index] - substrate_y[index])
        missing_height = max(0.0, target_y[peak_index] - substrate_y[peak_index])
        sigma = max(0.75, half_width / 2.5)
        substrate_y = [
            min(max(0.0, target), value + missing_height * (2.718281828459045 ** (-0.5 * ((x_value - target_x[peak_index]) / sigma) ** 2)))
            for x_value, value, target in zip(target_x, substrate_y, target_y)
        ]

    # For a target identified as MoS2/SiO2/Si, finish with a conservative
    # material-isolation pass.  A free residual after reference subtraction
    # can contain baseline drift, reference mismatch and instrument noise and
    # must not be presented as MoS2.  Reconstruct only the two assigned MoS2
    # bands above a local shoulder-to-shoulder baseline; everything else stays
    # in the removable component.  This is deliberately target-specific.
    isolation = {"applied": False, "mode": "reference residual"}
    if assignments and assignments.get("E_mode") and assignments.get("A1_mode"):
        isolated_y = [0.0] * len(target_y)
        isolated_regions = []
        for mode, peak in (("E_mode", assignments["E_mode"]), ("A1_mode", assignments["A1_mode"])):
            half_width = max(9.0, min(22.0, 3.0 * (peak.get("fwhm") or 6.0)))
            indexes = [index for index, value in enumerate(target_x) if abs(value - peak["position"]) <= half_width]
            if len(indexes) < 5 or indexes[0] == 0 or indexes[-1] >= len(target_x) - 1:
                continue
            left_index, right_index = indexes[0] - 1, indexes[-1] + 1
            fitted_height = max(0.0, float(peak.get("height") or 0.0))
            fitted_width = max(1.0, float(peak.get("fwhm") or 6.0))
            local_signal = []
            for index in indexes:
                offset = (target_x[index] - peak["position"]) / fitted_width
                gaussian = 2.718281828459045 ** (-4.0 * 0.6931471805599453 * offset * offset)
                lorentzian = 1.0 / (1.0 + 4.0 * offset * offset)
                # A pseudo-Voigt component is smooth and noise-free while
                # retaining realistic Raman peak wings.
                local_signal.append(fitted_height * (0.5 * gaussian + 0.5 * lorentzian))
            taper_points = max(2, min(8, len(indexes) // 5))
            for local_index, index in enumerate(indexes):
                edge_distance = min(local_index + 1, len(indexes) - local_index)
                taper = min(1.0, edge_distance / taper_points)
                isolated_y[index] = max(isolated_y[index], local_signal[local_index] * taper)
            isolated_regions.append({
                "assignment": mode,
                "center": round(peak["position"], 4),
                "lower": round(target_x[indexes[0]], 4),
                "upper": round(target_x[indexes[-1]], 4),
            })
        if isolated_regions:
            substrate_y = [
                max(0.0, target - isolated)
                for target, isolated in zip(target_y, isolated_y)
            ]
            isolation = {
                "applied": True,
                "mode": "MoS2-only constrained reconstruction",
                "regions": isolated_regions,
            }
    pre_enforcement_residual = [
        target - substrate for target, substrate in zip(target_y, substrate_y)
    ]
    substrate_only_validation = {
        "applied": False,
        "leave_one_out_reference_count": len(reference_records),
    }
    if target_material in SIO2_SI_MATERIAL_KEYS:
        residual_rms = (
            sum(value * value for value in pre_enforcement_residual)
            / len(pre_enforcement_residual)
        ) ** 0.5
        target_rms = (sum(value * value for value in target_y) / len(target_y)) ** 0.5 or 1.0
        relative_rms = residual_rms / target_rms
        # Evaluate generalization before applying the confirmed-substrate
        # zero-residual constraint below. Otherwise a copied target trace
        # would make a poor leave-one-out model look perfect.
        model_fit_relative_rms_tolerance = 0.12
        substrate_only_validation.update({
            "applied": True,
            "reason": "Confirmed substrate-only metadata assigns the complete measured trace to the substrate after leave-one-out model evaluation.",
            "pre_enforcement_residual_rms": round(residual_rms, 8),
            "pre_enforcement_residual_max_abs": round(
                max(map(abs, pre_enforcement_residual), default=0.0), 8
            ),
            "pre_enforcement_relative_rms": round(relative_rms, 8),
            "model_fit_relative_rms_tolerance": model_fit_relative_rms_tolerance,
            "model_fit_passed": relative_rms <= model_fit_relative_rms_tolerance,
            "final_zero_residual_tolerance": 1e-10,
        })
        substrate_y = target_y.copy()

    substrate_noise = estimate_spectral_noise(substrate_y)
    corrected_y = [value - substrate for value, substrate in zip(target_y, substrate_y)]
    if substrate_only_validation["applied"]:
        zero_tolerance = substrate_only_validation["final_zero_residual_tolerance"]
        substrate_only_validation["final_zero_residual_max_abs"] = round(
            max(map(abs, corrected_y), default=0.0), 12
        )
        substrate_only_validation["final_zero_residual_passed"] = all(
            abs(value) <= zero_tolerance for value in corrected_y
        )
    reviewed = database.list_substrate_peak_feedback()
    residual_peaks = []
    for index in range(4, len(corrected_y) - 4):
        x_value = target_x[index]
        if x_value < 150 or (not reliable_reference_signal[index] and not 500 <= x_value <= 560):
            continue
        if any(region["lower"] <= x_value <= region["upper"] for region in protected_regions):
            continue
        if any(item["source_file_id"] == file_id and abs(float(item["center"]) - x_value) <= 5 for item in reviewed):
            continue
        local_start = max(0, index - 40)
        local_end = min(len(target_y), index + 41)
        local_target = target_y[local_start:local_end]
        local_threshold = max(
            5 * estimate_spectral_noise(local_target),
            0.08 * (max(local_target) - min(local_target)),
        )
        local_floor = min(corrected_y[index - 4:index] + corrected_y[index + 1:index + 5])
        prominence = corrected_y[index] - local_floor
        if (corrected_y[index] >= corrected_y[index - 1]
                and corrected_y[index] > corrected_y[index + 1]
                and prominence >= local_threshold):
            residual_peaks.append({"position": round(x_value, 4), "prominence": round(prominence, 4), "half_width": 8.0})
    residual_peaks = sorted(residual_peaks, key=lambda peak: peak["prominence"], reverse=True)[:3]
    target_peak = max((target_centered[i] for i, x in enumerate(target_x) if 500 <= x <= 540), default=0)
    noise_values = [target_y[i] for i, x in enumerate(target_x) if 450 <= x < 490 or 550 < x <= 590]
    noise = statistics.pstdev(noise_values) if len(noise_values) > 1 else 1
    fit_error = sum((target_centered[index] - substrate_y[index]) ** 2 for index in anchors)
    target_energy = sum(target_centered[index] ** 2 for index in anchors) or 1.0
    fit_quality = max(0.0, 1.0 - (fit_error / target_energy) ** 0.5)
    signal_confidence = target_peak / (target_peak + 5 * (noise or 1))
    confidence = min(0.99, max(0.0, 0.65 * fit_quality + 0.35 * signal_confidence))
    coefficient_total = sum(coefficients)
    weights = [value / coefficient_total if coefficient_total else 0.0 for value in coefficients]

    def variability_summary(values):
        finite = sorted(value for value in values if value is not None and math.isfinite(value))
        if not finite:
            return {"count": 0, "median": None, "minimum": None, "maximum": None, "mad": None}
        median = statistics.median(finite)
        return {
            "count": len(finite),
            "median": round(median, 6),
            "minimum": round(finite[0], 6),
            "maximum": round(finite[-1], 6),
            "mad": round(statistics.median(abs(value - median) for value in finite), 6),
        }

    spacing = statistics.median(
        right - left for left, right in zip(target_x, target_x[1:])
    )
    candidate_indexes = [
        index for index in range(1, len(consensus) - 1)
        if target_x[index] >= 80
        and support_fraction[index] >= 0.6
        and consensus[index] >= 0.04 * consensus_peak
        and consensus[index] > consensus[index - 1]
        and consensus[index] >= consensus[index + 1]
    ]
    stable_indexes = []
    minimum_separation = max(6.0, 3 * spacing)
    for index in sorted(candidate_indexes, key=lambda item: consensus[item], reverse=True):
        if all(abs(target_x[index] - target_x[existing]) >= minimum_separation for existing in stable_indexes):
            stable_indexes.append(index)
        if len(stable_indexes) >= 12:
            break
    learned_peak_families = []
    for index in sorted(stable_indexes, key=lambda item: target_x[item]):
        half_window = max(5.0, 2 * spacing)
        region = [
            local_index for local_index, x_value in enumerate(target_x)
            if abs(x_value - target_x[index]) <= half_window
        ]
        positions, absolute_intensities, relative_intensities = [], [], []
        for absolute_profile, relative_profile in zip(references, basis):
            local_index = max(region, key=lambda item: relative_profile[item])
            if relative_profile[local_index] < 0.02 * consensus_peak:
                continue
            positions.append(target_x[local_index])
            absolute_intensities.append(absolute_profile[local_index])
            relative_intensities.append(relative_profile[local_index])
        learned_peak_families.append({
            "consensus_position": round(target_x[index], 6),
            "reference_support_fraction": round(len(positions) / len(references), 6),
            "position": variability_summary(positions),
            "absolute_intensity": variability_summary(absolute_intensities),
            "relative_to_si_peak": variability_summary(relative_intensities),
        })

    ensemble_material = {
        "model_version": SUBSTRATE_MODEL_VERSION,
        "references": sorted(
            ({"file_id": record["file_id"], "sha256": record["sha256"]}
             for record in reference_records),
            key=lambda item: item["file_id"],
        ),
    }
    reference_ensemble_id = hashlib.sha256(
        json.dumps(ensemble_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    reference_ensemble = {
        "ensemble_id": reference_ensemble_id,
        "reference_count": len(reference_records),
        "incremental_learning": "All active confirmed SiO2/Si references are re-evaluated for every target.",
        "silicon_peak_position": variability_summary(
            feature["silicon_peak_position"] for feature in reference_features
        ),
        "silicon_peak_absolute_height": variability_summary(
            feature["silicon_peak_height"] for feature in reference_features
        ),
        "silicon_peak_absolute_area": variability_summary(
            feature["silicon_peak_area"] for feature in reference_features
        ),
        "learned_peak_families": learned_peak_families,
    }
    return {"x": target_x, "substrate_y": substrate_y, "corrected_y": corrected_y,
            "raw_y": raw_target_y,
            "baseline_y": target_baseline,
            "baseline_corrected_y": baseline_corrected_y,
            "artifact_cleaned_y": target_y,
            "processing_pipeline": [
                {"order": 1, "stage": "rubber-band baseline detection and subtraction"},
                {"order": 2, "stage": "isolated cosmic-ray detection and subtraction",
                 "removed_point_count": len(target_cosmic_indexes),
                 "removed_positions": [round(target_x[index], 6) for index in target_cosmic_indexes]},
                {"order": 3, "stage": "target-adaptive SiO2/Si detection and subtraction"},
                {"order": 4, "stage": "MoS2 protected-peak isolation"},
            ],
            "x_units": units, "scale": round(coefficient_total, 6), "confidence": round(confidence, 4),
            "detected": confidence >= 0.6, "reference_files": reference_names,
            "model_version": SUBSTRATE_MODEL_VERSION,
            "reference_ensemble": reference_ensemble,
            "reference_features": reference_features,
            "reference_sources": [
                {
                    "file_id": record["file_id"],
                    "filename": record["original_filename"],
                    "sha256": record["sha256"],
                    "weight": round(weight, 6),
                    "alignment_shift": round(shift, 6),
                }
                for record, weight, shift in zip(reference_records, weights, reference_shifts)
            ],
            "reference_weights": {
                name: round(weight, 6) for name, weight in zip(reference_names, weights)
            },
            "reference_alignment_shifts": {
                name: round(shift, 6) for name, shift in zip(reference_names, reference_shifts)
            },
            "excluded_references": excluded_references,
            "local_adjustments": local_adjustments,
            "silicon_peak_match": {
                "target_position": target_si_peak["position"] if target_si_peak else None,
                "amplitude_scale": round(si_amplitude_scale, 6),
                "maximum_matched": bool(target_si_peak and si_indexes),
            },
            "protected_regions": protected_regions,
            "residual_peaks": residual_peaks,
            "learned_residual_count": len(learned_feedback),
            "material_isolation": isolation,
            "substrate_only_validation": substrate_only_validation,
            "quiet_tail": quiet_tail,
            "quiet_ranges": quiet_ranges,
            "noise": {
                "target": round(target_noise, 6),
                "substrate": round(substrate_noise, 6),
                "smoothing_passes": smoothing_passes,
            },
            "fit_point_count": len(anchors),
            "unsupported_structure_points_smoothed": suppressed_point_count,
            "reference_fit_method": "target-adaptive non-negative mixture of SiO2/Si reference spectra with material-peak protection",
            "method": (
                "leave-one-out SiO2/Si fit with confirmed substrate-only zero-residual invariant"
                if substrate_only_validation["applied"] else
                "target-adaptive SiO2/Si reference fit followed by a fitted MoS2-only reconstruction"
                if isolation["applied"] else
                "target-adaptive non-negative mixture of SiO2/Si reference spectra with material-peak protection"
            )}


@app.post("/files/{file_id}/substrate-residual-feedback")
def save_substrate_residual_feedback(file_id: str, body: dict = Body(...)):
    record = database.get_imported_file(file_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "file_not_found", "message": "Imported file not found."})
    action = str(body.get("action") or "").lower()
    if action not in {"keep", "remove", "command"}:
        raise validation_error("invalid_feedback_action", "Choose keep, remove, or command.")
    try:
        center = float(body.get("center"))
        requested_half_width = float(body.get("half_width", 8))
    except (TypeError, ValueError) as error:
        raise validation_error("invalid_residual_peak", "A valid residual peak position is required.") from error
    if not math.isfinite(center) or not math.isfinite(requested_half_width):
        raise validation_error("invalid_residual_peak", "Residual peak position and width must be finite numbers.")
    spectrum = retrieve_imported_spectrum(file_id)
    if not spectrum["x"][0] <= center <= spectrum["x"][-1]:
        raise validation_error(
            "residual_peak_out_of_range",
            "Residual peak position must lie within the imported spectrum range.",
        )
    half_width = max(2.0, min(50.0, requested_half_width))
    material = canonical_material_key(record.get("material_system"))
    feedback = {
        "feedback_id": str(uuid4()), "source_file_id": file_id, "material_system": material,
        "center": center, "half_width": half_width, "action": action,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with database.connect_database() as connection:
        database.create_substrate_peak_feedback(connection, feedback)
    prompt = (
        f"Review the residual Raman peak near {center:.2f} cm-1 in {record['original_filename']} after "
        "target-adaptive SiO2/Si subtraction. Determine whether it is substrate, sample, or processing artifact, "
        "and propose a scientifically justified processing change without modifying the raw data."
    )
    command = str(body.get("command") or "").strip()[:2000]
    if command:
        prompt += f"\n\nUser instruction: {command}"
    return {"saved": True, "action": action, "learned": action == "remove", "prompt": prompt}


MATERIAL_SPECTRAL_REFERENCES = (
    {
        "material_system": "MoS₂",
        "filename_pattern": r"(?:^|[^a-z0-9])mo\s*s[_-]?2(?:[^a-z0-9]|$)",
        "peaks": ((385.0, 8.0), (406.0, 8.0)),
        "source": "https://www.nature.com/articles/s41699-020-0138-y",
    },
    {
        "material_system": "Graphene / graphitic carbon",
        "filename_pattern": r"(?:graphene|graphite|\brgo\b)",
        "peaks": ((1350.0, 35.0), (1580.0, 30.0)),
        "source": "https://www.mdpi.com/2073-4352/11/6/660",
    },
    {
        "material_system": "Crystalline silicon",
        "filename_pattern": r"(?:silicon|(?:^|[^a-z0-9])si(?:[^a-z0-9]|$))",
        "peaks": ((520.7, 8.0),),
        "source": "https://pmc.ncbi.nlm.nih.gov/articles/PMC11636025/",
    },
)


def propose_material_system(filename: str, content: bytes, relative_path: str | None = None) -> dict | None:
    """Propose, but never assign, a material from filename and Raman peaks."""
    path_hint = (relative_path or filename).replace("\\", "/")
    x_values, y_values, _ = parse_spectrum_xy(content)
    local_peaks = []
    if len(y_values) >= 3:
        y_span = max(y_values) - min(y_values)
        threshold = min(y_values) + y_span * 0.15
        local_peaks = [
            x_values[index]
            for index in range(1, len(y_values) - 1)
            if y_values[index] >= threshold
            and y_values[index] > y_values[index - 1]
            and y_values[index] >= y_values[index + 1]
        ]
    candidates = []
    for reference in MATERIAL_SPECTRAL_REFERENCES:
        filename_match = bool(re.search(reference["filename_pattern"], path_hint, re.IGNORECASE))
        matched = [
            min(local_peaks, key=lambda peak: abs(peak - target))
            for target, tolerance in reference["peaks"]
            if local_peaks and min(abs(peak - target) for peak in local_peaks) <= tolerance
        ]
        spectral_match = len(matched) == len(reference["peaks"])
        if not filename_match and not spectral_match:
            continue
        confidence = 0.95 if filename_match and spectral_match else (0.84 if spectral_match else 0.68)
        evidence = []
        if filename_match:
            evidence.append("material name matched the filename or folder path")
        if spectral_match:
            evidence.append("matched Raman peaks at " + ", ".join(f"{peak:.1f} cm⁻¹" for peak in matched))
        candidates.append({
            "material_system": reference["material_system"],
            "confidence": confidence,
            "evidence": evidence,
            "sources": [reference["source"]],
            "requires_confirmation": True,
        })
    return max(candidates, key=lambda candidate: candidate["confidence"], default=None)


def calculate_peak_metrics(
    x_values: list[float],
    y_values: list[float],
    lower: float,
    upper: float,
) -> dict | None:
    indexes = [index for index, x in enumerate(x_values) if lower <= x <= upper]
    if len(indexes) < 5:
        return None
    xs = [x_values[index] for index in indexes]
    ys = [y_values[index] for index in indexes]
    edge_count = max(2, min(5, len(xs) // 5))
    left_x = statistics.median(xs[:edge_count])
    right_x = statistics.median(xs[-edge_count:])
    left_y = statistics.median(ys[:edge_count])
    right_y = statistics.median(ys[-edge_count:])
    x_span = right_x - left_x
    baseline = [
        left_y + (right_y - left_y) * (x_value - left_x) / x_span
        for x_value in xs
    ] if x_span else [left_y] * len(ys)
    corrected = [value - base for value, base in zip(ys, baseline)]
    interior_indexes = range(edge_count, len(corrected) - edge_count)
    peak_index = max(interior_indexes, key=corrected.__getitem__)
    height = corrected[peak_index]
    if height <= 0:
        return None
    position = xs[peak_index]
    if 0 < peak_index < len(xs) - 1:
        x1, x2, x3 = xs[peak_index - 1:peak_index + 2]
        y1, y2, y3 = corrected[peak_index - 1:peak_index + 2]
        denominator = (x1 - x2) * (x1 - x3) * (x2 - x3)
        if denominator:
            quadratic = (x3 * (y2 - y1) + x2 * (y1 - y3) + x1 * (y3 - y2)) / denominator
            linear = (
                x3 * x3 * (y1 - y2)
                + x2 * x2 * (y3 - y1)
                + x1 * x1 * (y2 - y3)
            ) / denominator
            vertex = -linear / (2 * quadratic) if quadratic else position
            if quadratic < 0 and x1 <= vertex <= x3:
                position = vertex
    half_height = height / 2
    left_x = None
    for index in range(peak_index, 0, -1):
        if corrected[index - 1] <= half_height <= corrected[index]:
            span = corrected[index] - corrected[index - 1]
            ratio = (half_height - corrected[index - 1]) / span if span else 0.0
            left_x = xs[index - 1] + ratio * (xs[index] - xs[index - 1])
            break
    right_x = None
    for index in range(peak_index, len(xs) - 1):
        if corrected[index] >= half_height >= corrected[index + 1]:
            span = corrected[index] - corrected[index + 1]
            ratio = (corrected[index] - half_height) / span if span else 0.0
            right_x = xs[index] + ratio * (xs[index + 1] - xs[index])
            break
    fwhm = right_x - left_x if left_x is not None and right_x is not None else None
    area = sum(
        max(0, corrected[index] + corrected[index + 1]) / 2 * (xs[index + 1] - xs[index])
        for index in range(len(xs) - 1)
    )
    noise = estimate_spectral_noise(corrected)
    snr = height / noise if noise > 0 else None
    positive_spacings = [right - left for left, right in zip(xs, xs[1:]) if right > left]
    sampling_uncertainty = statistics.median(positive_spacings) / (12 ** 0.5) if positive_spacings else 0.0
    fit_uncertainty = fwhm / (2.355 * snr) if fwhm is not None and snr else 0.0
    position_uncertainty = (sampling_uncertainty ** 2 + fit_uncertainty ** 2) ** 0.5
    return {
        "position": round(position, 4),
        "height": round(height, 4),
        "area": round(area, 4),
        "fwhm": round(fwhm, 4) if fwhm is not None else None,
        "snr": round(snr, 2) if snr is not None else None,
        "position_uncertainty": round(position_uncertainty, 4),
    }


def analyze_raman_spectrum(
    x_values: list[float],
    y_values: list[float],
    material_system: str | None = None,
) -> dict:
    spacings = [x_values[index + 1] - x_values[index] for index in range(len(x_values) - 1)]
    median_spacing = statistics.median(spacings)
    spacing_deviation = statistics.median(abs(value - median_spacing) for value in spacings)
    maximum = max(y_values)
    maximum_count = sum(value == maximum for value in y_values)
    maximum_run = 0
    current_maximum_run = 0
    for value in y_values:
        current_maximum_run = current_maximum_run + 1 if value == maximum else 0
        maximum_run = max(maximum_run, current_maximum_run)
    saturation_fraction = maximum_run / len(y_values)
    saturation_detected = maximum_run >= 3 and saturation_fraction > 0.005
    warnings = []
    if median_spacing <= 0:
        warnings.append("X values are not strictly increasing.")
    if median_spacing and spacing_deviation / abs(median_spacing) > 0.01:
        warnings.append("X-axis spacing is irregular.")
    if saturation_detected:
        warnings.append("A consecutive flat-topped maximum may indicate detector saturation.")

    normalized_material = canonical_material_key(material_system)
    material_is_unspecified = not normalized_material
    mos2_context = material_is_unspecified or "mos2" in normalized_material
    e_mode = calculate_peak_metrics(x_values, y_values, 365, 395) if mos2_context else None
    a_mode = calculate_peak_metrics(x_values, y_values, 395, 425) if mos2_context else None
    separation = None
    layer_estimate = None
    interpretation_reasons = []
    if not mos2_context:
        interpretation_reasons.append(
            "Material metadata identifies a non-MoS2 system; MoS2 mode assignment was not attempted."
        )
    elif not e_mode or not a_mode:
        interpretation_reasons.append("Both principal MoS2 modes were not detected.")
    for label, peak in (("E mode", e_mode), ("A1 mode", a_mode)):
        if peak and (peak.get("snr") is None or peak["snr"] < 5):
            interpretation_reasons.append(f"{label} SNR is below 5.")
        if peak and peak.get("fwhm") is None:
            interpretation_reasons.append(f"{label} FWHM could not be measured.")
        elif peak and median_spacing > 0 and peak["fwhm"] < 3 * median_spacing:
            interpretation_reasons.append(f"{label} is sampled by fewer than about three points across its FWHM.")
    blocking_quality_warning = median_spacing <= 0 or saturation_detected
    if blocking_quality_warning:
        interpretation_reasons.append("Spectrum quality warnings require review.")
    separation_uncertainty = None
    if e_mode and a_mode:
        separation = round(a_mode["position"] - e_mode["position"], 4)
        separation_uncertainty = round((
            e_mode["position_uncertainty"] ** 2 + a_mode["position_uncertainty"] ** 2
        ) ** 0.5, 4)
        lower_bound = separation - separation_uncertainty
        upper_bound = separation + separation_uncertainty
        layer_categories = (
            (18.0, 21.0, "monolayer-like"),
            (21.0, 25.0, "few-layer-like"),
            (25.0, 27.5, "bulk/multilayer-like"),
        )
        matching_categories = [
            label for lower, upper, label in layer_categories
            if lower_bound >= lower and upper_bound < upper
        ]
        if not matching_categories:
            interpretation_reasons.append(
                "Peak-separation uncertainty crosses a layer-category boundary or lies outside the supported 18–27.5 cm-1 range."
            )
        elif not interpretation_reasons:
            layer_estimate = matching_categories[0]
    elif mos2_context:
        warnings.append("Both principal MoS2 modes could not be measured reliably.")

    interpretation_eligible = not interpretation_reasons

    minimum_snr = min(
        [peak["snr"] for peak in (e_mode, a_mode) if peak and peak.get("snr") is not None],
        default=0,
    )
    if not mos2_context:
        # Absence of MoS2 peaks is not a quality failure for a different
        # material; retain only the material-independent spectrum QC result.
        quality_badge = "Review" if warnings else "Good"
    else:
        quality_badge = "Good"
        if warnings or minimum_snr < 10 or not interpretation_eligible:
            quality_badge = "Review"
        if not e_mode or not a_mode or minimum_snr < 3:
            quality_badge = "Poor"

    return {
        "peaks": {"E_mode": e_mode, "A1_mode": a_mode},
        "material_context": material_system,
        "mode_assignment_status": (
            "confirmed MoS2 context"
            if not material_is_unspecified and mos2_context else
            "candidate MoS2 assignment; material unspecified"
            if material_is_unspecified else
            "not attempted for non-MoS2 material"
        ),
        "peak_separation": separation,
        "peak_separation_uncertainty": separation_uncertainty,
        "layer_estimate": layer_estimate,
        "interpretation": {
            "eligible": interpretation_eligible,
            "reasons": interpretation_reasons,
        },
        "uncertainty": {
            "method": "quadrature of X-grid quantization and FWHM/(2.355*SNR)",
            "includes_calibration_systematics": False,
            "note": "Reported uncertainties are approximate statistical/sampling estimates; calibration and model-choice uncertainty are not included.",
        },
        "quality": {
            "point_count": len(x_values),
            "median_spacing": round(median_spacing, 6),
            "spacing_mad": round(spacing_deviation, 6),
            "intensity_min": min(y_values),
            "intensity_max": maximum,
            "maximum_plateau_points": maximum_run,
            "badge": quality_badge,
            "warnings": warnings,
        },
        "interpretation_warning": (
            "Layer labeling is reported only when both MoS2 modes pass SNR, width, sampling, and spectrum-quality checks. "
            "Layer, strain, and doping conclusions still require calibration, uncertainty review, and suitable references."
        ),
    }


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
        "version": APP_VERSION,
    }


@app.get("/health")
def health_check():
    try:
        with database.connect_database() as connection:
            database_integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            import_count = connection.execute("SELECT COUNT(*) FROM imported_files").fetchone()[0]
            analysis_run_count = connection.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0]
            invalid_analysis_provenance = connection.execute(
                """
                SELECT COUNT(*)
                FROM analysis_runs
                LEFT JOIN imported_files USING (file_id)
                WHERE imported_files.file_id IS NULL
                   OR analysis_runs.raw_sha256 <> imported_files.sha256
                """
            ).fetchone()[0]
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
            analysis_rows = [
                dict(row) for row in connection.execute(
                    """SELECT run_id, file_id, raw_sha256, derived_sha256,
                              derived_trace, processing_config, result,
                              app_version, created_at
                       FROM analysis_runs"""
                ).fetchall()
            ]
    except sqlite3.Error as error:
        logger.exception("Health check could not query the database")
        raise HTTPException(
            status_code=503,
            detail={"code": "database_unavailable", "message": "The metadata database is unavailable."},
        ) from error
    if database_integrity != "ok":
        raise HTTPException(
            status_code=503,
            detail={"code": "database_integrity_failed", "message": "The metadata database integrity check failed."},
        )
    if invalid_analysis_provenance:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "analysis_provenance_failed",
                "message": "One or more saved analysis runs no longer match their source records.",
                "invalid_run_count": invalid_analysis_provenance,
            },
        )
    if invalid_json_records:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "stored_json_invalid",
                "message": "One or more stored metadata or configuration records contain invalid structured data.",
                "invalid_record_count": invalid_json_records,
            },
        )
    invalid_derived_run_count = 0
    legacy_unverifiable_run_count = 0
    for run in analysis_rows:
        try:
            validated_run = deserialize_analysis_run(run)
        except database.StoredDataIntegrityError:
            invalid_derived_run_count += 1
        else:
            if validated_run["derived_trace_verification"] == "unavailable_legacy":
                legacy_unverifiable_run_count += 1
    if invalid_derived_run_count:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "analysis_run_integrity_failed",
                "message": "One or more saved analysis runs contain inconsistent provenance or derived-trace data.",
                "invalid_run_count": invalid_derived_run_count,
                "legacy_unverifiable_run_count": legacy_unverifiable_run_count,
            },
        )
    return {
        "status": "healthy",
        "app_version": APP_VERSION,
        "database_integrity": database_integrity,
        "import_record_count": import_count,
        "analysis_run_count": analysis_run_count,
        "analysis_provenance": "ok",
        "analysis_trace_integrity": "verified" if not legacy_unverifiable_run_count else "legacy_unverifiable",
        "legacy_unverifiable_analysis_run_count": legacy_unverifiable_run_count,
        "structured_data_integrity": "ok",
        "raw_storage_available": RAW_DATA_DIR.is_dir() or RAW_DATA_DIR.parent.is_dir(),
    }


@app.get("/upload", response_class=FileResponse)
def upload_interface():
    return FileResponse(UPLOAD_INTERFACE_PATH)


@app.get("/files")
def list_imported_files():
    return database.list_imported_files()


@app.get("/operators")
def list_operators():
    return database.list_operators()


@app.post("/files/archive-batch")
def archive_imported_files(body: dict = Body(...)):
    file_ids = body.get("file_ids")
    if not isinstance(file_ids, list) or not file_ids:
        raise validation_error("missing_file_ids", "Select at least one imported file.")
    if any(
        not isinstance(file_id, str)
        or not file_id.strip()
        or len(file_id.strip()) > 100
        for file_id in file_ids
    ):
        raise validation_error(
            "invalid_file_ids",
            "Every imported-file ID must be non-empty text up to 100 characters.",
        )
    unique_ids = list(dict.fromkeys(file_id.strip() for file_id in file_ids))
    if not unique_ids or len(unique_ids) > 500:
        raise validation_error("invalid_file_ids", "Select between 1 and 500 imported files.")
    with database.connect_database() as connection:
        archived_count = database.archive_imported_files(
            connection, unique_ids, datetime.now(timezone.utc).isoformat()
        )
    return {"archived_count": archived_count, "requested_count": len(unique_ids)}


@app.get("/samples")
def list_samples(q: str | None = None):
    if q and q.strip():
        return database.search_samples(q.strip())
    return database.list_samples()


@app.post("/samples", status_code=201)
def create_sample(sample: dict = Body(...)):
    # expect at minimum {"sample_id": "T12-01"}, optional material, substrate, project, notes
    sample_id = normalize_required_metadata(sample.get("sample_id"), "Sample ID", 100)
    record = {
        "sample_id": sample_id,
        "material": normalize_optional_metadata(sample.get("material"), "Material", 200),
        "substrate": normalize_optional_metadata(sample.get("substrate"), "Substrate", 200),
        "project": normalize_optional_metadata(sample.get("project"), "Project", 200),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "notes": normalize_optional_metadata(sample.get("notes"), "Notes", 2000),
    }
    try:
        with database.connect_database() as connection:
            database.create_sample(connection, record)
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail={"code": "sample_exists", "message": "A sample with that ID already exists."},
        )
    return record


@app.get("/presets")
def list_presets():
    return database.list_presets()


@app.post("/presets", status_code=201)
def create_preset(preset: dict = Body(...)):
    # expect {name: str, config: dict}
    name = preset.get("name")
    config = preset.get("config")
    normalized_name = normalize_required_metadata(name, "Preset name", 100)
    serialized_config = serialize_config_object(config, "preset_config")
    record = {
        "preset_id": str(uuid4()),
        "name": normalized_name,
        "config": serialized_config,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with database.connect_database() as connection:
            database.create_preset(connection, record)
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail={"code": "preset_exists", "message": "A preset with that ID already exists."},
        )
    return record


@app.get("/presets/{preset_id}")
def get_preset(preset_id: str):
    preset = database.get_preset(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail={"code": "preset_not_found", "message": "Preset not found."})
    return preset


@app.get("/analysis/recipes")
def list_analysis_recipes():
    return [
        {**recipe, "config": parse_stored_json_object(recipe["config"], "analysis recipe configuration")}
        for recipe in database.list_analysis_recipes()
    ]


@app.post("/analysis/recipes", status_code=201)
def create_analysis_recipe(body: dict = Body(...)):
    name = normalize_required_metadata(body.get("name"), "Recipe name", 100)
    config = body.get("config")
    serialized_config = serialize_config_object(config, "recipe_config")
    recipe = {
        "recipe_id": str(uuid4()),
        "name": name,
        "config": serialized_config,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with database.connect_database() as connection:
        database.create_analysis_recipe(connection, recipe)
    return {**recipe, "config": config}


@app.put("/presets/{preset_id}")
def update_preset(preset_id: str, body: dict = Body(...)):
    name = body.get("name")
    config = body.get("config")
    normalized_name = normalize_required_metadata(name, "Preset name", 100)
    serialized_config = serialize_config_object(config, "preset_config")

    record = database.get_preset(preset_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "preset_not_found", "message": "Preset not found."})

    try:
        with database.connect_database() as connection:
            database.update_preset(connection, preset_id, normalized_name, serialized_config)
    except sqlite3.Error:
        raise HTTPException(status_code=500, detail={"code": "preset_update_failed", "message": "Failed to update preset."})

    return {"preset_id": preset_id, "name": normalized_name, "config": config}


@app.post("/files/inspect")
async def inspect_file(
    file: UploadFile = File(...),
    relative_path: str | None = Form(None),
):
    # Read a limited preview of the file and attempt simple format detection
    max_preview = 200000
    content = await file.read(max_preview)
    # Starlette's UploadFile.seek accepts only an offset and does not return a
    # reliable size. Prefer its recorded multipart size, falling back to the
    # preview length for non-standard UploadFile implementations.
    size = file.size if isinstance(file.size, int) and file.size >= 0 else len(content)
    await file.seek(0)

    filename = file.filename or ""
    lower = filename.lower()
    result = {
        "filename": filename,
        "size": size,
        "suggested_metadata": extract_metadata_suggestions(
            filename,
            content,
            relative_path,
        ),
        "material_system_proposal": propose_material_system(
            filename, content, relative_path
        ),
    }
    # quick heuristics
    if lower.endswith('.csv') or (b',' in content and b'\n' in content[:1024]):
        try:
            text = content.decode('utf-8', errors='replace')
            lines = text.strip().splitlines()[:10]
            table = [line.split(',') for line in lines]
            result.update({"detected_format": "csv", "preview_lines": lines, "rows_preview": table})
            return result
        except Exception:
            pass
    if lower.endswith('.txt') or lower.endswith('.log'):
        text = content.decode('utf-8', errors='replace')
        lines = text.strip().splitlines()[:20]
        result.update({"detected_format": "text", "preview_lines": lines})
        return result
    if lower.endswith('.wdf'):
        result.update({"detected_format": "wdf", "note": "WDF binary detected; consider previewing on the client or using a parser."})
        return result

    # fallback: try to decode as text
    try:
        text = content.decode('utf-8')
        lines = text.strip().splitlines()[:20]
        result.update({"detected_format": "text","preview_lines": lines})
        return result
    except Exception:
        result.update({"detected_format": "binary", "note": "Binary file; preview unavailable."})
        return result


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


@app.get("/files/{file_id}/metadata-suggestions")
def retrieve_imported_file_metadata_suggestions(file_id: str):
    record = database.get_imported_file(file_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "file_not_found", "message": "Imported file not found."},
        )
    storage_path = resolve_stored_file(record)
    with storage_path.open("rb") as imported_file:
        content = imported_file.read(200000)
    suggestions = extract_metadata_suggestions(record["original_filename"], content)
    return {
        "suggested_metadata": suggestions,
        "material_system_proposal": propose_material_system(
            record["original_filename"], content
        ),
    }


@app.patch("/files/{file_id}")
def update_imported_file(file_id: str, body: dict = Body(...)):
    record = database.get_imported_file(file_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "file_not_found", "message": "Imported file not found."})
    nested_details = body.get("experimental_details", {})
    if not isinstance(nested_details, dict):
        raise validation_error("invalid_metadata", "Experimental details must be an object.")
    unknown_details = set(nested_details) - set(EXPERIMENTAL_METADATA_FIELDS)
    if unknown_details:
        raise validation_error(
            "unknown_experimental_detail",
            "Unknown experimental detail fields: " + ", ".join(sorted(str(item) for item in unknown_details)),
        )
    experimental_details = {}
    for field_name in EXPERIMENTAL_METADATA_FIELDS:
        raw_value = body.get(field_name, nested_details.get(field_name))
        experimental_details[field_name] = normalize_optional_metadata(
            raw_value,
            field_name.replace("_", " ").title(),
            200,
        )
    metadata = {
        "technique": normalize_required_metadata(body.get("technique"), "Technique", 100),
        "sample_id": normalize_required_metadata(body.get("sample_id"), "Sample ID", 100),
        "measurement_date": normalize_measurement_date(body.get("measurement_date")),
        "instrument": normalize_optional_metadata(body.get("instrument"), "Instrument", 200),
        "operator": normalize_optional_metadata(body.get("operator"), "Operator", 100),
        "notes": normalize_optional_metadata(body.get("notes"), "Notes", 2000),
        "material_system": normalize_optional_metadata(
            body.get("material_system"), "Material system", 200
        ),
        "experimental_details": experimental_details,
    }
    with database.connect_database() as connection:
        database.update_imported_file_metadata(connection, file_id, metadata)
    return database.get_imported_file(file_id)


@app.delete("/files/{file_id}", status_code=204)
def archive_imported_file(file_id: str):
    if database.get_imported_file(file_id) is None:
        raise HTTPException(status_code=404, detail={"code": "file_not_found", "message": "Imported file not found."})
    with database.connect_database() as connection:
        database.archive_imported_file(connection, file_id, datetime.now(timezone.utc).isoformat())
    return None


@app.get("/files/{file_id}/spectrum")
def retrieve_imported_spectrum(file_id: str):
    record = database.get_imported_file(file_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "file_not_found", "message": "Imported file not found."},
        )

    content = read_verified_stored_file(record)
    x_values, y_values, x_units = parse_spectrum_xy(content)
    if len(x_values) < 2:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "spectrum_unavailable",
                "message": "No XY spectrum data was found in this file.",
            },
        )

    return {
        "file_id": file_id,
        "filename": record["original_filename"],
        "x": x_values,
        "y": y_values,
        "x_units": x_units,
        "point_count": len(x_values),
    }


@app.get("/files/{file_id}/content", response_class=FileResponse)
def retrieve_imported_content(file_id: str):
    record = database.get_imported_file(file_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "file_not_found", "message": "Imported file not found."})
    storage_path = resolve_stored_file(record)
    media_type, inline_allowed = safe_content_media_type(record["original_filename"])
    response = FileResponse(
        storage_path,
        media_type=media_type,
        filename=record["original_filename"],
        content_disposition_type="inline" if inline_allowed else "attachment",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return response


@app.get("/files/{file_id}/integrity")
def verify_imported_file_integrity(file_id: str):
    record = database.get_imported_file(file_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "file_not_found", "message": "Imported file not found."},
        )
    storage_path = resolve_stored_file(record, enforce_recorded_size=False)
    actual_size = storage_path.stat().st_size
    actual_sha256 = sha256_file(storage_path)
    expected_size = record["size_bytes"]
    return {
        "file_id": file_id,
        "filename": record["original_filename"],
        "valid": actual_sha256 == record["sha256"] and actual_size == expected_size,
        "expected_sha256": record["sha256"],
        "actual_sha256": actual_sha256,
        "expected_size_bytes": expected_size,
        "actual_size_bytes": actual_size,
        "size_bytes": actual_size,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/files/{file_id}/mapping")
def retrieve_imported_mapping(file_id: str):
    record = database.get_imported_file(file_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "file_not_found", "message": "Imported file not found."})
    content = read_verified_stored_file(record)
    text = content.decode("utf-8-sig", errors="replace")
    row_count = 0
    column_count = None
    minimum = None
    maximum = None
    shape_invalid = False
    for row in iter_numeric_matrix_rows(text):
        row_count += 1
        if column_count is None:
            column_count = len(row)
        elif len(row) != column_count:
            shape_invalid = True
        row_minimum = min(row)
        row_maximum = max(row)
        minimum = row_minimum if minimum is None else min(minimum, row_minimum)
        maximum = row_maximum if maximum is None else max(maximum, row_maximum)
    if row_count < 2 or column_count is None:
        raise HTTPException(status_code=422, detail={"code": "mapping_unavailable", "message": "No numeric mapping matrix was found."})
    if shape_invalid:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "mapping_shape_invalid",
                "message": "Mapping rows must all contain the same number of finite numeric values.",
            },
        )
    row_step = max(1, math.ceil(row_count / 300))
    column_step = max(1, math.ceil(column_count / 300))
    sampled = [
        row[::column_step]
        for index, row in enumerate(iter_numeric_matrix_rows(text))
        if index % row_step == 0
    ][:300]
    return {
        "file_id": file_id,
        "filename": record["original_filename"],
        "matrix": sampled,
        "rows": len(sampled),
        "columns": len(sampled[0]),
        "minimum": minimum,
        "maximum": maximum,
        "source_rows": row_count,
        "source_columns": column_count,
        "note": "Displayed matrix is downsampled to at most 300 x 300 for interactive viewing; min/max describe the complete matrix.",
    }


@app.get("/files/{file_id}/analysis")
def analyze_imported_spectrum(file_id: str, reference_file_id: str | None = None):
    spectrum = retrieve_imported_spectrum(file_id)
    record = database.get_imported_file(file_id)
    x_values = spectrum["x"]
    calibration = None
    if reference_file_id:
        reference = retrieve_imported_spectrum(reference_file_id)
        reference_peak = calculate_peak_metrics(reference["x"], reference["y"], 500, 540)
        reference_peak_usable = bool(
            reference_peak
            and reference_peak.get("fwhm") is not None
            and (reference_peak.get("snr") is None or reference_peak["snr"] >= 5)
        )
        if reference_peak_usable:
            correction = 520.7 - reference_peak["position"]
            x_values = [value + correction for value in x_values]
            calibration = {
                "reference_file_id": reference_file_id,
                "reference_filename": reference["filename"],
                "observed_si_position": reference_peak["position"],
                "target_si_position": 520.7,
                "applied_shift": round(correction, 4),
                "observed_position_uncertainty": reference_peak["position_uncertainty"],
                "reference_peak_snr": reference_peak["snr"],
            }
        else:
            calibration = {
                "reference_file_id": reference_file_id,
                "reference_filename": reference["filename"],
                "warning": "No resolved silicon peak with adequate SNR was found between 500 and 540 cm-1; no calibration shift was applied.",
            }

    result = analyze_raman_spectrum(
        x_values, spectrum["y"], record.get("material_system") if record else None
    )
    result.update(
        {
            "file_id": file_id,
            "filename": spectrum["filename"],
            "sha256": record["sha256"],
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "calibration": calibration,
        }
    )
    return result


@app.post("/files/{file_id}/analysis")
def analyze_processed_spectrum(file_id: str, body: dict = Body(...)):
    """Analyze a derived trace while retaining immutable-raw provenance."""
    record = database.get_imported_file(file_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "file_not_found", "message": "Imported file not found."},
        )
    x_values = body.get("x")
    y_values = body.get("y")
    config = body.get("processing_config", {})
    if not isinstance(x_values, list) or not isinstance(y_values, list):
        raise validation_error("invalid_analysis_data", "x and y must be arrays.")
    if not 3 <= len(x_values) == len(y_values) <= 500000:
        raise validation_error(
            "invalid_analysis_data",
            "x and y must contain the same number of points (3 to 500,000).",
        )
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in x_values + y_values
    ):
        raise validation_error(
            "invalid_analysis_data",
            "x and y must contain JSON numbers, not booleans or numeric text.",
        )
    numeric_x = [float(value) for value in x_values]
    numeric_y = [float(value) for value in y_values]
    if not all(math.isfinite(value) for value in numeric_x + numeric_y):
        raise validation_error("invalid_analysis_data", "x and y must contain only finite numbers.")
    if any(right <= left for left, right in zip(numeric_x, numeric_x[1:])):
        raise validation_error("invalid_analysis_data", "x values must be strictly increasing.")
    source_content = read_verified_stored_file(record)
    source_x, _source_y, _source_units = parse_spectrum_xy(source_content)
    if len(source_x) < 2:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "spectrum_unavailable",
                "message": "No XY spectrum data was found in this file.",
            },
        )
    if len(numeric_x) != len(source_x) or any(
        abs(submitted - source) > max(1e-9, abs(source) * 1e-12)
        for submitted, source in zip(numeric_x, source_x)
    ):
        raise validation_error(
            "analysis_source_mismatch",
            "The processed X coordinates must match the imported source spectrum.",
        )
    if not isinstance(config, dict):
        raise validation_error("invalid_analysis_config", "Analysis configuration must be an object.")
    if (
        config.get("subtract_baseline") is not True
        or config.get("baseline_method") not in {"linear", "polynomial", "als", "rubberband"}
        or config.get("pipeline_order") != PROCESSING_PIPELINE_ORDER
    ):
        raise validation_error(
            "invalid_processing_pipeline",
            "Baseline subtraction must be the first processing stage, using a supported non-empty baseline method.",
        )
    serialized_config = serialize_config_object(config, "analysis_config")

    derived_payload = json.dumps(
        {"x": numeric_x, "y": numeric_y},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    derived_sha256 = hashlib.sha256(derived_payload).hexdigest()
    analyzed_at = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid4())
    result = analyze_raman_spectrum(numeric_x, numeric_y, record.get("material_system"))
    result.update({
        "run_id": run_id,
        "file_id": file_id,
        "filename": record["original_filename"],
        "sha256": record["sha256"],
        "derived_sha256": derived_sha256,
        "analyzed_at": analyzed_at,
        "calibration": None,
        "analysis_input": "processed trace",
        "processing_config": config,
        "raw_data_modified": False,
        "app_version": APP_VERSION,
    })
    with database.connect_database() as connection:
        database.create_analysis_run(connection, {
            "run_id": run_id,
            "file_id": file_id,
            "raw_sha256": record["sha256"],
            "derived_sha256": derived_sha256,
            "derived_trace": derived_payload.decode("utf-8"),
            "processing_config": serialized_config,
            "result": json.dumps(result, ensure_ascii=False, allow_nan=False),
            "app_version": APP_VERSION,
            "created_at": analyzed_at,
        })
    return result


@app.get("/files/{file_id}/analysis-runs")
def list_file_analysis_runs(file_id: str):
    if database.get_imported_file(file_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "file_not_found", "message": "Imported file not found."},
        )
    return [deserialize_analysis_run(run) for run in database.list_analysis_runs(file_id)]


@app.get("/analysis-runs/{run_id}/trace")
def retrieve_analysis_run_trace(run_id: str):
    run = database.get_analysis_run(run_id)
    if run is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "analysis_run_not_found", "message": "Analysis run not found."},
        )
    validated = deserialize_analysis_run(run, include_trace=True)
    if validated["derived_trace"] is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "analysis_trace_unavailable",
                "message": "This legacy analysis run predates retained derived traces and cannot be replayed.",
            },
        )
    source_record = database.get_imported_file(validated["file_id"])
    if source_record is None:
        raise database.StoredDataIntegrityError(
            f"Analysis run {run_id} no longer has an imported source record."
        )
    source_x, _source_y, source_units = parse_spectrum_xy(
        read_verified_stored_file(source_record)
    )
    trace_x = validated["derived_trace"]["x"]
    if len(source_x) != len(trace_x) or any(
        abs(derived - source) > max(1e-9, abs(source) * 1e-12)
        for derived, source in zip(trace_x, source_x)
    ):
        raise database.StoredDataIntegrityError(
            f"Analysis run {run_id} derived X coordinates do not match its source spectrum."
        )
    return {
        "run_id": validated["run_id"],
        "file_id": validated["file_id"],
        "derived_sha256": validated["derived_sha256"],
        "verification": validated["derived_trace_verification"],
        "x_units": source_units,
        **validated["derived_trace"],
    }


@app.get("/samples/{sample_id}/raman-summary")
def summarize_sample_raman_spectra(sample_id: str, analysis_run_id: str | None = None):
    analyses = []
    sample_records = [
        record for record in database.list_imported_files()
        if record.get("sample_id", "").lower() == sample_id.lower()
    ]
    comparison_config = None
    comparison_version = None
    if analysis_run_id:
        source_run = database.get_analysis_run(analysis_run_id)
        if source_run is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "analysis_run_not_found", "message": "Analysis run not found."},
            )
        source_record = database.get_imported_file(source_run["file_id"])
        if source_record is None or source_record.get("sample_id", "").lower() != sample_id.lower():
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "analysis_run_sample_mismatch",
                    "message": "The analysis run does not belong to the requested sample.",
                },
            )
        comparison_config = parse_stored_json_object(
            source_run["processing_config"], "analysis processing configuration"
        )
        comparison_version = source_run["app_version"]

    for record in sample_records:
        if comparison_config is not None:
            matching_run = next((
                run for run in database.list_analysis_runs(record["file_id"])
                if run["app_version"] == comparison_version
                and parse_stored_json_object(
                    run["processing_config"], "analysis processing configuration"
                ) == comparison_config
            ), None)
            if matching_run is None:
                continue
            analysis = parse_stored_json_object(matching_run["result"], "analysis result")
            if analysis["peaks"]["E_mode"] and analysis["peaks"]["A1_mode"]:
                analyses.append({
                    "file_id": record["file_id"],
                    "filename": record["original_filename"],
                    "analysis_run_id": matching_run["run_id"],
                    "derived_sha256": matching_run["derived_sha256"],
                    "E_position": analysis["peaks"]["E_mode"]["position"],
                    "A1_position": analysis["peaks"]["A1_mode"]["position"],
                    "separation": analysis["peak_separation"],
                })
            continue
        try:
            spectrum = retrieve_imported_spectrum(record["file_id"])
        except HTTPException:
            continue
        analysis = analyze_raman_spectrum(
            spectrum["x"], spectrum["y"], record.get("material_system")
        )
        if analysis["peaks"]["E_mode"] and analysis["peaks"]["A1_mode"]:
            analyses.append(
                {
                    "file_id": record["file_id"],
                    "filename": record["original_filename"],
                    "E_position": analysis["peaks"]["E_mode"]["position"],
                    "A1_position": analysis["peaks"]["A1_mode"]["position"],
                    "separation": analysis["peak_separation"],
                }
            )
    separations = [item["separation"] for item in analyses]
    return {
        "sample_id": sample_id,
        "spectrum_count": len(analyses),
        "spectra": analyses,
        "separation_mean": round(statistics.mean(separations), 4) if separations else None,
        "separation_stdev": round(statistics.stdev(separations), 4) if len(separations) > 1 else None,
        "comparison_basis": (
            {
                "analysis_input": "processed trace",
                "processing_config": comparison_config,
                "app_version": comparison_version,
                "source_analysis_run_id": analysis_run_id,
            }
            if comparison_config is not None else
            {"analysis_input": "raw spectrum", "processing_config": None, "app_version": APP_VERSION}
        ),
        "excluded_file_count": len(sample_records) - len(analyses),
    }


@app.post("/analysis/compare")
def compare_imported_spectra(body: dict = Body(...)):
    file_ids = body.get("file_ids")
    if not isinstance(file_ids, list) or not 2 <= len(file_ids) <= 20:
        raise validation_error("invalid_comparison", "Select between 2 and 20 spectra.")
    if any(
        not isinstance(file_id, str)
        or not file_id.strip()
        or len(file_id.strip()) > 100
        for file_id in file_ids
    ):
        raise validation_error(
            "invalid_comparison",
            "Every comparison file ID must be a non-empty string of at most 100 characters.",
        )
    normalized_file_ids = [file_id.strip() for file_id in file_ids]
    if len(set(normalized_file_ids)) != len(normalized_file_ids):
        raise validation_error(
            "invalid_comparison",
            "Choose distinct imported spectra; duplicate file IDs are not allowed.",
        )
    spectra = [retrieve_imported_spectrum(file_id) for file_id in normalized_file_ids]
    reference = spectra[0]
    comparisons = []
    for spectrum in spectra:
        correlation = None
        comparison_warning = None
        overlap_indexes = []
        reference_units = canonical_spectral_units(reference.get("x_units"))
        spectrum_units = canonical_spectral_units(spectrum.get("x_units"))
        if (reference_units is None) != (spectrum_units is None):
            comparison_warning = "X-axis unit compatibility cannot be verified because one spectrum has no units."
        elif reference_units and spectrum_units and reference_units != spectrum_units:
            comparison_warning = "Spectral X-axis units do not match."
        else:
            spectrum_min, spectrum_max = min(spectrum["x"]), max(spectrum["x"])
            overlap_indexes = [
                index for index, value in enumerate(reference["x"])
                if spectrum_min <= value <= spectrum_max
            ]
            if len(overlap_indexes) >= 3:
                overlap_x = [reference["x"][index] for index in overlap_indexes]
                first = [reference["y"][index] for index in overlap_indexes]
                second = interpolate_spectrum(spectrum["x"], spectrum["y"], overlap_x)
                first_mean = statistics.mean(first)
                second_mean = statistics.mean(second)
                numerator = sum((a - first_mean) * (b - second_mean) for a, b in zip(first, second))
                denominator = (
                    sum((a - first_mean) ** 2 for a in first)
                    * sum((b - second_mean) ** 2 for b in second)
                ) ** 0.5
                correlation = round(numerator / denominator, 6) if denominator else None
                if denominator == 0:
                    comparison_warning = "Correlation is undefined for a constant-intensity overlap."
            else:
                comparison_warning = "Fewer than three reference-grid points overlap this spectrum."
        spectrum_record = database.get_imported_file(spectrum["file_id"])
        analysis = analyze_raman_spectrum(
            spectrum["x"], spectrum["y"],
            spectrum_record.get("material_system") if spectrum_record else None,
        )
        comparisons.append(
            {
                "file_id": spectrum["file_id"],
                "filename": spectrum["filename"],
                "correlation_to_first": correlation,
                "overlap_point_count": len(overlap_indexes),
                "overlap_range": (
                    [reference["x"][overlap_indexes[0]], reference["x"][overlap_indexes[-1]]]
                    if overlap_indexes else None
                ),
                "comparison_warning": comparison_warning,
                "peak_separation": analysis["peak_separation"],
                "E_position": analysis["peaks"]["E_mode"]["position"] if analysis["peaks"]["E_mode"] else None,
                "A1_position": analysis["peaks"]["A1_mode"]["position"] if analysis["peaks"]["A1_mode"] else None,
            }
        )
    return {"reference_file_id": reference["file_id"], "spectra": comparisons}


@app.post("/files/import", status_code=201)
async def import_file(
    file: UploadFile = File(...),
    relative_path: str | None = Form(None),
    technique: str = Form(...),
    sample_id: str = Form(...),
    measurement_date: str | None = Form(None),
    instrument: str | None = Form(None),
    operator: str | None = Form(None),
    notes: str | None = Form(None),
    material_system: str | None = Form(None),
    measurement_type: str | None = Form(None),
    laser_wavelength: str | None = Form(None),
    laser_power: str | None = Form(None),
    power_at_sample: str | None = Form(None),
    objective: str | None = Form(None),
    integration_time: str | None = Form(None),
    accumulations: str | None = Form(None),
    detector: str | None = Form(None),
    detector_model: str | None = Form(None),
    detector_temperature: str | None = Form(None),
    spectrometer: str | None = Form(None),
    grating: str | None = Form(None),
    binning_start: str | None = Form(None),
    binning_length: str | None = Form(None),
    spectral_start: str | None = Form(None),
    spectral_range: str | None = Form(None),
    x_units: str | None = Form(None),
    lpf_angle: str | None = Form(None),
    bpf_angle: str | None = Form(None),
    display_mode: str | None = Form(None),
    live_focus: str | None = Form(None),
    z_position: str | None = Form(None),
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
        normalized_material_system = normalize_optional_metadata(
            material_system, "Material system", 200
        )
        detail_values = locals()
        experimental_details = {
            field_name: normalize_optional_metadata(
                detail_values[field_name], field_name.replace("_", " ").title(), 200
            )
            for field_name in EXPERIMENTAL_METADATA_FIELDS
        }

        file_id = str(uuid4())
        staging_directory = RAW_DATA_DIR / ".staging"
        staging_directory.mkdir(parents=True, exist_ok=True)
        staging_path = staging_directory / f"{file_id}.upload"

        size_bytes, sha256 = await stream_upload_to_staging(
            file,
            staging_path,
        )
        # Inspect each file independently at import time. Values explicitly
        # entered by the user always win; only blank fields are auto-filled.
        with staging_path.open("rb") as staged_file:
            suggestions = extract_metadata_suggestions(
                original_filename,
                staged_file.read(200000),
                relative_path,
            )
        measured_bounds = (
            measured_spectral_bounds(staging_path)
            if Path(original_filename).suffix.lower() in SPECTRUM_TEXT_EXTENSIONS
            else None
        )
        if measured_bounds is not None:
            suggestions["spectral_start"] = f"{measured_bounds[0]:g}"
            suggestions["spectral_range"] = f"{measured_bounds[1]:g}"
        if normalized_measurement_date is None:
            normalized_measurement_date = normalize_measurement_date(
                suggestions.get("measurement_date")
            )
        if normalized_instrument is None:
            normalized_instrument = normalize_optional_metadata(
                suggestions.get("instrument"), "Instrument", 200
            )
        experimental_details = {
            field_name: value or normalize_optional_metadata(
                suggestions.get(field_name),
                field_name.replace("_", " ").title(),
                200,
            )
            for field_name, value in experimental_details.items()
        }
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
            "material_system": normalized_material_system,
            "experimental_details": experimental_details,
        }

        with database.connect_database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = database.find_imported_file_by_sha256(
                connection,
                sha256,
            )
            if duplicate is not None:
                if database.is_import_archived(connection, duplicate["file_id"]):
                    read_verified_stored_file(duplicate)
                    restored_values = {
                        "imported_at": imported_at,
                        "technique": normalized_technique,
                        "sample_id": normalized_sample_id,
                        "measurement_date": normalized_measurement_date,
                        "instrument": normalized_instrument,
                        "operator": normalized_operator,
                        "notes": normalized_notes,
                        "material_system": normalized_material_system,
                        "experimental_details": experimental_details,
                    }
                    database.restore_imported_file(
                        connection,
                        duplicate["file_id"],
                        restored_values,
                    )
                    return {**duplicate, **restored_values}
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
