# Spatial Prediction Studio

Spatial Prediction Studio is an ArcMap 10.8 add-in for spatial prediction of soil properties from point observations and raster covariates. It runs the full digital soil mapping workflow inside ArcMap: data checks, validation design, preprocessing, variable selection, model fitting and tuning, prediction intervals, mapping, and reports.

The add-in keeps model development, independent testing and later use of the model apart. Every preprocessing step, selection step and tuning step is learned inside the training part of each fold. Reports state what each number means and what it does not show.

Version 0.5.0. Research software. The interface and reports are in English. The original Turkish version is kept at the tag [`v0.5.0-tr`](../../tree/v0.5.0-tr).

## Workflow in the ArcMap panel

The panel opens from the **Spatial Prediction** toolbar button. It has ten tabs:

| Tab | Purpose |
|---|---|
| 1 Project | Refresh layers, load the synthetic example, open or save a project, choose the output folder |
| 2 Data and target | Point layer or CSV/GeoJSON table with coordinates, target field and unit, observation ID, groups (profile, parcel, farm), depth, measurement method, sampling date, sample support, raster covariates and their metadata |
| 3 Validation | Fold design, prediction purpose, locked independent test set, user-defined folds |
| 4 Preprocessing | Imputation, scaling, unknown categories, target transformation |
| 5 Variable selection | Multicollinearity (VIF), variable selection or reduction, permutation variable importance |
| 6 Model and search | Model, candidate settings, hyperparameter search |
| 7 Evaluation | Outer-fold, out-of-fold, independent test and search tables; subgroup diagnostics |
| 8 Uncertainty | Prediction interval method and nominal coverage |
| 9 Mapping | Reference grid, processing window, physical bounds, area of applicability, figure resolution |
| 10 Report and log | Report, study package, model export, log |

**Show folds** draws the fold preview on the map. **Run model** needs a current fold preview. Results of earlier runs stay available as previous results.

## Methods

The method registry ([`backend/sps/registry.json`](backend/sps/registry.json)) lists every method, its parameters, its status and its known limits. Version 0.5.0 enables these methods:

- **Validation:** random k-fold, repeated random k-fold, grouped k-fold, spatial blocks, buffered spatial blocks, user-defined folds, k-fold nearest neighbor distance matching (kNNDM, through a local R installation of CAST 1.0.4), time-ordered validation with new groups. Outer folds estimate performance; inner folds select variables and tune settings.
- **Preprocessing:** imputation, scaling, handling of unknown categories; target transformation (none, log, Yeo-Johnson).
- **Multicollinearity and variable selection:** variance inflation factor (diagnostic or iterative elimination); Pearson, Spearman, mutual information, Elastic Net, sequential forward selection, recursive feature elimination, Boruta, principal component analysis.
- **Models:** training mean and median (reference), ridge, Elastic Net, random forest, extra trees, support vector regression, histogram gradient boosting, XGBoost, LightGBM, CatBoost, Cubist, ordinary kriging, regression kriging; experimental TabPFN v2, TabM and RealMLP.
- **Search:** manual settings, grid search, random search, Optuna.
- **Prediction intervals:** split conformal prediction, quantile regression forest, gradient boosting with quantile loss, conformalized quantile regression, experimental spatially weighted conformal prediction.
- **Mapping:** prediction rasters, interval rasters, area of applicability (AOA) and dissimilarity index (DI). Predictions outside the physical bounds are flagged in a separate layer and are not clipped. Predictions outside the training range can be masked.

Environmental clusters, design-based map validation and group bootstrap spread are listed in the registry but not implemented.

## Outputs

- Run report (HTML) with figures, tables and metric definitions.
- Detailed study report: a multi-page HTML report with every setting, fold, metric and model record, written for the methods section of a paper.
- Study package: a ZIP archive of the run with SHA-256 digests of all members.
- Model package (`*.spsmodel.zip`) that can be read back and checked.
- Rasters added to the ArcMap document.

## Architecture

| Folder | Content |
|---|---|
| `native/` | C# add-in: WinForms panel, ArcObjects adapter, job client, MCP pipe bridge (.NET Framework 4.8, x86) |
| `backend/` | Python 3.12 worker that runs every computation in a separate 64-bit process |
| `mcp-server/` | Local Model Context Protocol (MCP) server with 15 tools, so AI clients can drive the panel and read verified outputs |
| `installer/` | Inno Setup script and payload tools |
| `tools/` | Build, runtime setup, registry generation and example data scripts |
| `tests/`, `native/tests/` | Python tests and C# contract tests |
| `build/` | Read-back scripts for the 0.5.0 acceptance evidence |
| `docs/` | [Terminology](docs/TERMINOLOGY.md) used in the interface and reports |

ArcMap is a 32-bit application. The panel therefore writes each job as a JSON request and starts `backend/worker.py` in a private 64-bit Python environment. The worker reports progress as JSON lines. The panel verifies the SHA-256 digests of the outputs before it shows or maps them.

## Requirements

- Windows 10 or later, x64
- ArcGIS Desktop 10.8 (ArcMap)
- .NET Framework 4.8; MSBuild (Visual Studio 2019 Build Tools) to build
- Python 3.12, 64-bit, for the worker environments
- Optional: Conda, for the R and CAST 1.0.4 reference environment used by kNNDM and its tests
- Optional: Inno Setup, to build the installer

Dependency versions are pinned in `requirements-lock.txt`, `requirements-d-lock.txt` (deep-learning models) and `requirements-mcp-lock.txt`.

## Build and install from source

Run from PowerShell in the repository root:

```powershell
tools\setup-runtime.ps1 -BasePython C:\Path\To\python.exe      # core worker environment (runtime\)
tools\setup-runtime-d.ps1 -BasePython C:\Path\To\python.exe    # worker environment with deep-learning models (runtime-d\)
tools\setup-cast.ps1 -Conda C:\Path\To\conda.exe               # optional: R and CAST reference environment
tools\build.ps1                                                # compile, run native tests, build the add-in package
tools\install.ps1                                              # copy the add-in to the ArcMap add-in folder
```

Restart ArcMap after installation. The MCP server has its own environment: `tools\setup-runtime-mcp.ps1 -BasePython C:\Path\To\python.exe`.

TabPFN needs its model weights, which are not in this repository. `tools/setup-tabpfn-weights.py` downloads the pinned TabPFN v2 regression weights and the Prior Labs License 1.1 into `models/tabpfn-v2/`. Read that license before you use TabPFN. The model adapter never downloads anything by itself.

## Tests

```bash
python tools/make_examples.py --output examples      # synthetic example data used by the tests
python -m pytest tests -q
cd mcp-server && python -m pytest tests -q
```

`tools/test-backend.ps1` runs the backend suite in groups and writes JUnit XML evidence. `tools/test-native.ps1` builds and runs the C# contract tests. Tests that need R and CAST, Windows named pipes or the TabPFN weights do not run without them.

## Data

The example data are synthetic and generated by `tools/make_examples.py`. They are not field observations.

## License

No license has been chosen yet. Third-party components keep their own licenses; `tools/collect_licenses.py` collects their notices for the installer.
