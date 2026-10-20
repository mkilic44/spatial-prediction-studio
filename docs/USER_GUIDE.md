# User guide

This guide walks through the Spatial Prediction Studio panel in ArcMap, tab by tab. For installation see [INSTALL.md](INSTALL.md). For every method and parameter see [METHODS.md](METHODS.md). For the files a run writes see [OUTPUTS.md](OUTPUTS.md).

## The workflow in short

1. Open the panel with the **Spatial Prediction** button on the Spatial Prediction Studio toolbar.
2. Choose the observations, the target and the raster covariates (tab 2).
3. Choose the validation design and the prediction purpose (tab 3).
4. Choose preprocessing, variable selection, model, search, interval method and mapping options (tabs 4 to 9).
5. Click **Preflight check**, then **Show folds**. The fold preview is drawn on the map.
6. Click **Run model**. A run needs a current fold preview; any change to the data or settings asks for a new preview.
7. When the run completes, the rasters are added to the map and the report opens from tab 10.

The panel never computes inside ArcMap itself. It writes each job as a request file and starts a separate 64-bit Python worker. **Cancel** stops the worker and every process it started.

## Try the example first

On **1 Project**, click **Load example**. The panel copies synthetic example data into `%LOCALAPPDATA%\SpatialPredictionStudio\examples`, adds a point layer and a two-band covariate raster to the map, selects the target field `soil_value` (g/kg) and the ID field `sample_id`, and opens tab 2. The example data are artificial and are not field observations. Then click **Show folds** and **Run model**.

## 1 Project

- **Refresh layers** reads the point and raster layers of the active map again.
- **Load example** loads the synthetic example.
- **Save project** and **Open project** store and restore all sources and settings in a `*.sps.json` file. After a project is opened, a new fold preview is required.
- **Output folder** is where runs are written. The default is `%LOCALAPPDATA%\SpatialPredictionStudio\runs`.
- **Enable the local Model Context Protocol (MCP) connection** lets a local AI client that you installed with the MCP server drive this panel. See [MCP.md](MCP.md). It is on by default. The connection is a local named pipe that only the same Windows user can open; network access is denied.

## 2 Data and target

**Observations** come from either:

- an ArcMap **point layer** (optionally **only points selected on the map**), or
- a **CSV file** with an X / longitude field, a Y / latitude field and the table CRS (for example `EPSG:4326`). Click **Read table fields** after choosing the file.

Then set:

| Field | Meaning |
|---|---|
| Target field | The soil property to predict. Missing or nonfinite target values stop the run; they are never imputed. |
| Observation ID | A unique ID per observation. Duplicate IDs stop the run. |
| Target unit | The unit shown in reports and maps, for example `g/kg`. |
| Unit field (optional) | A field with the unit of each observation. Every value must equal the target unit; otherwise the run stops. |
| Separate independent test file | A CSV or GeoJSON file with observations that are locked away from model development. It must use the same target and ID fields and different IDs. |
| Profile / parcel / farm groups | Fields that link dependent observations. Grouped and spatial validation designs keep each group together. |
| Upper and lower depth fields | The sampling depth interval. |
| Measurement method field | The laboratory method of the target. |
| Sampling date field | Needed for time-ordered validation. |
| Area represented by the sample | The sample support: point, composite sample or parcel mean. For a parcel mean, give the parcel GeoJSON file and its ID field. |

**Excluded observations and reasons** and the raster metadata are read from the project file and shown read-only. The panel does not assume information that was not recorded.

**Raster covariates.** Every band of every file-based raster in the map is listed. Tick **Use** for each band to include, and check its type, sampling, scientific name, unit and ID. A categorical raster can only be sampled with the nearest cell. **Selected raster band metadata** shows what is recorded for the band, including whether it was derived from the target observations. A raster derived from the target observations cannot be used without evidence of independence, because it would leak the target into the covariates. Rasters must be GeoTIFF files with a defined coordinate reference system.

## 3 Validation

Choose the validation design and its parameters (see [METHODS.md](METHODS.md#validation-design)). Set the **prediction purpose**: prediction within the study area, transfer to another area, or independent test. With the user-defined folds method, import a fold table that states the training and test IDs explicitly.

Outer folds estimate performance. Inner folds inside each outer training part are used for variable selection and tuning. Repeated folds give more evaluation records, but not more independent observations.

## 4 Preprocessing

Imputation, scaling, unknown categories and the target transformation. Each step is learned only from the training observations of each fold.

## 5 Variable selection

Multicollinearity (variance inflation factor) diagnostics or elimination, and variable selection or reduction. All selection runs inside the training part of each fold.

## 6 Model and search

Choose the model, the candidate settings and the search method. **Refresh search parameters** fills the search space table from the chosen model. **Compute permutation variable importance** shows how much the prediction error changes when the values of a variable are permuted; the result does not show a causal effect.

## 7 Evaluation

After a run, this tab lists the outer fold, out-of-fold prediction, independent test and search tables. Performance metrics are read from the verified table of the completed job.

## 8 Uncertainty

Choose the prediction interval method and its nominal coverage. Quantile regression forest goes with random forest, and gradient boosting with quantile loss goes with histogram-based gradient boosting. **Fields for subgroup diagnostics** produces error and coverage diagnostics per category of the chosen fields; categories with too few observations or independent groups are flagged.

## 9 Mapping

- **Align rasters to the reference grid** and choose the reference raster.
- **Mask predictions outside the training range** if you want those cells masked in the prediction products.
- **Processing window (cells)** sets the block size for writing rasters; it does not change the results.
- **Physical lower bound / upper bound**: predictions outside these bounds are flagged in a separate layer. Prediction values are not clipped.
- **Maximum missing data fraction**: the largest share of observations that may have a missing covariate value at their location. Above it, or when a covariate is missing everywhere, the run stops.
- **Figure resolution** for report figures: 150 or 900 dots per inch (dpi).
- **Compute the model's area of applicability (AOA)**: the applicability layer has the value 1 inside and 0 outside the area. Low environmental dissimilarity does not by itself show that a prediction is adequate.

## 10 Report and log

- **Open report** shows the verified run report or the detailed study report inside ArcMap.
- **Save study package** saves the whole run, with reports, tables, maps and the model package, as one `*.spsstudy.zip` file.
- **Save model** saves the model of the completed run as a `*.spsmodel.zip` file. If you changed the settings after the run, the panel says that the saved model belongs to the previous settings.
- **Save log** saves the panel log.
- **Add previous result to map** adds the rasters of an earlier completed run.

Before the panel shows or maps any output, it checks the SHA-256 digest recorded for that file.

## Reading the results

- Error metrics are in the original unit of the target. The residual is prediction minus observation.
- Pooled out-of-fold metrics are reported separately from the mean of fold metrics.
- Training results (the model reapplied to its own training data) are shown separately and are not validation performance.
- The coefficient of determination (R²) is not the squared Pearson correlation and can be negative.
- A prediction interval is not a confidence interval. Read empirical coverage together with interval width.
- The report is generated from the records of the completed computation. It does not replace the methods text of a paper or a pedological interpretation.

## Files and logs

| What | Where |
|---|---|
| Runs | the output folder chosen on tab 1 (default `%LOCALAPPDATA%\SpatialPredictionStudio\runs`) |
| Panel logs | `%LOCALAPPDATA%\SpatialPredictionStudio\logs` |
| Example copies | `%LOCALAPPDATA%\SpatialPredictionStudio\examples` |
| Projects | wherever you save the `*.sps.json` file |
