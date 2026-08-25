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
checksums. Before copying, the backup command verifies database integrity and
checks every cataloged source file against its recorded size and checksum. It
then verifies the copied database and files before publishing the backup;
failed attempts leave no partial backup directory. The manifest reports
catalog verification and any uncataloged raw files. Copy the verified backup
to a second physical device or approved synchronized storage and periodically
test restoration.

`GET /health` reports the application version, SQLite integrity status, import
and analysis-run counts, analysis-provenance integrity, and raw-storage
availability without exposing local paths. Health checks fail if a legacy or
externally modified analysis run is orphaned or records a raw checksum that no
longer matches its source.

Imported content is served with trusted extension-derived media types rather
than uploader-supplied MIME claims. Supported image formats may be previewed
inline under a sandboxed content-security policy; all other raw files are
forced to download. Application responses also set no-sniff, same-origin
framing, referrer, permissions, and content-security headers.

Metadata create/edit operations enforce the same field lengths and text/null
types. Unknown experimental-detail names are rejected instead of silently
discarded. Preset, recipe, and analysis configurations are limited to a
100-KB object containing named scalar values; nested objects, arrays, and
NaN/infinite numbers are rejected with structured validation errors.
SQLite additionally enforces valid JSON-object storage for extended metadata,
presets, recipes, processing configurations, and analysis results. Legacy
malformed records return a structured integrity error, fail health checks, and
block backups instead of being silently replaced with empty values.

## Development status

The local application currently provides:

- immutable, checksum-tracked file import with multi-file and recursive-folder workflows;
- automatic Raman-header metadata extraction with editable per-file records;
- constant-memory full-file scanning for measured spectral bounds, avoiding
  truncated end values when a text spectrum exceeds the metadata preview;
- spectrum viewing for instrument `XYDATA` files and finite, monotonic
  two-column CSV/TSV/text exports, including descending acquisition order;
- searchable, grouped imports with spectrum, image, and mapping previews;
- finite numeric mapping matrices in comma, semicolon, tab, or whitespace
  formats, with previews bounded to 300 x 300 while full-source dimensions and
  intensity range remain visible;
- interactive spectrum zooming, panning, processing, peak fitting, and export;
- keyboard-contained import, metadata, and residual-review dialogs with Escape
  dismissal, descriptive relationships, initial focus, and focus restoration;
- a mandatory baseline-first pipeline: baseline detection/subtraction precedes
  cosmic-ray handling, Rayleigh removal, substrate/reference subtraction,
  smoothing, normalization, and peak fitting;
- consistent moving-average, Gaussian, and X-aware quadratic Savitzky–Golay
  smoothing across raw and reference-corrected traces;
- incremental target-adaptive SiO2/Si modeling using every active confirmed
  substrate reference, tracking peak-position and absolute/Si-relative
  intensity variability with a checksum-derived ensemble fingerprint; and
- Raman analysis tied to the exact processed trace displayed in the viewer.

Spectrum comparison accepts 2 to 20 distinct imported spectrum IDs and rejects
blank, duplicate, non-string, or overlong identifiers before file lookup.

## Scientific provenance and integrity

Imported raw files are never rewritten by processing operations. Each import
stores its byte count and SHA-256 checksum. The **Verify raw file** action
recalculates the checksum and byte count from disk; the equivalent API endpoint
is `GET /files/{file_id}/integrity`. Spectrum, mapping, adaptive-reference, and
analysis operations refuse to use retained bytes that no longer match the
immutable import record. Archived imports are excluded from both listings and
direct data endpoints; re-import restores one only after its retained raw file
passes the same integrity checks.

Processed Raman analysis records the source raw-file checksum and the complete
trace-transforming configuration in an immutable analysis-run record. This
includes artifact and baseline subtraction, smoothing, normalization, and the
selected reference's checksum or adaptive-model version and source checksums.
Display-only controls remain in exported viewer recipes, so they do not
invalidate scientific repeatability. Each new run stores the exact derived XY
trace together with its checksum, the application version, and the analysis
timestamp. The retained trace is independently checksum-verified whenever the
run is read and is available from `GET /analysis-runs/{run_id}/trace` for
replay or export. Legacy runs created before trace retention remain readable
but are explicitly marked `unavailable_legacy` rather than reported as
verified. `POST /files/{file_id}/analysis` accepts the displayed
finite, strictly increasing XY trace only when its X coordinates match the
source spectrum. `GET /files/{file_id}/analysis-runs` and the viewer's
**Analysis history** action expose the audit trail. None of these operations
modify the source file.

Verified history rows can be replayed directly in the viewer without changing
the current zoom/pan window or reapplying today's controls. Changing a
processing control exits replay and returns to the current live pipeline.

SQLite triggers enforce this audit trail below the API layer: new runs must
name an existing source and its exact raw checksum, retain equal-length derived
X/Y arrays, and keep redundant result provenance consistent with the immutable
columns; saved runs cannot be updated or deleted; and analyzed source
identities/checksums cannot be changed or deleted. Ordinary metadata remains
editable. Health checks re-hash retained derived traces. Backups repeat the
source/run-provenance audit before copying any data.

Repeatability statistics compare only saved runs produced with the same
processing configuration and application version; files without a comparable
run are explicitly counted as excluded. Pairwise spectrum correlations are
calculated after interpolation onto shared Raman-shift coordinates rather than
by array index, and report their overlap range and point count.

Manual reference subtraction is also interpolated by Raman shift, never by
array index. References with invalid coordinate grids or insufficient coverage
are rejected. The adaptive SiO2/Si model reports references excluded for
covering less than 95% of the target range or for having missing/incompatible
X-axis units. Duplicate or reversing X coordinates are rejected rather than
silently sorted or averaged. Spectrum comparison likewise refuses to calculate
correlation when only one spectrum has units or when their units conflict.

Adaptive model version 4 applies rubber-band baseline subtraction first to the
target and every accepted SiO2/Si reference, removes only high-confidence
single-point cosmic impulses second, then fits the substrate while protecting
MoS2 peak regions. Its response retains raw, detected-baseline,
baseline-corrected, artifact-cleaned, fitted-substrate, and final corrected
traces. Adding another confirmed SiO2/Si import automatically changes the
ensemble fingerprint and recomputes learned peak-position, absolute-intensity,
and Si-relative-intensity distributions for the next target fit.

Confirmed SiO2/Si-only records are checked with a leave-one-out ensemble: the
selected file is excluded and modeled from the other substrate references.
The API reports the predictive residual RMS and maximum before enforcement.
It also reports a separate pre-enforcement model-fit result using a 12% relative
RMS tolerance; this predictive check may fail even when the final physical
constraint passes.
Because confirmed substrate-only metadata assigns the complete trace to the
substrate class, the final corrected trace is then constrained to an exact zero
line. This invariant is tested across five independently shaped reference
spectra; the pre-enforcement diagnostics remain visible so model accuracy is
not hidden by the identity constraint.

For records identified as MoS2/SiO2/Si, the adaptive subtraction can return a
noise-free, fitted MoS2-only reconstruction. This curve is a derived model—not
a raw experimental residual—and the interface labels it accordingly. Peak,
layer, strain, and doping interpretations still require suitable calibration,
matched controls, uncertainty review, and scientific judgment. Peak positions
and separations include approximate sampling/statistical uncertainties. Layer
labels are withheld unless both MoS2 modes pass minimum SNR, measurable-width,
sampling-resolution, and spectrum-quality checks. A label is also withheld
when the separation uncertainty crosses a layer-category boundary or lies
outside the supported range. Explicitly non-MoS2 records are not assigned MoS2
modes; unspecified records are clearly labeled as candidate assignments.
Calibration shifts require a resolved silicon reference peak with adequate SNR.
The reported uncertainty does not include calibration or model-choice
systematics.
