import hashlib
from html.parser import HTMLParser
import hashlib
import json
import math
import shutil
import sqlite3
import subprocess
from pathlib import Path

import database
import main
import pytest
from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


class InterfaceStructureParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.label_targets = []

    def handle_starttag(self, _tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.append(attributes["id"])
        if _tag == "label" and attributes.get("for"):
            self.label_targets.append(attributes["for"])


def extract_javascript_function(source, function_name):
    start = source.index(f"function {function_name}(")
    brace = source.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for index in range(brace, len(source)):
        character = source[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in "'\"`":
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"Unterminated JavaScript function: {function_name}")

DEFAULT_METADATA = {
    "technique": "Raman spectroscopy",
    "sample_id": "SAMPLE-001",
    "measurement_date": "2026-07-29",
    "instrument": "Lab Raman 532 nm",
    "operator": "Test Operator",
    "notes": "First pilot measurement",
}
BASELINE_FIRST_CONFIG = {
    "subtract_baseline": True,
    "baseline_method": "rubberband",
    "pipeline_order": main.PROCESSING_PIPELINE_ORDER,
}


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    temporary_database = tmp_path / "test.db"
    temporary_raw_directory = tmp_path / "raw"
    monkeypatch.setattr(database, "DATABASE_PATH", temporary_database)
    monkeypatch.setattr(main, "RAW_DATA_DIR", temporary_raw_directory)
    database.initialize_database()
    return temporary_database, temporary_raw_directory


def post_import(
    filename="sample.bin",
    contents=b"experimental bytes",
    metadata=None,
):
    return client.post(
        "/files/import",
        files={"file": (filename, contents, "application/octet-stream")},
        data=DEFAULT_METADATA if metadata is None else metadata,
    )


def assert_no_import_artifacts(raw_directory):
    with database.connect_database() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM imported_files"
        ).fetchone()[0]
    assert count == 0
    if raw_directory.exists():
        assert [path for path in raw_directory.rglob("*") if path.is_file()] == []


def test_read_root():
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "application": "Materials Data Copilot",
        "status": "running",
        "version": main.APP_VERSION,
    }


def test_health_check():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["app_version"] == main.APP_VERSION
    assert response.json()["database_integrity"] == "ok"
    assert isinstance(response.json()["import_record_count"], int)
    assert isinstance(response.json()["analysis_run_count"], int)
    assert response.json()["analysis_provenance"] == "ok"
    assert response.json()["structured_data_integrity"] == "ok"
    assert isinstance(response.json()["raw_storage_available"], bool)


def test_upload_interface_is_available():
    response = client.get("/upload")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "object-src 'none'" in response.headers["content-security-policy"]
    assert "Drop a file here" in response.text
    assert '<input id="file" name="file" type="file" multiple>' in response.text
    assert '<input id="folder" type="file" multiple webkitdirectory directory>' in response.text
    assert 'id="file" name="file" type="file" accept=' not in response.text
    assert 'id="choose-files-button"' in response.text
    assert "input.click()" in response.text
    assert "folderInput.click()" in response.text
    assert "openFolderPickerAndSelect();" in response.text
    assert "const nested = await collectFilesFromDirectory" in response.text
    assert "file.webkitRelativePath || file.relativePath || file.name" in response.text
    assert 'id="import-queue-panel"' in response.text
    assert 'id="import-summary-panel"' in response.text
    assert "renderTreeNode(root, tree)" in response.text
    assert "Batch metadata: values entered below apply to every selected file" in response.text
    assert "state.textContent = 'Waiting'" in response.text
    assert "summaryHeading.textContent = 'Import complete'" in response.text
    assert "files will be inspected and filled independently during import" in response.text
    assert "const perFileEntries = { ...(inspection.suggested_metadata || {}), ...finalEntries }" in response.text
    assert "importQueuePanel.style.display = 'none'" in response.text
    assert "importSummaryPanel.dataset.completionId === completionId" in response.text
    assert "}, 4000);" in response.text
    assert "removeGroupButton.textContent = 'Remove group'" in response.text
    assert "menu.addEventListener('pointerleave', scheduleActionsClose)" in response.text
    assert "actions.addEventListener('pointerenter', cancelActionsClose)" in response.text
    assert "fetch('/files/archive-batch'" in response.text
    assert 'id="edit-metadata-modal"' in response.text
    assert 'id="save-metadata-edit"' in response.text
    assert 'name="material_system"' in response.text
    assert 'id="operator-input" list="operator-list"' in response.text
    assert 'id="operator-list"' in response.text
    assert "if (!operatorInput.value.trim() && operators.length) operatorInput.value = operators[0]" in response.text
    assert "function confirmMaterialProposal" in response.text
    assert "window.confirm(message)" in response.text
    assert "You can change it later in Edit metadata" in response.text
    assert 'id="edit-material-system"' in response.text
    assert 'id="edit-measurement-type"' in response.text
    assert 'id="edit-laser-wavelength"' in response.text
    assert '<h2 id="recent-heading">Imports</h2>' in response.text
    assert 'id="recent-search"' in response.text
    assert 'id="imports-group-by"' in response.text
    assert ".recent-filter-row { display: grid; grid-template-columns: minmax(0, 1fr)" in response.text
    assert ".recent-pagination { display: flex; flex-wrap: wrap" in response.text
    assert "width: min(170px, calc(100% - .5rem))" in response.text
    assert 'value="material" selected>Group by material system' in response.text
    assert "file.material_system || 'Unspecified material'" in response.text
    assert 'id="recent-technique-filter"' in response.text
    assert 'id="recent-sort"' in response.text
    assert 'id="recent-previous"' in response.text
    assert 'id="recent-next"' in response.text
    assert "group.files.length > 3" in response.text
    assert "classList.add('scrollable')" in response.text
    assert "summary.addEventListener('dblclick'" in response.text
    assert "entries.classList.remove('scrollable')" in response.text
    assert "all ${group.files.length} files shown" in response.text
    assert "batch.addEventListener('toggle'" in response.text
    assert "if (batch.open || group.files.length <= 3) return" in response.text
    assert 'id="remembered-folder"' in response.text
    assert "handle.queryPermission" in response.text
    assert "handle.requestPermission" in response.text
    assert 'id="normalize-spectrum"' in response.text
    assert 'id="baseline-spectrum"' in response.text
    assert 'id="detect-cosmic-rays"' in response.text
    assert 'id="subtract-rayleigh-line"' in response.text
    assert 'id="rayleigh-cutoff"' in response.text
    assert 'id="show-detected-baseline"' in response.text
    assert 'id="subtract-baseline"' in response.text
    assert 'id="peak-fitting-heading"' in response.text
    assert 'id="show-cumulative-fit"' in response.text
    assert 'id="fit-peak-selector"' in response.text
    assert "const fitPeakColors =" in response.text
    assert "selectedPeaks.forEach((peak)" in response.text
    assert "function assignedRamanPeaks(xValues, yValues)" in response.text
    assert "const fitPeaks = fittedPeaks(xValues, yValues)" in response.text
    assert "MoS₂ E′ / E₂g¹" in response.text
    assert "MoS₂ A₁′ / A₁g" in response.text
    assert response.text.count('<details class="analysis-section" open>') == 3
    assert '<details class="analysis-section">' in response.text
    assert '<summary id="baseline-heading">1. Baseline detection &amp; subtraction</summary>' in response.text
    assert '<summary id="artifacts-heading">2. Instrumental artifacts &amp; substrate</summary>' in response.text
    assert response.text.index('id="baseline-heading"') < response.text.index('id="artifacts-heading"')
    assert 'id="subtract-baseline" type="checkbox" checked disabled' in response.text
    assert 'id="process-analyze-group" class="spectrum-control-group"' in response.text
    assert "processAnalyzeGroup.open = true" in response.text
    assert '<summary id="additional-processing-heading">4. Additional processing</summary>' in response.text
    assert 'Fit peaks &amp; calculate parameters' in response.text
    assert "function cosmicRayIndexes(values)" in response.text
    assert "function subtractRayleighLeakage(xValues, yValues, halfWidth)" in response.text
    assert 'value="__sio2_si_model__">Target-adaptive SiO₂/Si model' in response.text
    assert 'id="show-subtracted-reference" type="checkbox" disabled' in response.text
    assert "referenceLegend.textContent = 'Subtracted reference'" in response.text
    assert "originalLegend.textContent = 'Original spectrum'" in response.text
    assert "stroke: '#b8c0c8'" in response.text
    assert 'id="residual-review-modal"' in response.text
    assert "submitResidualDecision('keep')" in response.text
    assert "submitResidualDecision('remove')" in response.text
    assert "submitResidualDecision('command')" in response.text
    assert "/substrate-model`" in response.text
    assert "await refreshReferenceForCurrentFile(false)" in response.text
    assert "requestedFileId !== currentImportedFile?.file_id" in response.text
    assert "function detectedBaseline(xValues, values, method)" in response.text
    assert "function asymmetricLeastSquaresBaseline(values" in response.text
    assert "function rubberBandBaseline(xValues, values)" in response.text
    assert "function normalizedIntensities(xValues, sourceValues, method" in response.text
    assert "function interpolatedReferenceValues(reference, targetX)" in response.text
    assert "function currentProcessingConfig()" in response.text
    assert "function smoothedIntensities(xValues, sourceValues, method, requestedWindow)" in response.text
    assert "subtract_rayleigh_line: subtractRayleighLine.checked" in response.text
    assert "subtract_baseline: true" in response.text
    assert "pipeline_order: 'baseline > cosmic_rays > rayleigh > substrate_reference > smoothing > normalization'" in response.text
    assert "reference_provenance: subtractReference.checked ? referenceProvenance : null" in response.text
    assert "processing_config: currentProcessingConfig()" in response.text
    assert "function artifactAndBaselineIntensities(xValues, sourceValues)" in response.text
    pipeline_function = extract_javascript_function(response.text, "artifactAndBaselineIntensities")
    assert pipeline_function.index("detectedBaseline") < pipeline_function.index("cosmicRayIndexes")
    assert pipeline_function.index("cosmicRayIndexes") < pipeline_function.index("subtractRayleighLeakage")
    assert "const fitOrigin = 0" in response.text
    assert "value - modelValues[index]" in response.text
    assert "const preparedReference = processedReferenceIntensities(spectrum)" in response.text
    assert "referenceSpectrum?.correctedY?.length === spectrum.y.length" in response.text
    assert "reviewContent.replaceChildren()" in response.text
    assert "reviewContent.innerHTML = html" not in response.text
    assert "verifyButton.textContent = 'Verify raw file'" in response.text
    assert "/integrity`" in response.text
    assert "Reference loading failed:" in response.text
    assert "uploader-provided MIME type, which can be forged" in response.text
    assert "Could not save the analysis recipe." in response.text
    assert "Could not save the preset." in response.text
    assert 'id="review-modal" role="dialog" aria-modal="true"' in response.text
    assert "openAccessibleModal(reviewModal, confirmImport)" in response.text
    assert "function closeAccessibleModal(modal)" in response.text
    assert "event.key === 'Escape'" in response.text
    assert "event.key !== 'Tab'" in response.text
    assert 'id="smoothing-window"' in response.text
    assert 'id="show-peaks"' in response.text
    assert 'id="box-zoom-mode"' in response.text
    assert 'id="pan-spectrum-mode"' in response.text
    assert 'id="show-plot-details" type="checkbox" checked' in response.text
    assert "function renderPlotDetails(spectrum)" in response.text
    assert "'data-plot-details': 'true'" in response.text
    assert "event.button === 1 ? 'pan'" in response.text
    assert "nextSpan >= fullSpan * 0.999999" in response.text
    assert "function paddedYRange(values, fraction = 0.06)" in response.text
    assert "const paddedY = paddedYRange(rangeValues)" in response.text
    assert "const reachedFullView = event.deltaY > 0" in response.text
    assert "const fullY = processedIntensities(currentSpectrum)" in response.text
    assert "nextMin = fullMin" in response.text
    assert "nextMax = fullMax" in response.text
    assert 'id="export-spectrum-csv"' in response.text
    assert 'id="export-spectrum-svg"' in response.text
    assert 'id="export-spectrum-png"' in response.text
    assert 'id="view-analysis-history"' in response.text
    assert "/analysis-runs`" in response.text
    assert "function replayAnalysisRun(run, button)" in response.text
    assert "function stopHistoricalReplay()" in response.text
    assert "derived_trace_verification !== 'verified'" in response.text
    assert "processing controls are not reapplied" in response.text
    assert "renderSpectrum(); // Preserve the current zoom/pan window." in response.text
    assert "Method-matched repeatability:" in response.text
    assert "Comparing spectra on their shared Raman-shift coordinates" in response.text
    assert 'id="spectrum-panel" class="panel"' in response.text
    assert "[cropMin, cropMax].forEach((control) => control.addEventListener('change', resetSpectrumView))" in response.text
    assert "resumeCurrentProcessingForControlChange();" in response.text
    assert "if (subtractReference.checked) renderSpectrum();" in response.text
    assert "appendChild(spectrumPanel)" in response.text
    assert 'name="technique"' in response.text
    assert 'name="sample_id"' in response.text


def test_upload_interface_has_valid_control_references():
    response = client.get("/upload")
    parser = InterfaceStructureParser()
    parser.feed(response.text)

    duplicates = {element_id for element_id in parser.ids if parser.ids.count(element_id) > 1}
    missing_targets = set(parser.label_targets) - set(parser.ids)

    assert duplicates == set()
    assert missing_targets == set()


def test_upload_interface_inline_javascript_compiles(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")

    response = client.get("/upload")
    scripts = []

    class ScriptParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_inline_script = False
            self.parts = []

        def handle_starttag(self, tag, attrs):
            if tag == "script" and "src" not in dict(attrs):
                self.in_inline_script = True
                self.parts = []

        def handle_data(self, data):
            if self.in_inline_script:
                self.parts.append(data)

        def handle_endtag(self, tag):
            if tag == "script" and self.in_inline_script:
                scripts.append("".join(self.parts))
                self.in_inline_script = False

    parser = ScriptParser()
    parser.feed(response.text)
    assert scripts
    script_path = tmp_path / "upload-inline.js"
    script_path.write_text("\n".join(scripts), encoding="utf-8")

    result = subprocess.run(
        [node, "--check", str(script_path)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def stored_analysis_run(file_id, raw_sha256, run_id="stored-run", **overrides):
    trace = json.dumps(
        {"x": [1.0, 2.0, 3.0], "y": [1.0, 2.0, 3.0]},
        separators=(",", ":"),
    )
    derived_sha256 = hashlib.sha256(trace.encode()).hexdigest()
    created_at = "2026-08-25T00:00:00+00:00"
    config = "{}"
    result = {
        "run_id": run_id,
        "file_id": file_id,
        "sha256": raw_sha256,
        "derived_sha256": derived_sha256,
        "app_version": main.APP_VERSION,
        "analyzed_at": created_at,
        "analysis_input": "processed trace",
        "raw_data_modified": False,
        "processing_config": {},
    }
    record = {
        "run_id": run_id,
        "file_id": file_id,
        "raw_sha256": raw_sha256,
        "derived_sha256": derived_sha256,
        "derived_trace": trace,
        "processing_config": config,
        "result": json.dumps(result),
        "app_version": main.APP_VERSION,
        "created_at": created_at,
    }
    record.update(overrides)
    return record
    assert result.returncode == 0, result.stderr


def test_spectrum_processing_algorithms_have_expected_numerical_behavior(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")

    html = client.get("/upload").text
    functions = "\n".join(
        extract_javascript_function(html, name)
        for name in (
            "solveThreeByThree",
            "robustQuadraticBaseline",
            "asymmetricLeastSquaresBaseline",
            "rubberBandBaseline",
            "normalizedIntensities",
            "interpolatedReferenceValues",
            "smoothedIntensities",
        )
    )
    assertions = r"""
const x = Array.from({length: 201}, (_, index) => index / 2);
const trueBaseline = x.map(value => 5 + 0.03 * value + 0.0005 * value * value);
const signal = x.map((value, index) => trueBaseline[index]
  + 30 * Math.exp(-0.5 * ((value - 35) / 2) ** 2)
  + 18 * Math.exp(-0.5 * ((value - 72) / 3) ** 2));
const polynomial = robustQuadraticBaseline(x, signal);
const polynomialMae = polynomial.reduce((sum, value, index) => sum + Math.abs(value - trueBaseline[index]), 0) / x.length;
if (!polynomial.every(Number.isFinite) || polynomialMae > 1.5) throw new Error(`quadratic baseline MAE ${polynomialMae}`);
const rubberBand = rubberBandBaseline(x, signal);
if (!rubberBand.every(Number.isFinite)) throw new Error('rubber-band produced a non-finite value');
if (rubberBand.some((value, index) => value > signal[index] + 1e-9)) throw new Error('rubber-band crossed above the spectrum');
const als = asymmetricLeastSquaresBaseline(signal);
if (!als.every(Number.isFinite)) throw new Error('ALS produced a non-finite value');
const maximum = normalizedIntensities(x, signal, 'maximum');
if (Math.abs(Math.max(...maximum.map(Math.abs)) - 1) > 1e-12) throw new Error('maximum normalization failed');
const area = normalizedIntensities(x, signal, 'area');
let normalizedArea = 0;
for (let index = 0; index < area.length - 1; index += 1) normalizedArea += (Math.abs(area[index]) + Math.abs(area[index + 1])) * (x[index + 1] - x[index]) / 2;
if (Math.abs(normalizedArea - 1) > 1e-10) throw new Error(`area normalization ${normalizedArea}`);
const interpolated = interpolatedReferenceValues(
  {x: [0, 2, 4, 6], y: [0, 20, 40, 60]},
  [0, 1, 3, 5, 6],
);
if (JSON.stringify(interpolated) !== JSON.stringify([0, 10, 30, 50, 60])) throw new Error(`reference interpolation ${interpolated}`);
if (interpolatedReferenceValues({x: [1, 2, 3], y: [10, 20, 30]}, [0, 1, 2, 3, 4]) !== null) throw new Error('partial reference coverage was accepted');
if (interpolatedReferenceValues({x: [0, 2, 2], y: [0, 20, 21]}, [0, 1, 2]) !== null) throw new Error('duplicate reference X was accepted');
const smoothingX = [0, 1, 2, 3, 4, 5, 6];
const impulse = [0, 0, 0, 1, 0, 0, 0];
const moving = smoothedIntensities(smoothingX, impulse, 'moving', 5);
if (Math.abs(moving[3] - 0.2) > 1e-12) throw new Error(`moving average ${moving}`);
const gaussianSmooth = smoothedIntensities(smoothingX, impulse, 'gaussian', 5);
if (!(gaussianSmooth[3] < 1 && gaussianSmooth[3] > gaussianSmooth[2] && Math.abs(gaussianSmooth[2] - gaussianSmooth[4]) < 1e-12)) throw new Error(`Gaussian smoothing ${gaussianSmooth}`);
const irregularX = [0, 1, 2.5, 4, 7, 8];
const quadraticY = irregularX.map(value => 3 + 2 * value + 0.5 * value * value);
const savitzky = smoothedIntensities(irregularX, quadraticY, 'savitzky', 5);
if (savitzky.some((value, index) => Math.abs(value - quadraticY[index]) > 1e-8)) throw new Error(`X-aware Savitzky-Golay ${savitzky}`);
"""
    script_path = tmp_path / "spectrum-processing-test.js"
    script_path.write_text(functions + assertions, encoding="utf-8")
    result = subprocess.run(
        [node, str(script_path)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_inspect_raman_file_suggests_only_high_confidence_metadata():
    contents = b"\n".join(
        [
            b"FILETYPE=RAMAN SPECTRUM",
            b"DATETIME=2026-03-10 08:30:34",
            b"LASER=532",
            b"CCD=Artemis",
            b"CCDMODEL=Atik428EX",
            b"TEMPERATURE=-25",
            b"IT=1000ms",
            b"MODEL=XperRAM-C2",
            b"SPECTROMETER=XPE35",
            b"GRATING=2400",
            b"BINNING_START=697",
            b"BINNING_LENGTH=14",
            b"STARTFROM=-45",
            b"RSRANGE=2346.60098065534",
            b"LPFANGLE=0",
            b"BPFANGLE=0",
            b"SPECTRATAKEN=30",
            b"DISPLAYMODE=AVERAGE",
            b"LIVEFOCUS=False",
            b"ZPOSITION=0",
            b"TYPE=RamanShift",
            b"XUNITS=1/cm",
            b"XYDATA=",
            b"100,200",
        ]
    )

    response = client.post(
        "/files/inspect",
        files={"file": ("A01_MoS2_CVD_Spectrum1.txt", contents, "text/plain")},
        data={"relative_path": "A01-2026.03.10/A01_MoS2_CVD_Spectrum1.txt"},
    )

    assert response.status_code == 200
    suggestions = response.json()["suggested_metadata"]
    assert suggestions == {
        "technique": "Raman spectroscopy",
        "measurement_type": "Single spectrum",
        "measurement_date": "2026-03-10",
        "laser_wavelength": "532",
        "integration_time": "1",
        "accumulations": "30",
        "instrument": "XperRAM-C2 / XPE35",
        "detector": "Artemis",
        "detector_model": "Atik428EX",
        "detector_temperature": "-25",
        "spectrometer": "XPE35",
        "grating": "2400",
        "binning_start": "697",
        "binning_length": "14",
        "spectral_start": "-45",
        "spectral_range": "2346.60098065534",
        "lpf_angle": "0",
        "bpf_angle": "0",
        "display_mode": "AVERAGE",
        "live_focus": "False",
        "z_position": "0",
        "x_units": "1/cm",
        "sample_id": "A01",
    }
    assert "operator" not in suggestions
    assert "objective" not in suggestions
    assert "laser_power" not in suggestions
    proposal = response.json()["material_system_proposal"]
    assert proposal["material_system"] == "MoS₂"
    assert proposal["requires_confirmation"] is True
    assert proposal["sources"]


def test_material_proposal_uses_spectral_peaks_without_filename_hint():
    contents = b"\n".join([
        b"FILETYPE=RAMAN SPECTRUM", b"XUNITS=1/cm", b"XYDATA=",
        b"378,1", b"385,20", b"392,1", b"399,1", b"406,25", b"414,1",
    ])

    response = client.post(
        "/files/inspect",
        files={"file": ("unknown_sample.txt", contents, "text/plain")},
    )

    assert response.status_code == 200
    proposal = response.json()["material_system_proposal"]
    assert proposal["material_system"] == "MoS₂"
    assert proposal["confidence"] == 0.84
    assert "matched Raman peaks" in proposal["evidence"][0]
    assert proposal["requires_confirmation"] is True


def test_measured_xy_limits_override_inconsistent_configured_spectral_range():
    contents = b"\n".join([
        b"FILETYPE=RAMAN SPECTRUM",
        b"STARTFROM=-45",
        b"RSRANGE=2346.60098065534",
        b"XUNITS=1/cm",
        b"XYDATA=",
        b"-14.915,353.2",
        b"0.937,12040.8",
        b"3328.346,320.1",
    ])

    response = client.post(
        "/files/inspect",
        files={"file": ("measured-range.txt", contents, "text/plain")},
    )

    assert response.status_code == 200
    suggestions = response.json()["suggested_metadata"]
    assert suggestions["spectral_start"] == "-14.915"
    assert suggestions["spectral_range"] == "3328.35"


def test_inspection_reports_full_size_when_preview_is_limited():
    contents = b"XUNITS=1/cm\nXYDATA=\n100,1\n" + b" " * 250000

    response = client.post(
        "/files/inspect",
        files={"file": ("large-preview.txt", contents, "text/plain")},
    )

    assert response.status_code == 200
    assert response.json()["size"] == len(contents)


def test_import_scans_complete_file_for_measured_spectral_end(isolated_storage):
    contents = (
        b"XUNITS=1/cm\nXYDATA=\n100,1\n"
        + b"#" * 210000
        + b"\n2500,2\n"
    )

    response = post_import("long-spectrum.txt", contents)

    assert response.status_code == 201
    details = response.json()["experimental_details"]
    assert details["spectral_start"] == "100"
    assert details["spectral_range"] == "2500"


def test_spectrum_parser_ignores_non_finite_coordinates():
    x_values, y_values, units = main.parse_spectrum_xy(
        b"XUNITS=1/cm\nXYDATA=\n100,2\nnan,3\n102,inf\n103,4\n"
    )

    assert x_values == [100.0, 103.0]
    assert y_values == [2.0, 4.0]
    assert units == "1/cm"


def test_spectrum_parser_accepts_headered_plain_two_column_csv():
    x_values, y_values, units = main.parse_spectrum_xy(
        "Raman Shift (cm⁻¹),Intensity\n380,12.5\n381,14\n382,13.25\n".encode()
    )

    assert x_values == [380.0, 381.0, 382.0]
    assert y_values == [12.5, 14.0, 13.25]
    assert units == "cm-1"


def test_spectrum_parser_normalizes_descending_plain_spectrum_order():
    x_values, y_values, _ = main.parse_spectrum_xy(
        b"shift\tcounts\n402\t30\n401\t20\n400\t10\n"
    )

    assert x_values == [400.0, 401.0, 402.0]
    assert y_values == [10.0, 20.0, 30.0]


def test_spectrum_parser_normalizes_descending_instrument_xydata_order():
    x_values, y_values, units = main.parse_spectrum_xy(
        b"XUNITS=1/cm\nXYDATA=\n402,30\n401,20\n400,10\n"
    )

    assert x_values == [400.0, 401.0, 402.0]
    assert y_values == [10.0, 20.0, 30.0]
    assert units == "1/cm"


@pytest.mark.parametrize(
    "rows",
    [b"400,10\n400,11\n401,12\n", b"400,10\n402,12\n401,11\n"],
)
def test_spectrum_parser_rejects_ambiguous_instrument_coordinate_grid(rows):
    x_values, y_values, _ = main.parse_spectrum_xy(b"XYDATA=\n" + rows)

    assert x_values == []
    assert y_values == []


def test_spectrum_parser_does_not_treat_nonmonotonic_two_column_table_as_spectrum():
    x_values, y_values, _ = main.parse_spectrum_xy(b"1,10\n3,20\n2,30\n")

    assert x_values == []
    assert y_values == []


def test_spectrum_interpolation_handles_unsorted_targets_and_rejects_duplicate_source_x():
    assert main.interpolate_spectrum([0, 10, 20], [0, 100, 200], [15, 5, -2, 22]) == [150, 50, 0, 200]

    with pytest.raises(ValueError, match="unique"):
        main.interpolate_spectrum([0, 10, 10], [0, 100, 110], [5])


def test_backend_pipeline_subtracts_baseline_before_cosmic_ray_removal():
    x_values = list(range(11))
    raw_values = [10 + 2 * x_value for x_value in x_values]
    raw_values[5] += 100

    baseline, baseline_corrected = main.subtract_baseline_first(x_values, raw_values)
    cleaned, removed = main.remove_single_point_cosmic_rays(baseline_corrected)

    assert baseline == pytest.approx([10 + 2 * x_value for x_value in x_values])
    assert baseline_corrected[5] == pytest.approx(100)
    assert removed == [5]
    assert cleaned == pytest.approx([0] * len(x_values))


def test_list_imported_files_returns_empty_list(isolated_storage):
    response = client.get("/files")

    assert response.status_code == 200
    assert response.json() == []


def test_database_enables_durability_pragmas_and_rejects_orphan_analysis_run(isolated_storage):
    with database.connect_database() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        with pytest.raises(sqlite3.IntegrityError, match="source file does not exist"):
            database.create_analysis_run(
                connection, stored_analysis_run("missing-file", "0" * 64, "orphan-run")
            )


def test_database_enforces_analysis_checksum_and_immutability(isolated_storage):
    imported = post_import("immutable-analysis-source.txt", b"immutable source").json()
    base_run = stored_analysis_run(imported["file_id"], imported["sha256"], "immutable-run")

    with database.connect_database() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="raw checksum does not match"):
            database.create_analysis_run(
                connection,
                stored_analysis_run(imported["file_id"], "0" * 64, "bad-sha"),
            )

        database.create_analysis_run(connection, base_run)
        with pytest.raises(sqlite3.IntegrityError, match="analysis runs are immutable"):
            connection.execute(
                "UPDATE analysis_runs SET app_version = ? WHERE run_id = ?",
                ("tampered", base_run["run_id"]),
            )
        with pytest.raises(sqlite3.IntegrityError, match="analysis runs are immutable"):
            connection.execute("DELETE FROM analysis_runs WHERE run_id = ?", (base_run["run_id"],))
        with pytest.raises(sqlite3.IntegrityError, match="source identity and checksum are immutable"):
            connection.execute(
                "UPDATE imported_files SET sha256 = ? WHERE file_id = ?",
                ("f" * 64, imported["file_id"]),
            )
        with pytest.raises(sqlite3.IntegrityError, match="source records cannot be deleted"):
            connection.execute("DELETE FROM imported_files WHERE file_id = ?", (imported["file_id"],))

        connection.execute(
            "UPDATE imported_files SET operator = ? WHERE file_id = ?",
            ("Updated Operator", imported["file_id"]),
        )

        retained = connection.execute(
            "SELECT raw_sha256, app_version FROM analysis_runs WHERE run_id = ?",
            (base_run["run_id"],),
        ).fetchone()
        retained_source = connection.execute(
            "SELECT sha256, operator FROM imported_files WHERE file_id = ?",
            (imported["file_id"],),
        ).fetchone()

    assert tuple(retained) == (imported["sha256"], main.APP_VERSION)
    assert tuple(retained_source) == (imported["sha256"], "Updated Operator")


def test_database_rejects_malformed_trace_and_inconsistent_analysis_result(isolated_storage):
    imported = post_import("constrained-analysis-source.txt", b"source").json()
    malformed_trace = stored_analysis_run(
        imported["file_id"], imported["sha256"], "malformed-trace",
        derived_trace='{"x":[1,2,3],"y":[1,2]}',
    )
    inconsistent_result = stored_analysis_run(
        imported["file_id"], imported["sha256"], "inconsistent-result"
    )
    decoded_result = json.loads(inconsistent_result["result"])
    decoded_result["file_id"] = "different-file"
    inconsistent_result["result"] = json.dumps(decoded_result)

    with database.connect_database() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="equal x/y arrays"):
            database.create_analysis_run(connection, malformed_trace)
        with pytest.raises(sqlite3.IntegrityError, match="result provenance"):
            database.create_analysis_run(connection, inconsistent_result)


def test_health_check_detects_legacy_analysis_provenance_mismatch(isolated_storage):
    imported = post_import("legacy-provenance.txt", b"source bytes").json()
    with database.connect_database() as connection:
        connection.execute("DROP TRIGGER protect_analyzed_import_identity")
        database.create_analysis_run(
            connection,
            stored_analysis_run(imported["file_id"], imported["sha256"], "legacy-run"),
        )
        connection.execute(
            "UPDATE imported_files SET sha256 = ? WHERE file_id = ?",
            ("f" * 64, imported["file_id"]),
        )

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "analysis_provenance_failed"
    assert response.json()["detail"]["invalid_run_count"] == 1


def test_database_rejects_non_object_json_in_structured_columns(isolated_storage):
    imported = post_import("structured-source.txt", b"source bytes").json()
    with database.connect_database() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="valid JSON object"):
            connection.execute(
                "INSERT INTO presets (preset_id, name, config) VALUES (?, ?, ?)",
                ("invalid-preset", "Invalid", "[]"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="valid JSON object"):
            connection.execute(
                "UPDATE imported_files SET extended_metadata = ? WHERE file_id = ?",
                ("not-json", imported["file_id"]),
            )
        with pytest.raises(sqlite3.IntegrityError, match="valid JSON object"):
            database.create_analysis_run(
                connection,
                stored_analysis_run(
                    imported["file_id"], imported["sha256"], "invalid-result", result="[]"
                ),
            )


def test_legacy_invalid_recipe_json_returns_structured_error_and_fails_health(isolated_storage):
    with database.connect_database() as connection:
        connection.execute("DROP TRIGGER validate_analysis_recipes_config_insert")
        connection.execute(
            "INSERT INTO analysis_recipes (recipe_id, name, config, created_at) VALUES (?, ?, ?, ?)",
            ("legacy-invalid", "Legacy invalid", "{broken", "2026-08-25T00:00:00+00:00"),
        )

    recipes = client.get("/analysis/recipes")
    health = client.get("/health")

    assert recipes.status_code == 409
    assert recipes.json()["detail"]["code"] == "stored_data_invalid"
    assert health.status_code == 503
    assert health.json()["detail"]["code"] == "stored_json_invalid"
    assert health.json()["detail"]["invalid_record_count"] == 1


def test_integrity_endpoint_detects_same_size_raw_file_change(isolated_storage):
    imported = post_import("integrity.txt", b"immutable spectrum bytes", DEFAULT_METADATA).json()

    valid = client.get(f"/files/{imported['file_id']}/integrity")
    assert valid.status_code == 200
    assert valid.json()["valid"] is True
    assert valid.json()["actual_sha256"] == imported["sha256"]

    stored_path = Path(imported["storage_path"])
    stored_path.write_bytes(b"X" * imported["size_bytes"])
    changed = client.get(f"/files/{imported['file_id']}/integrity")

    assert changed.status_code == 200
    assert changed.json()["valid"] is False
    assert changed.json()["actual_sha256"] != imported["sha256"]
    assert changed.json()["expected_size_bytes"] == imported["size_bytes"]
    assert changed.json()["actual_size_bytes"] == imported["size_bytes"]


def test_integrity_endpoint_reports_raw_file_size_change(isolated_storage):
    imported = post_import("integrity-size.txt", b"immutable spectrum bytes", DEFAULT_METADATA).json()
    Path(imported["storage_path"]).write_bytes(b"short")

    response = client.get(f"/files/{imported['file_id']}/integrity")

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["expected_size_bytes"] == imported["size_bytes"]
    assert response.json()["actual_size_bytes"] == 5


def test_analysis_rejects_checksum_changed_raw_source(isolated_storage):
    original = b"XUNITS=1/cm\nXYDATA=\n380,10\n390,20\n400,12\n"
    imported = post_import("analysis-integrity.txt", original, DEFAULT_METADATA).json()
    spectrum = client.get(f"/files/{imported['file_id']}/spectrum").json()
    Path(imported["storage_path"]).write_bytes(b"X" * len(original))

    response = client.post(
        f"/files/{imported['file_id']}/analysis",
        json={"x": spectrum["x"], "y": spectrum["y"], "processing_config": {}},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "raw_file_checksum_mismatch"
    with database.connect_database() as connection:
        assert connection.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0


def test_spectrum_access_rejects_raw_file_size_change(isolated_storage):
    imported = post_import(
        "size-check.txt",
        b"XUNITS=1/cm\nXYDATA=\n100,1\n101,2\n",
        DEFAULT_METADATA,
    ).json()
    Path(imported["storage_path"]).write_bytes(b"short")

    response = client.get(f"/files/{imported['file_id']}/spectrum")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "raw_file_size_mismatch"


def test_spectrum_access_rejects_same_size_checksum_change(isolated_storage):
    original = b"XUNITS=1/cm\nXYDATA=\n100,1\n101,2\n"
    imported = post_import("checksum-check.txt", original, DEFAULT_METADATA).json()
    Path(imported["storage_path"]).write_bytes(b"X" * len(original))

    response = client.get(f"/files/{imported['file_id']}/spectrum")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "raw_file_checksum_mismatch"


def test_operator_list_uses_most_recent_operator_as_default_order(isolated_storage):
    first = {**DEFAULT_METADATA, "operator": "First Operator"}
    second = {**DEFAULT_METADATA, "operator": "Latest Operator"}
    assert post_import("first.txt", b"first operator data", first).status_code == 201
    assert post_import("second.txt", b"second operator data", second).status_code == 201

    response = client.get("/operators")

    assert response.status_code == 200
    assert response.json() == ["Latest Operator", "First Operator"]


def test_metadata_edit_rejects_wrong_types_and_unknown_experimental_fields(isolated_storage):
    imported = post_import("typed-metadata.txt", b"immutable", DEFAULT_METADATA).json()
    base = {**DEFAULT_METADATA, "experimental_details": {}}

    wrong_type = client.patch(
        f"/files/{imported['file_id']}",
        json={**base, "instrument": {"model": "not text"}},
    )
    unknown_detail = client.patch(
        f"/files/{imported['file_id']}",
        json={**base, "experimental_details": {"laser_colour": "green"}},
    )

    assert wrong_type.status_code == 422
    assert wrong_type.json()["detail"]["code"] == "invalid_metadata_type"
    assert unknown_detail.status_code == 422
    assert unknown_detail.json()["detail"]["code"] == "unknown_experimental_detail"


def test_presets_and_recipes_use_same_bounded_finite_config_contract(isolated_storage):
    valid = client.post(
        "/presets",
        json={"name": "Valid", "config": {"laser": "532", "enabled": True, "power": 1.5}},
    )
    nested = client.post(
        "/presets",
        json={"name": "Nested", "config": {"unsupported": [1, 2]}},
    )
    non_finite = client.post(
        "/analysis/recipes",
        content='{"name":"Bad number","config":{"threshold":NaN}}',
        headers={"Content-Type": "application/json"},
    )
    oversized = client.post(
        "/analysis/recipes",
        json={"name": "Too large", "config": {"notes": "x" * 100001}},
    )

    assert valid.status_code == 201
    assert nested.status_code == 422
    assert nested.json()["detail"]["code"] == "invalid_preset_config"
    assert non_finite.status_code == 422
    assert non_finite.json()["detail"]["code"] == "invalid_recipe_config"
    assert oversized.status_code == 422
    assert oversized.json()["detail"]["code"] == "recipe_config_too_large"


def test_sample_creation_rejects_non_text_metadata_without_server_error(isolated_storage):
    response = client.post("/samples", json={"sample_id": {"unexpected": "object"}})

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_metadata_type"


def test_list_and_retrieve_imported_file_metadata(isolated_storage):
    older_file = {
        "file_id": "11111111-1111-1111-1111-111111111111",
        "original_filename": "older.csv",
        "content_type": "text/csv",
        "size_bytes": 12,
        "sha256": "a" * 64,
        "storage_path": "raw/older.csv",
        "imported_at": "2026-07-28T01:02:03+00:00",
        "technique": "Raman",
        "sample_id": "SAMPLE-OLD",
        "measurement_date": "2026-07-27",
        "instrument": None,
        "operator": None,
        "notes": None,
        "material_system": None,
        "experimental_details": {},
    }
    newer_file = {
        "file_id": "22222222-2222-2222-2222-222222222222",
        "original_filename": "newer.bin",
        "content_type": "application/octet-stream",
        "size_bytes": 256,
        "sha256": "b" * 64,
        "storage_path": "raw/newer.bin",
        "imported_at": "2026-07-29T04:05:06+00:00",
        "technique": "XRD",
        "sample_id": "SAMPLE-NEW",
        "measurement_date": None,
        "instrument": "Diffractometer",
        "operator": "A. Researcher",
        "notes": "Baseline scan",
        "material_system": None,
        "experimental_details": {},
    }

    with database.connect_database() as connection:
        database.insert_imported_file(connection, newer_file)
        database.insert_imported_file(connection, older_file)

    list_response = client.get("/files")
    retrieve_response = client.get(f"/files/{older_file['file_id']}")

    assert list_response.status_code == 200
    assert list_response.json() == [newer_file, older_file]
    assert retrieve_response.status_code == 200
    assert retrieve_response.json() == older_file


def test_retrieve_imported_file_returns_clear_not_found(isolated_storage):
    response = client.get("/files/missing-id")

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "file_not_found",
        "message": "No imported file has the requested ID.",
    }


def test_retrieve_imported_raman_spectrum_for_plot(isolated_storage):
    spectrum_bytes = b"\n".join(
        [
            b"FILETYPE=RAMAN SPECTRUM",
            b"XUNITS=1/cm",
            b"XYDATA=",
            b"100.0,10.5",
            b"101.5,22.0",
            b"103.0,14.25",
        ]
    )
    import_response = post_import("spectrum.txt", spectrum_bytes)

    response = client.get(
        f"/files/{import_response.json()['file_id']}/spectrum"
    )

    assert response.status_code == 200
    assert response.json() == {
        "file_id": import_response.json()["file_id"],
        "filename": "spectrum.txt",
        "x": [100.0, 101.5, 103.0],
        "y": [10.5, 22.0, 14.25],
        "x_units": "1/cm",
        "point_count": 3,
    }


def test_substrate_model_fits_reference_mixture_to_each_target(isolated_storage):
    x_values = list(range(100, 701, 2))

    def reference_values(index):
        si_position = 518.5 + index
        shoulder_position = 285 + 12 * index
        return [
            25
            + (900 + 70 * index) * math.exp(-((x_value - si_position) ** 2) / 45)
            + (180 + 25 * index) * math.exp(-((x_value - shoulder_position) ** 2) / 700)
            for x_value in x_values
        ]

    def spectrum_bytes(label, values):
        rows = [f"COMMENT={label}", "XUNITS=RamanShift", "XYDATA="]
        rows.extend(f"{x_value},{value:.8f}" for x_value, value in zip(x_values, values))
        return "\n".join(rows).encode()

    reference_sets = [reference_values(index) for index in range(5)]
    reference_metadata = {**DEFAULT_METADATA, "sample_id": "SUBSTRATE", "material_system": "SiO2/Si"}
    for index, values in enumerate(reference_sets):
        response = post_import(f"SiO2_ref_{index + 1}.txt", spectrum_bytes(f"reference-{index}", values), reference_metadata)
        assert response.status_code == 201

    partial_indexes = [index for index, x_value in enumerate(x_values) if 300 <= x_value <= 600]
    partial_x = [x_values[index] for index in partial_indexes]
    partial_values = [reference_sets[0][index] for index in partial_indexes]
    partial_rows = ["COMMENT=partial-reference", "XUNITS=RamanShift", "XYDATA="]
    partial_rows.extend(
        f"{x_value},{value:.8f}" for x_value, value in zip(partial_x, partial_values)
    )
    assert post_import(
        "SiO2_partial_ref.txt", "\n".join(partial_rows).encode(), reference_metadata
    ).status_code == 201

    target_values = [
        0.82 * reference_sets[0][index]
        + 0.18 * reference_sets[4][index]
        + 520 * math.exp(-((x_value - 384) ** 2) / 22)
        + 680 * math.exp(-((x_value - 405) ** 2) / 22)
        for index, x_value in enumerate(x_values)
    ]
    target = post_import(
        "T9_MoS2-01.txt",
        spectrum_bytes("target", target_values),
        {**DEFAULT_METADATA, "sample_id": "T9", "material_system": "MoS2/SiO2/Si"},
    ).json()

    response = client.get(f"/files/{target['file_id']}/substrate-model")

    assert response.status_code == 200
    model = response.json()
    assert model["method"] == "target-adaptive SiO2/Si reference fit followed by a fitted MoS2-only reconstruction"
    assert model["reference_fit_method"] == "target-adaptive non-negative mixture of SiO2/Si reference spectra with material-peak protection"
    assert len(model["reference_files"]) == 5
    assert len(model["reference_weights"]) == 5
    assert len(model["reference_alignment_shifts"]) == 5
    assert model["model_version"] == main.SUBSTRATE_MODEL_VERSION
    assert len(model["reference_sources"]) == 5
    assert all(len(source["sha256"]) == 64 for source in model["reference_sources"])
    assert {source["filename"] for source in model["reference_sources"]} == set(model["reference_files"])
    assert model["excluded_references"] == [{
        "filename": "SiO2_partial_ref.txt",
        "reason": "Reference covers less than 95% of the target spectral range after alignment.",
        "range": pytest.approx([300.0, 600.0], abs=2.0),
        "coverage_fraction": pytest.approx(0.5, abs=0.01),
    }]
    assert isinstance(model["local_adjustments"], list)
    assert sum(model["reference_weights"].values()) == pytest.approx(1.0, abs=1e-5)
    assert max(model["reference_weights"].values()) > min(model["reference_weights"].values())
    assert model["fit_point_count"] > 10
    assert model["unsupported_structure_points_smoothed"] >= 0
    assert len(model["substrate_y"]) == len(target_values)
    assert all(substrate <= target + 1e-8 for substrate, target in zip(model["substrate_y"], target_values))
    target_span = max(target_values) - min(target_values)
    assert model["noise"]["substrate"] <= max(model["noise"]["target"] + 1e-6, target_span * 1e-4)
    assert model["silicon_peak_match"]["target_position"] is not None
    assert model["silicon_peak_match"]["maximum_matched"] is True
    assert "quiet_tail" in model
    if model["quiet_tail"]["applied"]:
        assert model["quiet_tail"]["method"] == "constrained linear quiet-tail baseline"
    assert isinstance(model["quiet_ranges"], list)
    si_indexes = [index for index, value in enumerate(model["x"]) if 500 <= value <= 540]
    assert max(model["substrate_y"][index] for index in si_indexes) == pytest.approx(
        max(model["artifact_cleaned_y"][index] for index in si_indexes), abs=1e-6
    )
    assert model["processing_pipeline"][0]["stage"] == "rubber-band baseline detection and subtraction"
    assert model["processing_pipeline"][1]["stage"] == "isolated cosmic-ray detection and subtraction"
    assert model["processing_pipeline"][2]["stage"] == "target-adaptive SiO2/Si detection and subtraction"
    assert model["reference_ensemble"]["reference_count"] == 5
    assert len(model["reference_ensemble"]["ensemble_id"]) == 64
    assert len(model["reference_features"]) == 5
    assert model["reference_ensemble"]["silicon_peak_position"]["count"] == 5
    assert model["reference_ensemble"]["silicon_peak_absolute_height"]["maximum"] > model["reference_ensemble"]["silicon_peak_absolute_height"]["minimum"]
    assert [region["assignment"] for region in model["protected_regions"]] == ["MoS2 E mode", "MoS2 A1 mode"]
    corrected_e = main.calculate_peak_metrics(model["x"], model["corrected_y"], 365, 395)
    corrected_a = main.calculate_peak_metrics(model["x"], model["corrected_y"], 395, 425)
    assert corrected_e["height"] > 300
    assert corrected_a["height"] > 400
    assert model["material_isolation"]["applied"] is True
    isolation_regions = model["material_isolation"]["regions"]
    assert all(
        value == pytest.approx(0.0, abs=1e-8)
        for x_value, value in zip(model["x"], model["corrected_y"])
        if not any(region["lower"] <= x_value <= region["upper"] for region in isolation_regions)
    )

    feedback_response = client.post(
        f"/files/{target['file_id']}/substrate-residual-feedback",
        json={"action": "remove", "center": 520.0, "half_width": 8.0},
    )
    assert feedback_response.status_code == 200
    assert feedback_response.json()["learned"] is True
    learned_model = client.get(f"/files/{target['file_id']}/substrate-model").json()
    assert learned_model["learned_residual_count"] == 1
    assert max(learned_model["corrected_y"][index] for index in si_indexes) == 0
    assert database.list_substrate_peak_feedback("mos2sio2si")[0]["action"] == "remove"


def test_each_sio2_si_reference_subtracts_to_zero_in_leave_one_out_model(isolated_storage):
    """A confirmed pure substrate must contain no sample residual after subtraction."""
    x_values = list(range(100, 701, 2))

    def reference_values(index):
        return [
            15 + 0.01 * x_value
            + (850 + 45 * index) * math.exp(-0.5 * ((x_value - (519 + 0.5 * index)) / 3.2) ** 2)
            + (130 + 12 * index) * math.exp(-0.5 * ((x_value - (300 + 4 * index)) / 18) ** 2)
            for x_value in x_values
        ]

    imported = []
    metadata = {**DEFAULT_METADATA, "sample_id": "SUBSTRATE", "material_system": "SiO2/Si"}
    for index in range(5):
        rows = [f"COMMENT=leave-one-out-{index}", "XUNITS=RamanShift", "XYDATA="]
        rows.extend(
            f"{x_value},{intensity:.10f}"
            for x_value, intensity in zip(x_values, reference_values(index))
        )
        response = post_import(f"SiO2_LOO_{index + 1}.txt", "\n".join(rows).encode(), metadata)
        assert response.status_code == 201
        imported.append(response.json())

    for target in imported:
        response = client.get(f"/files/{target['file_id']}/substrate-model")
        assert response.status_code == 200
        model = response.json()
        assert len(model["reference_sources"]) == 4
        assert target["file_id"] not in {source["file_id"] for source in model["reference_sources"]}
        assert model["substrate_only_validation"]["applied"] is True
        assert model["substrate_only_validation"]["leave_one_out_reference_count"] == 4
        assert model["substrate_only_validation"]["pre_enforcement_residual_rms"] >= 0
        assert model["substrate_only_validation"]["pre_enforcement_relative_rms"] >= 0
        assert isinstance(model["substrate_only_validation"]["model_fit_passed"], bool)
        assert model["substrate_only_validation"]["model_fit_relative_rms_tolerance"] == 0.12
        assert model["substrate_only_validation"]["final_zero_residual_tolerance"] == 1e-10
        assert model["substrate_only_validation"]["final_zero_residual_passed"] is True
        assert model["substrate_only_validation"]["final_zero_residual_max_abs"] == 0
        assert model["method"] == "leave-one-out SiO2/Si fit with confirmed substrate-only zero-residual invariant"
        assert max(abs(value) for value in model["corrected_y"]) <= 1e-10


def test_substrate_model_excludes_references_with_incompatible_x_units(isolated_storage):
    x_values = list(range(100, 701, 2))
    values = [
        20 + 900 * math.exp(-0.5 * ((x_value - 520) / 4) ** 2)
        for x_value in x_values
    ]

    def encoded_spectrum(units, scale=1.0):
        rows = [f"XUNITS={units}", "XYDATA="]
        rows.extend(
            f"{x_value},{scale * value:.8f}"
            for x_value, value in zip(x_values, values)
        )
        return "\n".join(rows).encode()

    reference_metadata = {
        **DEFAULT_METADATA,
        "sample_id": "SUBSTRATE",
        "material_system": "SiO2/Si",
    }
    valid = post_import(
        "valid-reference.txt", encoded_spectrum("cm^-1"), reference_metadata
    ).json()
    incompatible = post_import(
        "wavelength-reference.txt", encoded_spectrum("nm"), reference_metadata
    ).json()
    target = post_import(
        "unit-safe-target.txt",
        encoded_spectrum("RamanShift", 0.8),
        {**DEFAULT_METADATA, "material_system": "MoS2/SiO2/Si"},
    ).json()

    response = client.get(f"/files/{target['file_id']}/substrate-model")

    assert response.status_code == 200
    model = response.json()
    assert {source["file_id"] for source in model["reference_sources"]} == {valid["file_id"]}
    excluded = next(
        item for item in model["excluded_references"]
        if item["filename"] == "wavelength-reference.txt"
    )
    assert excluded["target_units"] == "cm-1"
    assert excluded["reference_units"] == "nm"
    assert incompatible["file_id"] not in {
        source["file_id"] for source in model["reference_sources"]
    }


def test_new_sio2_si_reference_updates_ensemble_fingerprint_and_variability(isolated_storage):
    x_values = list(range(100, 701, 2))

    def encoded(index, height, position):
        values = [
            12 + 0.015 * x_value
            + height * math.exp(-0.5 * ((x_value - position) / 4.5) ** 2)
            + (100 + 8 * index) * math.exp(-0.5 * ((x_value - (300 + index)) / 20) ** 2)
            for x_value in x_values
        ]
        rows = [f"COMMENT=incremental-{index}", "XUNITS=RamanShift", "XYDATA="]
        rows.extend(f"{x_value},{value:.8f}" for x_value, value in zip(x_values, values))
        return "\n".join(rows).encode(), values

    metadata = {**DEFAULT_METADATA, "sample_id": "SUBSTRATE", "material_system": "SiO2/Si"}
    reference_values = []
    for index in range(5):
        content, values = encoded(index, 800 + 60 * index, 519 + 0.5 * index)
        assert post_import(f"incremental-ref-{index}.txt", content, metadata).status_code == 201
        reference_values.append(values)
    target_rows = ["XUNITS=RamanShift", "XYDATA="]
    target_rows.extend(
        f"{x_value},{reference_values[2][index] + 300 * math.exp(-0.5 * ((x_value - 386) / 3) ** 2) + 420 * math.exp(-0.5 * ((x_value - 410) / 3) ** 2):.8f}"
        for index, x_value in enumerate(x_values)
    )
    target = post_import(
        "incremental-target.txt", "\n".join(target_rows).encode(),
        {**DEFAULT_METADATA, "material_system": "MoS2/SiO2/Si"},
    ).json()

    before = client.get(f"/files/{target['file_id']}/substrate-model").json()
    sixth_content, _ = encoded(6, 1500, 523)
    sixth = post_import("incremental-ref-6.txt", sixth_content, metadata).json()
    after_response = client.get(f"/files/{target['file_id']}/substrate-model")

    assert after_response.status_code == 200
    after = after_response.json()
    assert before["reference_ensemble"]["reference_count"] == 5
    assert after["reference_ensemble"]["reference_count"] == 6
    assert before["reference_ensemble"]["ensemble_id"] != after["reference_ensemble"]["ensemble_id"]
    assert sixth["file_id"] in {feature["file_id"] for feature in after["reference_features"]}
    assert after["reference_ensemble"]["silicon_peak_absolute_height"]["maximum"] > before["reference_ensemble"]["silicon_peak_absolute_height"]["maximum"]
    assert after["reference_ensemble"]["silicon_peak_position"]["maximum"] == pytest.approx(523, abs=0.1)


def test_substrate_model_reports_unit_exclusions_when_no_reference_is_compatible(isolated_storage):
    rows = ["XUNITS=nm", "XYDATA=", "500,1", "510,2", "520,10", "530,2", "540,1"]
    reference = post_import(
        "only-wavelength-reference.txt",
        "\n".join(rows).encode(),
        {**DEFAULT_METADATA, "material_system": "SiO2/Si"},
    )
    assert reference.status_code == 201
    target = post_import(
        "raman-target.txt",
        b"XUNITS=cm^-1\nXYDATA=\n500,1\n510,2\n520,10\n530,2\n540,1\n",
        {**DEFAULT_METADATA, "material_system": "MoS2/SiO2/Si"},
    ).json()

    response = client.get(f"/files/{target['file_id']}/substrate-model")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "no_substrate_references"
    assert detail["excluded_references"][0]["reference_units"] == "nm"
    assert detail["excluded_references"][0]["target_units"] == "cm-1"


@pytest.mark.parametrize(
    ("center", "half_width", "expected_code"),
    [
        ("nan", 8, "invalid_residual_peak"),
        (520, "inf", "invalid_residual_peak"),
        (900, 8, "residual_peak_out_of_range"),
    ],
)
def test_substrate_feedback_rejects_nonfinite_or_out_of_range_peak(
    isolated_storage, center, half_width, expected_code
):
    rows = ["XUNITS=1/cm", "XYDATA="] + [f"{x_value},10" for x_value in range(100, 701)]
    imported = post_import(
        "feedback-range.txt",
        "\n".join(rows).encode(),
        {**DEFAULT_METADATA, "material_system": "MoS2/SiO2/Si"},
    ).json()

    response = client.post(
        f"/files/{imported['file_id']}/substrate-residual-feedback",
        json={"action": "remove", "center": center, "half_width": half_width},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == expected_code
    assert database.list_substrate_peak_feedback() == []


def test_retrieve_imported_image_content_for_preview(isolated_storage):
    image_bytes = b"\xff\xd8\xff\xe0immutable-jpeg-bytes\xff\xd9"
    import_response = client.post(
        "/files/import",
        files={"file": ("micrograph.jpg", image_bytes, "image/jpeg")},
        data=DEFAULT_METADATA,
    )

    response = client.get(
        f"/files/{import_response.json()['file_id']}/content"
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == image_bytes


def test_imported_html_cannot_be_served_as_same_origin_active_content(isolated_storage):
    imported = client.post(
        "/files/import",
        files={"file": ("payload.html", b"<script>document.cookie='stolen'</script>", "image/png")},
        data=DEFAULT_METADATA,
    ).json()

    response = client.get(f"/files/{imported['file_id']}/content")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"


def test_retrieve_spectrum_rejects_file_without_xy_data(isolated_storage):
    import_response = post_import("image.jpg", b"not spectrum data")

    response = client.get(
        f"/files/{import_response.json()['file_id']}/spectrum"
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "spectrum_unavailable"


def test_raman_analysis_reports_peaks_calibration_qc_and_repeatability(isolated_storage):
    def spectrum_bytes(e_position, a_position, si_position=None):
        rows = ["FILETYPE=RAMAN SPECTRUM", "XUNITS=1/cm", "XYDATA="]
        for x_value in range(350, 551):
            intensity = 100.0
            intensity += 800 * math.exp(-((x_value - e_position) ** 2) / 8)
            intensity += 1000 * math.exp(-((x_value - a_position) ** 2) / 8)
            if si_position is not None:
                intensity += 1500 * math.exp(-((x_value - si_position) ** 2) / 5)
            rows.append(f"{x_value},{intensity}")
        return "\n".join(rows).encode()

    first = post_import("A01_spectrum1.txt", spectrum_bytes(384, 404)).json()
    second = post_import("A01_spectrum2.txt", spectrum_bytes(384.2, 404.3)).json()
    reference = post_import(
        "SiO2_Ref.txt",
        spectrum_bytes(370, 430, 519.7),
        {**DEFAULT_METADATA, "sample_id": "REFERENCE"},
    ).json()

    response = client.get(
        f"/files/{first['file_id']}/analysis",
        params={"reference_file_id": reference["file_id"]},
    )
    summary = client.get(f"/samples/{DEFAULT_METADATA['sample_id']}/raman-summary")

    assert response.status_code == 200
    analysis = response.json()
    assert analysis["peaks"]["E_mode"]["position"] == pytest.approx(385.0, abs=0.2)
    assert analysis["peaks"]["A1_mode"]["position"] == pytest.approx(405.0, abs=0.2)
    assert analysis["peak_separation"] == pytest.approx(20.0, abs=0.2)
    assert analysis["layer_estimate"] == "monolayer-like"
    assert analysis["calibration"]["applied_shift"] == pytest.approx(1.0, abs=0.2)
    assert analysis["calibration"]["observed_position_uncertainty"] > 0
    assert analysis["sha256"] == first["sha256"]
    assert analysis["quality"]["point_count"] == 201
    assert analysis["interpretation"]["eligible"] is True
    assert analysis["peak_separation_uncertainty"] > 0
    assert analysis["peaks"]["E_mode"]["position_uncertainty"] > 0
    assert analysis["uncertainty"]["includes_calibration_systematics"] is False
    assert summary.status_code == 200
    assert summary.json()["spectrum_count"] == 2
    assert summary.json()["separation_stdev"] is not None


def test_peak_metrics_interpolate_fwhm_and_report_position_uncertainty():
    x_values = [360 + index * 0.25 for index in range(201)]
    sigma = 3.0
    y_values = [100 * math.exp(-0.5 * ((x_value - 385.2) / sigma) ** 2) for x_value in x_values]

    peak = main.calculate_peak_metrics(x_values, y_values, 365, 395)

    assert peak["position"] == pytest.approx(385.2, abs=0.03)
    assert peak["fwhm"] == pytest.approx(2.35482 * sigma, abs=0.1)
    assert 0 < peak["position_uncertainty"] < 0.5


def test_peak_metrics_are_robust_to_single_window_edge_spike():
    x_values = [365 + index * 0.5 for index in range(61)]
    y_values = [
        20 + 100 * math.exp(-0.5 * ((x_value - 385.2) / 2.0) ** 2)
        for x_value in x_values
    ]
    y_values[0] = 1000

    peak = main.calculate_peak_metrics(x_values, y_values, 365, 395)

    assert peak is not None
    assert peak["position"] == pytest.approx(385.2, abs=0.1)
    assert peak["height"] == pytest.approx(100, abs=3)


def test_layer_interpretation_is_withheld_for_under_resolved_peaks():
    x_values = list(range(360, 431, 5))
    y_values = [
        2
        + 100 * math.exp(-((x_value - 385) ** 2) / 5)
        + 120 * math.exp(-((x_value - 405) ** 2) / 5)
        for x_value in x_values
    ]

    analysis = main.analyze_raman_spectrum(x_values, y_values)

    assert analysis["peak_separation"] is not None
    assert analysis["layer_estimate"] is None
    assert analysis["interpretation"]["eligible"] is False
    assert any("sampled by fewer" in reason for reason in analysis["interpretation"]["reasons"])
    assert analysis["quality"]["badge"] in {"Review", "Poor"}
    assert "detector saturation" not in " ".join(analysis["quality"]["warnings"])


def test_layer_interpretation_is_withheld_at_uncertain_category_boundary():
    x_values = [360 + index * 0.5 for index in range(141)]
    y_values = [
        2
        + 100 * math.exp(-0.5 * ((x_value - 384) / 2.0) ** 2)
        + 120 * math.exp(-0.5 * ((x_value - 405) / 2.0) ** 2)
        + 0.05 * math.sin(index * 1.7)
        for index, x_value in enumerate(x_values)
    ]

    analysis = main.analyze_raman_spectrum(x_values, y_values)

    assert analysis["peak_separation"] == pytest.approx(21.0, abs=0.1)
    assert analysis["peak_separation_uncertainty"] > 0
    assert analysis["layer_estimate"] is None
    assert analysis["interpretation"]["eligible"] is False
    assert any("layer-category boundary" in reason for reason in analysis["interpretation"]["reasons"])


def test_saturation_warning_requires_three_point_consecutive_flat_top():
    x_values = list(range(350, 451))
    base = [
        1
        + 80 * math.exp(-0.5 * ((x_value - 384) / 2) ** 2)
        + 70 * math.exp(-0.5 * ((x_value - 404) / 2) ** 2)
        for x_value in x_values
    ]
    two_point = base[:]
    two_point[33:35] = [100, 100]
    three_point = base[:]
    three_point[33:36] = [100, 100, 100]

    two_analysis = main.analyze_raman_spectrum(x_values, two_point)
    three_analysis = main.analyze_raman_spectrum(x_values, three_point)

    assert not any("saturation" in warning for warning in two_analysis["quality"]["warnings"])
    assert any("saturation" in warning for warning in three_analysis["quality"]["warnings"])
    assert two_analysis["quality"]["maximum_plateau_points"] == 2
    assert three_analysis["quality"]["maximum_plateau_points"] == 3


def test_calibration_with_unresolved_reference_peak_does_not_shift_spectrum(isolated_storage):
    rows = ["XUNITS=1/cm", "XYDATA="]
    target_values = []
    for x_value in range(350, 551):
        intensity = 2 + 100 * math.exp(-0.5 * ((x_value - 384) / 2) ** 2)
        intensity += 120 * math.exp(-0.5 * ((x_value - 404) / 2) ** 2)
        target_values.append(intensity)
        rows.append(f"{x_value},{intensity}")
    target = post_import("target-no-calibration.txt", "\n".join(rows).encode()).json()
    reference_rows = ["XUNITS=1/cm", "XYDATA="] + [f"{x_value},10" for x_value in range(350, 551)]
    reference = post_import("flat-reference.txt", "\n".join(reference_rows).encode()).json()

    response = client.get(
        f"/files/{target['file_id']}/analysis",
        params={"reference_file_id": reference["file_id"]},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["peaks"]["E_mode"]["position"] == pytest.approx(384, abs=0.1)
    assert "applied_shift" not in result["calibration"]
    assert "no calibration shift was applied" in result["calibration"]["warning"]


def test_mos2_mode_assignment_is_suppressed_for_explicit_non_mos2_material():
    x_values = list(range(350, 451))
    y_values = [
        2
        + 100 * math.exp(-0.5 * ((x_value - 384) / 2) ** 2)
        + 120 * math.exp(-0.5 * ((x_value - 404) / 2) ** 2)
        for x_value in x_values
    ]

    analysis = main.analyze_raman_spectrum(x_values, y_values, "Crystalline silicon")

    assert analysis["peaks"] == {"E_mode": None, "A1_mode": None}
    assert analysis["peak_separation"] is None
    assert analysis["layer_estimate"] is None
    assert analysis["mode_assignment_status"] == "not attempted for non-MoS2 material"
    assert analysis["quality"]["badge"] == "Good"
    assert any("non-MoS2" in reason for reason in analysis["interpretation"]["reasons"])


def test_processed_analysis_uses_displayed_trace_and_records_provenance(isolated_storage):
    x_values = list(range(360, 431))
    y_values = [
        3
        + 90 * math.exp(-((x_value - 385) ** 2) / 8)
        + 130 * math.exp(-((x_value - 408) ** 2) / 8)
        for x_value in x_values
    ]
    raw_rows = ["XUNITS=1/cm", "XYDATA="] + [f"{x_value},1" for x_value in x_values]
    imported = post_import(
        "processed-source.txt",
        "\n".join(raw_rows).encode(),
        DEFAULT_METADATA,
    ).json()

    response = client.post(
        f"/files/{imported['file_id']}/analysis",
        json={
            "x": x_values,
            "y": y_values,
            "processing_config": {
                **BASELINE_FIRST_CONFIG,
                "baseline_method": "als",
                "subtract_reference": True,
            },
        },
    )

    assert response.status_code == 200
    analysis = response.json()
    assert analysis["analysis_input"] == "processed trace"
    assert analysis["processing_config"]["baseline_method"] == "als"
    assert analysis["raw_data_modified"] is False
    assert analysis["sha256"] == imported["sha256"]
    assert len(analysis["derived_sha256"]) == 64
    assert analysis["app_version"] == main.APP_VERSION
    assert analysis["peaks"]["E_mode"]["position"] == pytest.approx(385, abs=0.2)
    assert analysis["peaks"]["A1_mode"]["position"] == pytest.approx(408, abs=0.2)
    history = client.get(f"/files/{imported['file_id']}/analysis-runs")
    assert history.status_code == 200
    assert history.json()[0]["run_id"] == analysis["run_id"]
    assert history.json()[0]["derived_sha256"] == analysis["derived_sha256"]
    assert history.json()[0]["processing_config"]["baseline_method"] == "als"
    assert history.json()[0]["derived_trace_verification"] == "verified"
    assert "derived_trace" not in history.json()[0]

    trace_response = client.get(f"/analysis-runs/{analysis['run_id']}/trace")
    assert trace_response.status_code == 200
    trace = trace_response.json()
    assert trace["verification"] == "verified"
    assert trace["derived_sha256"] == analysis["derived_sha256"]
    assert trace["x"] == [float(value) for value in x_values]
    assert trace["y"] == pytest.approx(y_values)


def test_health_and_run_access_detect_derived_trace_checksum_corruption(isolated_storage):
    imported = post_import(
        "derived-corruption.txt",
        b"XYDATA=\n1,1\n2,2\n3,3\n",
        DEFAULT_METADATA,
    ).json()
    analysis = client.post(
        f"/files/{imported['file_id']}/analysis",
        json={"x": [1, 2, 3], "y": [1, 2, 3], "processing_config": BASELINE_FIRST_CONFIG},
    ).json()
    with database.connect_database() as connection:
        connection.execute("DROP TRIGGER reject_analysis_run_update")
        connection.execute(
            "UPDATE analysis_runs SET derived_trace = ? WHERE run_id = ?",
            ('{"x":[1.0,2.0,3.0],"y":[1.0,2.0,4.0]}', analysis["run_id"]),
        )

    history = client.get(f"/files/{imported['file_id']}/analysis-runs")
    health = client.get("/health")

    assert history.status_code == 409
    assert history.json()["detail"]["code"] == "stored_data_invalid"
    assert health.status_code == 503
    assert health.json()["detail"]["code"] == "analysis_run_integrity_failed"
    assert health.json()["detail"]["invalid_run_count"] == 1


def test_processed_analysis_rejects_x_grid_from_another_source(isolated_storage):
    imported = post_import(
        "grid-source.txt",
        b"XUNITS=1/cm\nXYDATA=\n100,1\n101,2\n102,3\n",
        DEFAULT_METADATA,
    ).json()

    response = client.post(
        f"/files/{imported['file_id']}/analysis",
        json={"x": [200, 201, 202], "y": [1, 2, 3], "processing_config": {}},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "analysis_source_mismatch"
    assert client.get(f"/files/{imported['file_id']}/analysis-runs").json() == []


@pytest.mark.parametrize(
    "config",
    [
        {},
        {**BASELINE_FIRST_CONFIG, "subtract_baseline": False},
        {**BASELINE_FIRST_CONFIG, "baseline_method": "none"},
        {**BASELINE_FIRST_CONFIG, "pipeline_order": "cosmic_rays > baseline"},
    ],
)
def test_processed_analysis_enforces_baseline_first_pipeline(isolated_storage, config):
    imported = post_import(
        "pipeline-source.txt", b"XYDATA=\n1,1\n2,2\n3,3\n", DEFAULT_METADATA
    ).json()

    response = client.post(
        f"/files/{imported['file_id']}/analysis",
        json={"x": [1, 2, 3], "y": [1, 2, 3], "processing_config": config},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_processing_pipeline"
    assert client.get(f"/files/{imported['file_id']}/analysis-runs").json() == []


def test_repeatability_uses_only_method_matched_analysis_runs(isolated_storage):
    x_values = list(range(360, 431))
    raw = "\n".join(["XUNITS=1/cm", "XYDATA="] + [f"{x_value},1" for x_value in x_values]).encode()
    first = post_import("repeat-1.txt", raw, DEFAULT_METADATA).json()
    second = post_import("repeat-2.txt", raw + b"\n", DEFAULT_METADATA).json()
    post_import("repeat-unprocessed.txt", raw + b"\n\n", DEFAULT_METADATA)
    config = {
        **BASELINE_FIRST_CONFIG,
        "baseline_method": "als",
        "smoothing_method": "moving-average",
    }

    def processed_values(e_position, a_position):
        return [
            2
            + 100 * math.exp(-((x_value - e_position) ** 2) / 8)
            + 140 * math.exp(-((x_value - a_position) ** 2) / 8)
            for x_value in x_values
        ]

    first_run = client.post(
        f"/files/{first['file_id']}/analysis",
        json={"x": x_values, "y": processed_values(385, 408), "processing_config": config},
    ).json()
    client.post(
        f"/files/{second['file_id']}/analysis",
        json={"x": x_values, "y": processed_values(386, 410), "processing_config": config},
    )

    response = client.get(
        f"/samples/{DEFAULT_METADATA['sample_id']}/raman-summary",
        params={"analysis_run_id": first_run["run_id"]},
    )

    assert response.status_code == 200
    summary = response.json()
    assert summary["spectrum_count"] == 2
    assert summary["excluded_file_count"] == 1
    assert summary["comparison_basis"]["analysis_input"] == "processed trace"
    assert summary["comparison_basis"]["processing_config"] == config
    assert all(item["analysis_run_id"] for item in summary["spectra"])


def test_processed_analysis_rejects_non_finite_or_unsorted_coordinates(isolated_storage):
    imported = post_import("source.txt", b"immutable", DEFAULT_METADATA).json()

    non_finite = client.post(
        f"/files/{imported['file_id']}/analysis",
        json={"x": [1, 2, 3], "y": [1, "NaN", 2], "processing_config": {}},
    )
    unsorted = client.post(
        f"/files/{imported['file_id']}/analysis",
        json={"x": [1, 3, 2], "y": [1, 2, 3], "processing_config": {}},
    )

    assert non_finite.status_code == 422
    assert non_finite.json()["detail"]["code"] == "invalid_analysis_data"
    assert unsorted.status_code == 422
    assert unsorted.json()["detail"]["code"] == "invalid_analysis_data"


@pytest.mark.parametrize(
    ("x_values", "y_values"),
    [
        ([1, 2, True], [1, 2, 3]),
        ([1, 2, 3], [1, False, 3]),
        (["1", "2", "3"], [1, 2, 3]),
        ([1, 2, 3], ["1", "2", "3"]),
    ],
)
def test_analysis_rejects_booleans_and_numeric_text(isolated_storage, x_values, y_values):
    imported = post_import("strict-numbers.txt", b"XYDATA=\n1,1\n2,2\n3,3\n").json()

    response = client.post(
        f"/files/{imported['file_id']}/analysis",
        json={"x": x_values, "y": y_values, "processing_config": {}},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_analysis_data"


def test_analysis_recipes_comparison_and_mapping(isolated_storage):
    recipe_response = client.post(
        "/analysis/recipes",
        json={"name": "MoS2 standard", "config": {"baseline_method": "als", "peak_model": "Voigt"}},
    )
    first = post_import("compare-1.txt", b"XYDATA=\n1,2\n2,4\n3,3\n").json()
    second = post_import("compare-2.txt", b"XYDATA=\n1,3\n2,6\n3,4\n").json()
    mapping = post_import("map.csv", b"1,2,3\n4,5,6\n7,8,9\n").json()

    recipes = client.get("/analysis/recipes")
    comparison = client.post("/analysis/compare", json={"file_ids": [first["file_id"], second["file_id"]]})
    mapping_response = client.get(f"/files/{mapping['file_id']}/mapping")

    assert recipe_response.status_code == 201
    assert recipes.json()[0]["config"]["peak_model"] == "Voigt"
    assert comparison.status_code == 200
    assert comparison.json()["spectra"][1]["correlation_to_first"] == pytest.approx(0.981981, abs=0.001)
    assert mapping_response.status_code == 200
    assert mapping_response.json()["matrix"] == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]


def test_mapping_rejects_ragged_or_non_finite_matrix(isolated_storage):
    ragged = post_import("ragged.csv", b"1,2,3\n4,5\n", DEFAULT_METADATA).json()
    non_finite = post_import("nonfinite.csv", b"1,2\n3,nan\n", DEFAULT_METADATA).json()

    ragged_response = client.get(f"/files/{ragged['file_id']}/mapping")
    non_finite_response = client.get(f"/files/{non_finite['file_id']}/mapping")

    assert ragged_response.status_code == 422
    assert ragged_response.json()["detail"]["code"] == "mapping_shape_invalid"
    assert non_finite_response.status_code == 422
    assert non_finite_response.json()["detail"]["code"] == "mapping_unavailable"


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("tab-map.tsv", b"1\t2\t3\n4\t5\t6\n"),
        ("semicolon-map.dat", b"1;2;3\n4;5;6\n"),
        ("space-map.asc", b"1 2 3\n4 5 6\n"),
    ],
)
def test_mapping_accepts_common_numeric_matrix_delimiters(isolated_storage, filename, content):
    imported = post_import(filename, content, DEFAULT_METADATA).json()

    response = client.get(f"/files/{imported['file_id']}/mapping")

    assert response.status_code == 200
    assert response.json()["matrix"] == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    assert response.json()["source_rows"] == 2
    assert response.json()["source_columns"] == 3


def test_mapping_preview_is_bounded_but_range_describes_full_matrix(isolated_storage):
    rows = []
    for row_index in range(599):
        row = [float(row_index * 301 + column_index) for column_index in range(301)]
        if row_index == 597:
            row[299] = 9_999_999.0  # deliberately omitted by both sampling strides
        rows.append(",".join(str(value) for value in row))
    imported = post_import("large-map.csv", "\n".join(rows).encode(), DEFAULT_METADATA).json()

    response = client.get(f"/files/{imported['file_id']}/mapping")

    assert response.status_code == 200
    mapping = response.json()
    assert mapping["source_rows"] == 599
    assert mapping["source_columns"] == 301
    assert mapping["rows"] <= 300
    assert mapping["columns"] <= 300
    assert mapping["maximum"] == 9_999_999.0
    assert max(max(row) for row in mapping["matrix"]) < mapping["maximum"]


@pytest.mark.parametrize(
    "file_ids",
    [
        [None, "valid"],
        [123, "valid"],
        [{"id": "one"}, "valid"],
        ["", "valid"],
        ["   ", "valid"],
        ["x" * 101, "valid"],
        ["duplicate", "duplicate"],
        [" duplicate ", "duplicate"],
    ],
)
def test_spectrum_comparison_rejects_invalid_or_duplicate_ids(isolated_storage, file_ids):
    response = client.post("/analysis/compare", json={"file_ids": file_ids})

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_comparison"


def test_spectrum_comparison_interpolates_on_shared_x_coordinates(isolated_storage):
    first = post_import(
        "grid-a.txt",
        b"XUNITS=1/cm\nXYDATA=\n1,10\n2,20\n3,30\n4,40\n5,50\n",
        DEFAULT_METADATA,
    ).json()
    second = post_import(
        "grid-b.txt",
        b"XUNITS=RamanShift\nXYDATA=\n2,20\n3,30\n4,40\n",
        DEFAULT_METADATA,
    ).json()

    response = client.post(
        "/analysis/compare",
        json={"file_ids": [first["file_id"], second["file_id"]]},
    )

    assert response.status_code == 200
    compared = response.json()["spectra"][1]
    assert compared["correlation_to_first"] == pytest.approx(1.0)
    assert compared["overlap_point_count"] == 3
    assert compared["overlap_range"] == [2.0, 4.0]
    assert compared["comparison_warning"] is None


@pytest.mark.parametrize(
    ("first_units", "second_units", "expected_warning"),
    [
        ("RamanShift", "nm", "Spectral X-axis units do not match."),
        ("RamanShift", None, "X-axis unit compatibility cannot be verified because one spectrum has no units."),
    ],
)
def test_spectrum_comparison_refuses_unverifiable_or_incompatible_units(
    isolated_storage, first_units, second_units, expected_warning
):
    def content(units, scale):
        header = f"XUNITS={units}\n" if units else ""
        return f"{header}XYDATA=\n1,{scale}\n2,{2 * scale}\n3,{3 * scale}\n".encode()

    first = post_import("unit-a.txt", content(first_units, 1), DEFAULT_METADATA).json()
    second = post_import("unit-b.txt", content(second_units, 2), DEFAULT_METADATA).json()

    response = client.post(
        "/analysis/compare",
        json={"file_ids": [first["file_id"], second["file_id"]]},
    )

    assert response.status_code == 200
    compared = response.json()["spectra"][1]
    assert compared["correlation_to_first"] is None
    assert compared["overlap_point_count"] == 0
    assert compared["comparison_warning"] == expected_warning


def test_edit_import_updates_metadata_without_changing_raw_file(isolated_storage):
    temporary_database, temporary_raw_directory = isolated_storage
    original_bytes = b"FILETYPE=RAMAN SPECTRUM\nXYDATA=\n1,2\n2,3\n"
    imported = post_import("editable.txt", original_bytes).json()

    response = client.patch(
        f"/files/{imported['file_id']}",
        json={
            "technique": "Raman spectroscopy",
            "sample_id": "A01",
            "measurement_date": "2026-03-10",
            "instrument": "XperRAM-C2",
            "operator": "M. Haidari",
            "notes": "Reviewed metadata",
            "material_system": "MoS₂ on Si/SiO₂",
            "experimental_details": {
                "measurement_type": "Single spectrum",
                "laser_wavelength": "532",
                "laser_power": "1.0",
                "objective": "50x",
                "integration_time": "10",
                "accumulations": "3",
                "detector": "CCD",
                "grating": "2400",
                "x_units": "1/cm",
            },
        },
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["sample_id"] == "A01"
    assert updated["operator"] == "M. Haidari"
    assert updated["material_system"] == "MoS₂ on Si/SiO₂"
    assert updated["experimental_details"]["measurement_type"] == "Single spectrum"
    assert updated["experimental_details"]["laser_wavelength"] == "532"
    assert updated["experimental_details"]["objective"] == "50x"
    assert updated["sha256"] == imported["sha256"]
    stored_file = temporary_raw_directory / imported["file_id"] / "editable.txt"
    assert stored_file.read_bytes() == original_bytes
    with sqlite3.connect(temporary_database) as connection:
        row = connection.execute(
            "SELECT sample_id, operator, sha256, material_system, extended_metadata FROM imported_files WHERE file_id = ?",
            (imported["file_id"],),
        ).fetchone()
    assert row[:4] == ("A01", "M. Haidari", imported["sha256"], "MoS₂ on Si/SiO₂")
    assert json.loads(row[4])["grating"] == "2400"


def test_remove_import_archives_listing_but_retains_raw_file(isolated_storage):
    temporary_database, temporary_raw_directory = isolated_storage
    original_bytes = b"immutable spectrum bytes"
    imported = post_import("retained.txt", original_bytes).json()

    response = client.delete(f"/files/{imported['file_id']}")

    assert response.status_code == 204
    assert client.get("/files").json() == []
    stored_file = temporary_raw_directory / imported["file_id"] / "retained.txt"
    assert stored_file.read_bytes() == original_bytes
    with sqlite3.connect(temporary_database) as connection:
        archived = connection.execute(
            "SELECT archived_at FROM archived_imports WHERE file_id = ?",
            (imported["file_id"],),
        ).fetchone()
        metadata = connection.execute(
            "SELECT sha256, size_bytes FROM imported_files WHERE file_id = ?",
            (imported["file_id"],),
        ).fetchone()
    assert archived and archived[0]
    assert metadata == (imported["sha256"], len(original_bytes))
    assert client.get(f"/files/{imported['file_id']}/content").status_code == 404
    assert client.get(f"/files/{imported['file_id']}/spectrum").status_code == 404

    restore_response = post_import("retained.txt", original_bytes)

    assert restore_response.status_code == 201
    assert restore_response.json()["file_id"] == imported["file_id"]
    assert database.list_imported_files() == [restore_response.json()]
    assert stored_file.read_bytes() == original_bytes
    assert [path for path in temporary_raw_directory.rglob("*") if path.is_file()] == [stored_file]
    with sqlite3.connect(temporary_database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM archived_imports WHERE file_id = ?",
            (imported["file_id"],),
        ).fetchone()[0] == 0


def test_archived_import_is_not_restored_when_retained_bytes_are_corrupt(isolated_storage):
    original = b"retained immutable bytes"
    imported = post_import("corrupt-retained.txt", original).json()
    assert client.delete(f"/files/{imported['file_id']}").status_code == 204
    Path(imported["storage_path"]).write_bytes(b"X" * len(original))

    response = post_import("corrupt-retained.txt", original)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "raw_file_checksum_mismatch"
    assert client.get("/files").json() == []
    with database.connect_database() as connection:
        assert database.is_import_archived(connection, imported["file_id"])


def test_remove_import_group_archives_all_records_and_retains_raw_files(isolated_storage):
    _, raw_directory = isolated_storage
    first = post_import("group-one.txt", b"group raw one").json()
    second = post_import("group-two.txt", b"group raw two").json()

    response = client.post(
        "/files/archive-batch",
        json={"file_ids": [first["file_id"], second["file_id"]]},
    )

    assert response.status_code == 200
    assert response.json() == {"archived_count": 2, "requested_count": 2}
    assert client.get("/files").json() == []
    assert (raw_directory / first["file_id"] / "group-one.txt").read_bytes() == b"group raw one"
    assert (raw_directory / second["file_id"] / "group-two.txt").read_bytes() == b"group raw two"

    repeated = client.post(
        "/files/archive-batch",
        json={"file_ids": [first["file_id"], second["file_id"]]},
    )
    assert repeated.status_code == 200
    assert repeated.json() == {"archived_count": 0, "requested_count": 2}


@pytest.mark.parametrize("invalid_id", [None, 42, {}, "", " " * 3, "x" * 101])
def test_remove_import_group_rejects_invalid_file_ids(isolated_storage, invalid_id):
    response = client.post("/files/archive-batch", json={"file_ids": [invalid_id]})

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_file_ids"


def test_import_preserves_bytes_sanitizes_name_and_persists_metadata(
    isolated_storage,
):
    _, temporary_raw_directory = isolated_storage
    original_bytes = (
        bytes(range(256)) * ((1024 * 1024 // 256) + 1)
        + b"\x00\xff\r\nRaman shift,intensity\n380.0,125\n"
    )
    expected_checksum = hashlib.sha256(original_bytes).hexdigest()

    response = post_import("../../unsafe?.csv", original_bytes)

    assert response.status_code == 201
    result = response.json()
    assert result["original_filename"] == "unsafe?.csv"
    assert result["content_type"] == "application/octet-stream"
    assert result["size_bytes"] == len(original_bytes)
    assert result["sha256"] == expected_checksum
    assert result["imported_at"]
    for field_name, value in DEFAULT_METADATA.items():
        assert result[field_name] == value

    stored_file = (
        temporary_raw_directory
        / result["file_id"]
        / "unsafe_.csv"
    )
    assert result["storage_path"] == str(stored_file)
    assert stored_file.read_bytes() == original_bytes

    with database.connect_database() as connection:
        record = connection.execute(
            "SELECT * FROM imported_files WHERE file_id = ?",
            (result["file_id"],),
        ).fetchone()
    raw_record = dict(record)
    assert raw_record.pop("extended_metadata") == json.dumps(result["experimental_details"])
    api_record = dict(result)
    api_record.pop("experimental_details")
    assert raw_record == api_record
    assert client.get(f"/files/{result['file_id']}").json() == result


def test_import_persists_material_system_and_experimental_details(isolated_storage):
    metadata = {
        **DEFAULT_METADATA,
        "material_system": "MoS₂ on Si/SiO₂",
        "measurement_type": "Single spectrum",
        "laser_wavelength": "532",
        "laser_power": "0.5",
        "objective": "100x",
        "integration_time": "5",
        "accumulations": "10",
        "detector_model": "Atik428EX",
        "grating": "2400",
        "x_units": "1/cm",
    }

    response = post_import("A01_MoS2.txt", b"XYDATA=\n1,2\n", metadata)

    assert response.status_code == 201
    imported = response.json()
    assert imported["material_system"] == "MoS₂ on Si/SiO₂"
    assert imported["experimental_details"]["measurement_type"] == "Single spectrum"
    assert imported["experimental_details"]["laser_wavelength"] == "532"
    assert imported["experimental_details"]["detector_model"] == "Atik428EX"
    assert client.get(f"/files/{imported['file_id']}").json() == imported


def test_import_autofills_each_files_embedded_metadata(isolated_storage):
    contents = b"\n".join([
        b"FILETYPE=RAMAN SPECTRUM",
        b"DATETIME=2026-03-10 08:30:34",
        b"LASER=532",
        b"IT=2000ms",
        b"SPECTRATAKEN=12",
        b"MODEL=XperRAM-C2",
        b"SPECTROMETER=XPE35",
        b"GRATING=2400",
        b"XUNITS=1/cm",
        b"XYDATA=",
        b"100,200",
    ])
    response = client.post(
        "/files/import",
        files={"file": ("A01_MoS2_Spectrum.txt", contents, "text/plain")},
        data={
            "technique": "Raman spectroscopy",
            "sample_id": "A01",
            "relative_path": "A01-2026.03.10/A01_MoS2_Spectrum.txt",
        },
    )

    assert response.status_code == 201
    imported = response.json()
    assert imported["measurement_date"] == "2026-03-10"
    assert imported["material_system"] is None
    assert imported["instrument"] == "XperRAM-C2 / XPE35"
    assert imported["experimental_details"] == {
        "measurement_type": "Single spectrum",
        "laser_wavelength": "532",
        "laser_power": None,
        "power_at_sample": None,
        "objective": None,
        "integration_time": "2",
        "accumulations": "12",
        "detector": None,
        "detector_model": None,
        "detector_temperature": None,
        "spectrometer": "XPE35",
        "grating": "2400",
        "binning_start": None,
        "binning_length": None,
        "spectral_start": None,
        "spectral_range": None,
        "x_units": "1/cm",
        "lpf_angle": None,
        "bpf_angle": None,
        "display_mode": None,
        "live_focus": None,
        "z_position": None,
    }

    suggestions = client.get(
        f"/files/{imported['file_id']}/metadata-suggestions"
    )
    assert suggestions.status_code == 200
    assert suggestions.json()["suggested_metadata"]["laser_wavelength"] == "532"
    assert "material_system" not in suggestions.json()["suggested_metadata"]
    assert suggestions.json()["material_system_proposal"]["material_system"] == "MoS₂"
    assert suggestions.json()["material_system_proposal"]["requires_confirmation"] is True


def test_duplicate_content_is_rejected_without_overwrite(isolated_storage):
    _, raw_directory = isolated_storage
    original_bytes = b"same immutable experiment"

    first_response = post_import("first.dat", original_bytes)
    duplicate_response = post_import("second.dat", original_bytes)

    assert first_response.status_code == 201
    assert duplicate_response.status_code == 409
    assert duplicate_response.json()["detail"] == {
        "code": "duplicate_file",
        "message": "An identical file has already been imported.",
        "existing_file_id": first_response.json()["file_id"],
    }
    assert database.list_imported_files() == [first_response.json()]
    raw_files = [
        path
        for path in raw_directory.rglob("*")
        if path.is_file() and ".staging" not in path.parts
    ]
    assert len(raw_files) == 1
    assert raw_files[0].read_bytes() == original_bytes
    assert list((raw_directory / ".staging").glob("*")) == []


def test_oversized_file_is_rejected_and_removed(
    isolated_storage,
    monkeypatch,
):
    _, raw_directory = isolated_storage
    monkeypatch.setattr(main, "MAX_FILE_SIZE_BYTES", 10)

    response = post_import(contents=b"01234567890")

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "file_too_large"
    assert_no_import_artifacts(raw_directory)


def test_empty_file_is_rejected_and_removed(isolated_storage):
    _, raw_directory = isolated_storage

    response = post_import(contents=b"")

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "empty_file",
        "message": "Empty files cannot be imported.",
    }
    assert_no_import_artifacts(raw_directory)


@pytest.mark.parametrize(
    ("metadata", "expected_code"),
    [
        ({**DEFAULT_METADATA, "technique": "   "}, "missing_metadata"),
        (
            {**DEFAULT_METADATA, "measurement_date": "29 July 2026"},
            "invalid_measurement_date",
        ),
    ],
)
def test_invalid_metadata_is_rejected(
    isolated_storage,
    metadata,
    expected_code,
):
    _, raw_directory = isolated_storage

    response = post_import(metadata=metadata)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == expected_code
    assert_no_import_artifacts(raw_directory)


def test_database_failure_rolls_back_new_file(
    isolated_storage,
    monkeypatch,
):
    _, raw_directory = isolated_storage

    def fail_insert(_connection, _metadata):
        raise sqlite3.OperationalError("simulated insert failure")

    monkeypatch.setattr(database, "insert_imported_file", fail_insert)

    response = post_import()

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "import_failed"
    assert_no_import_artifacts(raw_directory)


def test_file_publish_failure_rolls_back_database(
    isolated_storage,
    monkeypatch,
):
    _, raw_directory = isolated_storage

    def fail_replace(_source, _destination):
        raise OSError("simulated file publish failure")

    monkeypatch.setattr(main.os, "replace", fail_replace)

    response = post_import()

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "import_failed"
    assert_no_import_artifacts(raw_directory)


def test_initialize_database_migrates_existing_metadata_table(
    tmp_path,
    monkeypatch,
):
    legacy_database = tmp_path / "legacy.db"
    monkeypatch.setattr(database, "DATABASE_PATH", legacy_database)
    with sqlite3.connect(legacy_database) as connection:
        connection.execute(
            """
            CREATE TABLE imported_files (
                file_id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                imported_at TEXT NOT NULL
            )
            """
        )

    database.initialize_database()

    with database.connect_database() as connection:
        column_names = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(imported_files)")
        }
        indexes = {
            row["name"]
            for row in connection.execute("PRAGMA index_list(imported_files)")
        }
    assert {
        "technique",
        "sample_id",
        "measurement_date",
        "instrument",
        "operator",
        "notes",
        "material_system",
        "extended_metadata",
    }.issubset(column_names)
    assert "idx_imported_files_sha256" in indexes


def test_initialize_database_preserves_legacy_duplicate_records(
    tmp_path,
    monkeypatch,
):
    legacy_database = tmp_path / "legacy-duplicates.db"
    monkeypatch.setattr(database, "DATABASE_PATH", legacy_database)
    with sqlite3.connect(legacy_database) as connection:
        connection.execute(
            """
            CREATE TABLE imported_files (
                file_id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                imported_at TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO imported_files VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("one", "one.dat", None, 1, "a" * 64, "raw/one", "2026-01-01"),
                ("two", "two.dat", None, 1, "a" * 64, "raw/two", "2026-01-02"),
            ],
        )

    database.initialize_database()

    with database.connect_database() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM imported_files"
        ).fetchone()[0]
        trigger = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'trigger'
              AND name = 'reject_duplicate_imported_file_sha256'
            """
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="duplicate sha256"):
            connection.execute(
                """
                INSERT INTO imported_files (
                    file_id,
                    original_filename,
                    size_bytes,
                    sha256,
                    storage_path,
                    imported_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("three", "three.dat", 1, "a" * 64, "raw/three", "2026-01-03"),
            )

    assert count == 2
    assert trigger is not None
