import hashlib
import json
import math
import sqlite3

import database
import main
import pytest
from fastapi.testclient import TestClient

from main import app


client = TestClient(app)

DEFAULT_METADATA = {
    "technique": "Raman spectroscopy",
    "sample_id": "SAMPLE-001",
    "measurement_date": "2026-07-29",
    "instrument": "Lab Raman 532 nm",
    "operator": "Test Operator",
    "notes": "First pilot measurement",
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
        "version": "0.2.0",
    }


def test_health_check():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_upload_interface_is_available():
    response = client.get("/upload")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
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
    assert '<details class="analysis-section">' in response.text
    assert '<summary id="artifacts-heading">Instrumental artifacts</summary>' in response.text
    assert '<summary id="additional-processing-heading">Additional processing</summary>' in response.text
    assert 'Fit peaks &amp; calculate parameters' in response.text
    assert "function cosmicRayIndexes(values)" in response.text
    assert "function subtractRayleighLeakage(xValues, yValues, halfWidth)" in response.text
    assert 'value="__sio2_si_model__">Target-adaptive SiO₂/Si model' in response.text
    assert "/substrate-model`" in response.text
    assert "function detectedBaseline(values, method)" in response.text
    assert 'id="smoothing-window"' in response.text
    assert 'id="show-peaks"' in response.text
    assert 'id="box-zoom-mode"' in response.text
    assert 'id="pan-spectrum-mode"' in response.text
    assert "event.button === 1 ? 'pan'" in response.text
    assert "nextSpan >= fullSpan * 0.999999" in response.text
    assert "const reachedFullView = event.deltaY > 0" in response.text
    assert "const fullY = processedIntensities(currentSpectrum)" in response.text
    assert "nextMin = fullMin" in response.text
    assert "nextMax = fullMax" in response.text
    assert 'id="export-spectrum-csv"' in response.text
    assert 'id="export-spectrum-svg"' in response.text
    assert 'id="export-spectrum-png"' in response.text
    assert 'id="spectrum-panel" class="panel"' in response.text
    assert "[cropMin, cropMax].forEach((control) => control.addEventListener('change', resetSpectrumView))" in response.text
    assert "smoothingWindow.value; renderSpectrum();" in response.text
    assert "if (subtractReference.checked) renderSpectrum();" in response.text
    assert "appendChild(spectrumPanel)" in response.text
    assert 'name="technique"' in response.text
    assert 'name="sample_id"' in response.text


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


def test_list_imported_files_returns_empty_list(isolated_storage):
    response = client.get("/files")

    assert response.status_code == 200
    assert response.json() == []


def test_operator_list_uses_most_recent_operator_as_default_order(isolated_storage):
    first = {**DEFAULT_METADATA, "operator": "First Operator"}
    second = {**DEFAULT_METADATA, "operator": "Latest Operator"}
    assert post_import("first.txt", b"first operator data", first).status_code == 201
    assert post_import("second.txt", b"second operator data", second).status_code == 201

    response = client.get("/operators")

    assert response.status_code == 200
    assert response.json() == ["Latest Operator", "First Operator"]


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
    assert model["method"] == "target-adaptive non-negative mixture of SiO2/Si reference spectra with material-peak protection"
    assert len(model["reference_files"]) == 5
    assert len(model["reference_weights"]) == 5
    assert sum(model["reference_weights"].values()) == pytest.approx(1.0, abs=1e-5)
    assert max(model["reference_weights"].values()) > min(model["reference_weights"].values())
    assert model["fit_point_count"] > 10
    assert len(model["substrate_y"]) == len(target_values)
    assert [region["assignment"] for region in model["protected_regions"]] == ["MoS2 E mode", "MoS2 A1 mode"]
    corrected_e = main.calculate_peak_metrics(model["x"], model["corrected_y"], 365, 395)
    corrected_a = main.calculate_peak_metrics(model["x"], model["corrected_y"], 395, 425)
    assert corrected_e["height"] > 300
    assert corrected_a["height"] > 400


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
    assert response.content == image_bytes


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
    assert analysis["sha256"] == first["sha256"]
    assert analysis["quality"]["point_count"] == 201
    assert summary.status_code == 200
    assert summary.json()["spectrum_count"] == 2
    assert summary.json()["separation_stdev"] is not None


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
