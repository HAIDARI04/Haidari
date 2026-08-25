import hashlib
import csv
import io
import logging
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
from fastapi.responses import FileResponse
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


def parse_spectrum_xy(content: bytes) -> tuple[list[float], list[float], str | None]:
    """Parse numeric XYDATA pairs without modifying the imported raw file."""
    text = content.decode("utf-8-sig", errors="replace")
    x_values: list[float] = []
    y_values: list[float] = []
    x_units = None
    in_xy_data = False

    for line in text.splitlines():
        stripped = line.strip()
        if not in_xy_data:
            if stripped.upper().startswith("XUNITS="):
                x_units = stripped.partition("=")[2].strip() or None
            if stripped.upper() == "XYDATA=":
                in_xy_data = True
            continue

        parts = re.split(r"[,;\t\s]+", stripped)
        if len(parts) < 2:
            continue
        try:
            x_value = float(parts[0])
            y_value = float(parts[1])
        except ValueError:
            continue
        x_values.append(x_value)
        y_values.append(y_value)

    return x_values, y_values, x_units


def interpolate_spectrum(source_x, source_y, target_x):
    pairs = sorted(zip(source_x, source_y))
    result = []
    index = 0
    for target in target_x:
        while index + 1 < len(pairs) and pairs[index + 1][0] < target:
            index += 1
        if target <= pairs[0][0]: result.append(pairs[0][1]); continue
        if target >= pairs[-1][0]: result.append(pairs[-1][1]); continue
        x1, y1 = pairs[index]; x2, y2 = pairs[index + 1]
        result.append(y1 + (target - x1) / (x2 - x1) * (y2 - y1))
    return result


@app.get("/files/{file_id}/substrate-model")
def model_sio2_si_substrate(file_id: str):
    target_spectrum = retrieve_imported_spectrum(file_id)
    target_record = database.get_imported_file(file_id) or {}
    target_x, target_y = target_spectrum["x"], target_spectrum["y"]
    units = target_spectrum["x_units"]
    references, reference_names = [], []
    for record in database.list_imported_files():
        material = re.sub(r"[^a-z0-9]", "", (record.get("material_system") or "").lower().replace("₂", "2"))
        # Only substrate-only measurements teach the model. A target labelled
        # MoS2/SiO2/Si must never become a reference for another target.
        if record["file_id"] == file_id or material not in {
            "sio2si", "sio2onsi", "silicondioxideonsilicon",
        }:
            continue
        reference = retrieve_imported_spectrum(record["file_id"])
        references.append(interpolate_spectrum(reference["x"], reference["y"], target_x))
        reference_names.append(record["original_filename"])
    if not references:
        raise validation_error("no_substrate_references", "No SiO₂/Si reference spectra are available.")

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
    # Fit where the learned references consistently contain substrate signal.
    # This avoids teaching the fit to remove unrelated target-only Raman peaks.
    anchors = [
        index for index, (x_value, signal) in enumerate(zip(target_x, consensus))
        if x_value >= 80 and (signal >= 0.04 * consensus_peak or 500 <= x_value <= 540)
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
    protected_regions = []
    target_material = re.sub(
        r"[^a-z0-9]", "", (target_record.get("material_system") or "").lower().replace("₂", "2")
    )
    if "mos2" in target_material:
        assignments = analyze_raman_spectrum(target_x, target_y)["peaks"]
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
    corrected_y = [value - substrate for value, substrate in zip(target_y, substrate_y)]
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
    return {"x": target_x, "substrate_y": substrate_y, "corrected_y": corrected_y,
            "x_units": units, "scale": round(coefficient_total, 6), "confidence": round(confidence, 4),
            "detected": confidence >= 0.6, "reference_files": reference_names,
            "reference_weights": {
                name: round(weight, 6) for name, weight in zip(reference_names, weights)
            },
            "protected_regions": protected_regions,
            "fit_point_count": len(anchors),
            "method": "target-adaptive non-negative mixture of SiO2/Si reference spectra with material-peak protection"}


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
    baseline = [ys[0] + (ys[-1] - ys[0]) * index / (len(ys) - 1) for index in range(len(ys))]
    corrected = [value - base for value, base in zip(ys, baseline)]
    peak_index = max(range(len(corrected)), key=corrected.__getitem__)
    height = corrected[peak_index]
    if height <= 0:
        return None
    position = xs[peak_index]
    if 0 < peak_index < len(xs) - 1:
        left, center, right = corrected[peak_index - 1:peak_index + 2]
        denominator = left - 2 * center + right
        if denominator:
            step = (xs[peak_index + 1] - xs[peak_index - 1]) / 2
            position += 0.5 * (left - right) / denominator * step
    half_height = height / 2
    left_cross = next((index for index in range(peak_index, 0, -1) if corrected[index - 1] <= half_height), None)
    right_cross = next((index for index in range(peak_index, len(xs) - 1) if corrected[index + 1] <= half_height), None)
    fwhm = xs[right_cross + 1] - xs[left_cross - 1] if left_cross is not None and right_cross is not None else None
    area = sum(
        max(0, corrected[index] + corrected[index + 1]) / 2 * (xs[index + 1] - xs[index])
        for index in range(len(xs) - 1)
    )
    noise = statistics.median(abs(corrected[index + 1] - corrected[index]) for index in range(len(corrected) - 1)) / 0.6745
    return {
        "position": round(position, 4),
        "height": round(height, 4),
        "area": round(area, 4),
        "fwhm": round(fwhm, 4) if fwhm is not None else None,
        "snr": round(height / noise, 2) if noise > 0 else None,
    }


def analyze_raman_spectrum(x_values: list[float], y_values: list[float]) -> dict:
    spacings = [x_values[index + 1] - x_values[index] for index in range(len(x_values) - 1)]
    median_spacing = statistics.median(spacings)
    spacing_deviation = statistics.median(abs(value - median_spacing) for value in spacings)
    maximum = max(y_values)
    saturation_fraction = sum(value == maximum for value in y_values) / len(y_values)
    warnings = []
    if median_spacing <= 0:
        warnings.append("X values are not strictly increasing.")
    if median_spacing and spacing_deviation / abs(median_spacing) > 0.01:
        warnings.append("X-axis spacing is irregular.")
    if saturation_fraction > 0.005:
        warnings.append("Repeated maximum values may indicate detector saturation.")

    e_mode = calculate_peak_metrics(x_values, y_values, 365, 395)
    a_mode = calculate_peak_metrics(x_values, y_values, 395, 425)
    separation = None
    layer_estimate = None
    if e_mode and a_mode:
        separation = round(a_mode["position"] - e_mode["position"], 4)
        if 18 <= separation < 21:
            layer_estimate = "monolayer-like"
        elif 21 <= separation < 25:
            layer_estimate = "few-layer-like"
        elif separation >= 25:
            layer_estimate = "bulk/multilayer-like"
        else:
            layer_estimate = "outside typical MoS2 range"
    else:
        warnings.append("Both principal MoS2 modes could not be measured reliably.")

    minimum_snr = min(
        [peak["snr"] for peak in (e_mode, a_mode) if peak and peak.get("snr") is not None],
        default=0,
    )
    quality_badge = "Good"
    if warnings or minimum_snr < 10:
        quality_badge = "Review"
    if not e_mode or not a_mode or minimum_snr < 3:
        quality_badge = "Poor"

    return {
        "peaks": {"E_mode": e_mode, "A1_mode": a_mode},
        "peak_separation": separation,
        "layer_estimate": layer_estimate,
        "quality": {
            "point_count": len(x_values),
            "median_spacing": round(median_spacing, 6),
            "spacing_mad": round(spacing_deviation, 6),
            "intensity_min": min(y_values),
            "intensity_max": maximum,
            "badge": quality_badge,
            "warnings": warnings,
        },
        "interpretation_warning": "Layer, strain, and doping interpretations require calibration and suitable reference measurements.",
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


@app.get("/operators")
def list_operators():
    return database.list_operators()


@app.post("/files/archive-batch")
def archive_imported_files(body: dict = Body(...)):
    file_ids = body.get("file_ids")
    if not isinstance(file_ids, list) or not file_ids:
        raise validation_error("missing_file_ids", "Select at least one imported file.")
    unique_ids = list(dict.fromkeys(str(file_id) for file_id in file_ids if str(file_id).strip()))
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
    sample_id = sample.get("sample_id")
    if not sample_id or not str(sample_id).strip():
        raise validation_error("missing_sample_id", "`sample_id` is required.")
    record = {
        "sample_id": str(sample_id).strip(),
        "material": sample.get("material"),
        "substrate": sample.get("substrate"),
        "project": sample.get("project"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "notes": sample.get("notes"),
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
    if not name or not str(name).strip() or not isinstance(config, dict):
        raise validation_error("invalid_preset", "`name` and `config` object are required.")
    record = {
        "preset_id": str(uuid4()),
        "name": str(name).strip(),
        "config": json.dumps(config),
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
        {**recipe, "config": json.loads(recipe["config"])}
        for recipe in database.list_analysis_recipes()
    ]


@app.post("/analysis/recipes", status_code=201)
def create_analysis_recipe(body: dict = Body(...)):
    name = str(body.get("name", "")).strip()
    config = body.get("config")
    if not name or not isinstance(config, dict):
        raise validation_error("invalid_recipe", "Recipe name and configuration are required.")
    recipe = {
        "recipe_id": str(uuid4()),
        "name": name[:100],
        "config": json.dumps(config),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with database.connect_database() as connection:
        database.create_analysis_recipe(connection, recipe)
    return {**recipe, "config": config}


@app.put("/presets/{preset_id}")
def update_preset(preset_id: str, body: dict = Body(...)):
    name = body.get("name")
    config = body.get("config")
    if not name or not isinstance(name, str) or not name.strip():
        raise validation_error("invalid_preset_name", "`name` is required and must be a non-empty string.")
    if not isinstance(config, dict):
        raise validation_error("invalid_preset_config", "`config` must be a JSON object mapping field names to values.")

    # simple validation: keys must be strings and values must be primitive
    for k, v in config.items():
        if not isinstance(k, str):
            raise validation_error("invalid_preset_config", "preset config keys must be strings")
        if not isinstance(v, (str, int, float)) and v is not None:
            raise validation_error("invalid_preset_config", "preset config values must be string/number/null")

    record = database.get_preset(preset_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "preset_not_found", "message": "Preset not found."})

    try:
        with database.connect_database() as connection:
            database.update_preset(connection, preset_id, name.strip(), json.dumps(config))
    except sqlite3.Error:
        raise HTTPException(status_code=500, detail={"code": "preset_update_failed", "message": "Failed to update preset."})

    return {"preset_id": preset_id, "name": name.strip(), "config": config}


@app.post("/files/inspect")
async def inspect_file(
    file: UploadFile = File(...),
    relative_path: str | None = Form(None),
):
    # Read a limited preview of the file and attempt simple format detection
    max_preview = 200000
    content = await file.read(max_preview)
    size = 0
    try:
        size = (await file.seek(0, os.SEEK_END))
    except Exception:
        size = len(content)

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
    storage_path = Path(record["storage_path"])
    try:
        storage_path.resolve().relative_to(RAW_DATA_DIR.resolve())
    except ValueError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "invalid_storage_path", "message": "Stored file path is invalid."},
        ) from error
    if not storage_path.is_file():
        raise HTTPException(
            status_code=404,
            detail={"code": "stored_file_missing", "message": "The imported raw file is missing."},
        )
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
    experimental_details = {}
    for field_name in EXPERIMENTAL_METADATA_FIELDS:
        raw_value = body.get(field_name, nested_details.get(field_name))
        experimental_details[field_name] = normalize_optional_metadata(
            None if raw_value is None else str(raw_value),
            field_name.replace("_", " ").title(),
            200,
        )
    metadata = {
        "technique": normalize_required_metadata(str(body.get("technique", "")), "Technique", 100),
        "sample_id": normalize_required_metadata(str(body.get("sample_id", "")), "Sample ID", 100),
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

    storage_path = Path(record["storage_path"])
    try:
        storage_path.resolve().relative_to(RAW_DATA_DIR.resolve())
    except ValueError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "invalid_storage_path", "message": "Stored file path is invalid."},
        ) from error
    if not storage_path.is_file():
        raise HTTPException(
            status_code=404,
            detail={"code": "stored_file_missing", "message": "The imported raw file is missing."},
        )

    x_values, y_values, x_units = parse_spectrum_xy(storage_path.read_bytes())
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
    storage_path = Path(record["storage_path"])
    try:
        storage_path.resolve().relative_to(RAW_DATA_DIR.resolve())
    except ValueError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "invalid_storage_path", "message": "Stored file path is invalid."},
        ) from error
    if not storage_path.is_file():
        raise HTTPException(status_code=404, detail={"code": "stored_file_missing", "message": "The imported raw file is missing."})
    return FileResponse(storage_path, media_type=record["content_type"] or "application/octet-stream")


@app.get("/files/{file_id}/mapping")
def retrieve_imported_mapping(file_id: str):
    record = database.get_imported_file(file_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "file_not_found", "message": "Imported file not found."})
    storage_path = Path(record["storage_path"])
    try:
        storage_path.resolve().relative_to(RAW_DATA_DIR.resolve())
    except ValueError as error:
        raise HTTPException(status_code=409, detail={"code": "invalid_storage_path", "message": "Stored file path is invalid."}) from error
    rows = []
    for row in csv.reader(io.StringIO(storage_path.read_text(encoding="utf-8-sig", errors="replace"))):
        try:
            numeric = [float(value.strip()) for value in row if value.strip()]
        except ValueError:
            continue
        if len(numeric) >= 2:
            rows.append(numeric)
    if len(rows) < 2:
        raise HTTPException(status_code=422, detail={"code": "mapping_unavailable", "message": "No numeric mapping matrix was found."})
    column_count = min(len(row) for row in rows)
    matrix = [row[:column_count] for row in rows]
    row_step = max(1, len(matrix) // 300)
    column_step = max(1, column_count // 300)
    sampled = [row[::column_step] for row in matrix[::row_step]]
    values = [value for row in sampled for value in row]
    return {
        "file_id": file_id,
        "filename": record["original_filename"],
        "matrix": sampled,
        "rows": len(sampled),
        "columns": len(sampled[0]),
        "minimum": min(values),
        "maximum": max(values),
        "note": "Displayed matrix may be downsampled to 300 x 300 for interactive viewing.",
    }


@app.get("/files/{file_id}/analysis")
def analyze_imported_spectrum(file_id: str, reference_file_id: str | None = None):
    spectrum = retrieve_imported_spectrum(file_id)
    x_values = spectrum["x"]
    calibration = None
    if reference_file_id:
        reference = retrieve_imported_spectrum(reference_file_id)
        reference_peak = calculate_peak_metrics(reference["x"], reference["y"], 500, 540)
        if reference_peak:
            correction = 520.7 - reference_peak["position"]
            x_values = [value + correction for value in x_values]
            calibration = {
                "reference_file_id": reference_file_id,
                "reference_filename": reference["filename"],
                "observed_si_position": reference_peak["position"],
                "target_si_position": 520.7,
                "applied_shift": round(correction, 4),
            }
        else:
            calibration = {
                "reference_file_id": reference_file_id,
                "reference_filename": reference["filename"],
                "warning": "No usable silicon peak was found between 500 and 540 cm-1.",
            }

    result = analyze_raman_spectrum(x_values, spectrum["y"])
    record = database.get_imported_file(file_id)
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


@app.get("/samples/{sample_id}/raman-summary")
def summarize_sample_raman_spectra(sample_id: str):
    analyses = []
    for record in database.list_imported_files():
        if record.get("sample_id", "").lower() != sample_id.lower():
            continue
        try:
            spectrum = retrieve_imported_spectrum(record["file_id"])
        except HTTPException:
            continue
        analysis = analyze_raman_spectrum(spectrum["x"], spectrum["y"])
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
    }


@app.post("/analysis/compare")
def compare_imported_spectra(body: dict = Body(...)):
    file_ids = body.get("file_ids")
    if not isinstance(file_ids, list) or not 2 <= len(file_ids) <= 20:
        raise validation_error("invalid_comparison", "Select between 2 and 20 spectra.")
    spectra = [retrieve_imported_spectrum(str(file_id)) for file_id in file_ids]
    reference = spectra[0]
    comparisons = []
    for spectrum in spectra:
        count = min(len(reference["y"]), len(spectrum["y"]))
        first = reference["y"][:count]
        second = spectrum["y"][:count]
        first_mean = statistics.mean(first)
        second_mean = statistics.mean(second)
        numerator = sum((a - first_mean) * (b - second_mean) for a, b in zip(first, second))
        denominator = (
            sum((a - first_mean) ** 2 for a in first)
            * sum((b - second_mean) ** 2 for b in second)
        ) ** 0.5
        analysis = analyze_raman_spectrum(spectrum["x"], spectrum["y"])
        comparisons.append(
            {
                "file_id": spectrum["file_id"],
                "filename": spectrum["filename"],
                "correlation_to_first": round(numerator / denominator, 6) if denominator else None,
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
