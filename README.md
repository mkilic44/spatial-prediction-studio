# Spatial Prediction Studio

Spatial Prediction Studio is an ArcMap 10.8 add-in for spatial prediction of soil properties from point observations and raster covariates. It runs the digital soil mapping workflow inside ArcMap: data checks, validation design, preprocessing, variable selection, model fitting and tuning, prediction intervals, mapping and reports.

The add-in keeps model development, independent testing and later use of the model apart. Every preprocessing, selection and tuning step is learned inside the training part of each fold. Reports state what each number means and what it does not show.

Version 0.5.0. Research software. The original Turkish version is kept in the branch [`turkish-original`](../../tree/turkish-original).

## Install

You need Windows 10 or 11 (64-bit) with ArcGIS Desktop 10.8 (ArcMap). No administrator rights are needed.

1. Download this repository: **Code > Download ZIP**, then extract it.
2. Close ArcMap and double-click **`Install.cmd`**. The first installation downloads about 700 MB and takes 10 to 30 minutes.
3. Start ArcMap and click **Spatial Prediction** on the Spatial Prediction Studio toolbar. To try it, click **Load example** on the first tab.

To remove it, run **`Uninstall.cmd`**. Your projects and results are kept. Options, troubleshooting and what goes where: [docs/INSTALL.md](docs/INSTALL.md).

## Documentation

| Document | Content |
|---|---|
| [Installation](docs/INSTALL.md) | Requirements, installation options, updating, uninstalling, troubleshooting |
| [User guide](docs/USER_GUIDE.md) | The panel tab by tab, from data to report |
| [Methods reference](docs/METHODS.md) | Every validation design, preprocessing step, selection method, model, search and interval method with its parameters |
| [Outputs](docs/OUTPUTS.md) | Rasters, reports, tables, model and study packages, metric definitions |
| [MCP server](docs/MCP.md) | Optional connection for local AI clients |
| [Development and releases](docs/BUILD.md) | Repository layout, tests, continuous integration, building |
| [Third-party components](docs/THIRD_PARTY_LICENSES.md) | Packages that the installer downloads and their licenses |
| [Terminology](docs/TERMINOLOGY.md) | English terms used in the interface and reports |

## Workflow in the panel

| Tab | Purpose |
|---|---|
| 1 Project | Refresh layers, load the synthetic example, open or save a project, choose the output folder, enable the local MCP connection |
| 2 Data and target | Point layer or CSV table with coordinates, target field and unit, observation ID, groups (profile, parcel, farm), depth, measurement method, sampling date, sample support, independent test file, raster covariates and their metadata |
| 3 Validation | Validation design, prediction purpose, locked independent test set, user-defined folds |
| 4 Preprocessing | Imputation, scaling, unknown categories, target transformation |
| 5 Variable selection | Multicollinearity (variance inflation factor), variable selection or reduction |
| 6 Model and search | Model, candidate settings, hyperparameter search, permutation variable importance |
| 7 Evaluation | Outer-fold, out-of-fold, independent test and search tables of the completed run |
| 8 Uncertainty | Prediction interval method, nominal coverage, subgroup diagnostics |
| 9 Mapping | Reference grid, masking, processing window, physical bounds, area of applicability, figure resolution |
| 10 Report and log | Report, study package, model export, log, previous results |

**Show folds** draws the fold preview on the map. **Run model** needs a current fold preview. Results of earlier runs stay available as previous results.

## Methods

The method registry (`backend/sps/registry.json`) lists every method with its parameters, status and known limits; [docs/METHODS.md](docs/METHODS.md) is generated from it. Version 0.5.0 enables:

- **Validation:** random k-fold, repeated random k-fold, grouped k-fold, spatial blocks, buffered spatial blocks, user-defined folds, k-fold nearest neighbor distance matching (kNNDM, with CAST 1.0.4 in an optional R runtime), time-ordered validation with new groups. Outer folds estimate performance; inner folds select variables and tune settings.
- **Preprocessing:** imputation, scaling, handling of unknown categories; target transformation (none, log, Yeo-Johnson).
- **Multicollinearity and variable selection:** variance inflation factor (diagnostic or iterative elimination); Pearson, Spearman, mutual information, Elastic Net, sequential forward selection, recursive feature elimination, Boruta, principal component analysis.
- **Models:** training mean and median (reference), ridge, Elastic Net, random forest, extra trees, support vector regression, histogram gradient boosting, XGBoost, LightGBM, CatBoost, Cubist, ordinary kriging, regression kriging; experimental TabPFN v2, TabM and RealMLP.
- **Search:** manual settings, grid search, random search, Optuna.
- **Prediction intervals:** split conformal prediction, quantile regression forest, gradient boosting with quantile loss, conformalized quantile regression, experimental spatially weighted conformal prediction.
- **Mapping:** prediction rasters, interval rasters, area of applicability (AOA) and dissimilarity index (DI). Predictions outside the physical bounds are flagged in a separate layer and are not clipped. Predictions outside the training range can be masked.

Environmental clusters, design-based map validation and group bootstrap spread are listed in the registry but not implemented.

## How it works

ArcMap is a 32-bit application. The panel writes each job as a JSON request and starts `backend/worker.py` in a private 64-bit Python runtime with pinned packages. The worker reports progress as JSON lines and writes every output with its SHA-256 digest. The panel checks these digests before it shows or maps an output. The installer compiles the add-in on your computer against your own ArcGIS Desktop assemblies.

| Folder | Content |
|---|---|
| `native/` | C# add-in (.NET Framework 4.8, x86) and its contract tests |
| `backend/` | Python worker and the method registry |
| `mcp-server/` | Optional local Model Context Protocol (MCP) server with 15 tools |
| `installer/` | `Install.cmd` back end, runtime verification, offline installer of the maintainer |
| `tools/`, `tests/`, `build/`, `docs/` | Development scripts, tests, archived acceptance scripts, documentation |

## Data

The example data are synthetic and generated by `tools/make_examples.py`. They are not field observations.

## Third-party components

The installer downloads open-source packages with pinned versions; each keeps its own license (see [docs/THIRD_PARTY_LICENSES.md](docs/THIRD_PARTY_LICENSES.md)). The experimental TabPFN model is licensed under the Prior Labs License 1.1: **Built with PriorLabs-TabPFN.** Its weights are downloaded only with `Install.cmd -WithTabPFN -AcceptTabPFNLicense`.

## License

Spatial Prediction Studio is released under the [MIT License](LICENSE). The packages that the installer downloads keep their own licenses ([docs/THIRD_PARTY_LICENSES.md](docs/THIRD_PARTY_LICENSES.md)). The TabPFN model and its weights are covered by the Prior Labs License 1.1, not by the MIT License.
