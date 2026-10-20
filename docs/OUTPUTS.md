# Outputs, reports and model packages

Each run writes its files to a new folder inside the output folder chosen on the **1 Project** tab (default `%LOCALAPPDATA%\SpatialPredictionStudio\runs`). The job folder holds the request and an `output` folder with the results and the progress log (`progress.jsonl`). `output\result.json` lists every output with its SHA-256 digest. The panel checks these digests before it opens or maps a file.

Which files appear depends on the settings: interval rasters need an interval method, applicability rasters need the area of applicability option, and so on.

## Rasters added to the map

| File | Layer name in ArcMap |
|---|---|
| `prediction.tif` | Final prediction |
| `prediction_lower.tif`, `prediction_upper.tif` | Prediction interval lower bound, upper bound |
| `prediction_interval_width.tif` | Prediction interval width |
| `nearest_sample_distance.tif` | Distance to nearest training location |
| `range_extrapolation.tif` | Extrapolation beyond the training range |
| `unknown_category.tif` | Category not seen in training |
| `physical_violation.tif` | Prediction outside the physical bounds |
| `quantile_crossing.tif` | Crossing quantiles (quantile interval methods) |
| `applicability.tif`, `dissimilarity.tif` | Area of applicability (1: inside, 0: outside); environmental dissimilarity (higher value: more different) |
| `interval_available.tif` | Local prediction interval (1: available, 0: not available), spatially weighted conformal method only |
| `interval_effective_n.tif`, `interval_effective_group_n.tif` | Effective number of observations and of groups in local calibration, same method |

The fold preview writes `folds.geojson`, drawn on the map as one layer per repeat.

## Reports

| File | Content |
|---|---|
| `report.html` | Run report: data, validation design, metrics by scope, figures, uncertainty, mapping and notes on interpretation. |
| `study_report_index.html` and `study_report_page_NNN.html` | Detailed study report in local pages with a table of contents; every table is complete. Open it with **Open report**. |
| `study_report.html` | The same detailed study report as one page. |
| `figures/*.png`, `figures/*.svg` | Report figures: observed against predicted, residual histograms and maps, fold maps, coverage calibration. PNG at the chosen resolution and SVG vector graphics. |

The detailed study report records every setting that was requested, searched, selected and effectively used, all metric definitions, runtime and versions, and the limits of interpretation. It is written so that the methods of a paper can be checked against it.

## Tables

All tables are CSV (UTF-8) unless stated. Text that starts like a spreadsheet formula gets a leading single quote in CSV files; the JSON files keep the full value.

| File | Content |
|---|---|
| `sampled_data.csv`, `sampled_data.json` | Each observation with its ID, group, coordinates, observed target, dates, role, covariate values and original attributes. |
| `data_dictionary.json` | Meaning, unit and scope of every column. |
| `excluded_samples.csv`, `.json` | Observations excluded before modeling, with the reason. |
| `validation_folds.csv`, `.json` | Repeat, fold and role (training or test) of every observation. |
| `data_roles.csv`, `role_data.csv`, `learning_membership.csv` | Which observations took part in which step: outer folds, inner folds, calibration, final model. |
| `oof_predictions.csv` | Out-of-fold predictions of the outer folds: observed, predicted, residual and interval. |
| `independent_predictions.csv` | Predictions for the locked independent test set. |
| `metrics.csv`, `publication_metrics.csv` | Metrics by scope (pooled out-of-fold, per repeat, per fold, mean of folds, independent test), with labels, units and reasons when a metric is undefined. |
| `fold_metrics.csv` | Metrics and model record of each outer fold. |
| `training_metrics.csv`, `training_predictions.csv` | The model reapplied to its training data. These are not validation results. |
| `calibration_predictions.csv` | Predictions for the observations held out for interval calibration. |
| `inner_validation_predictions.csv`, `inner_fold_results.csv`, `trial_results.csv` | Inner validation of each search trial. |
| `optimization_trials.csv`, `.json` | Every search trial with its parameters and inner metrics. |
| `hyperparameters.csv` | Requested, searched, selected and effective parameters of every fitted model. |
| `selected_features.csv` | Covariates of the final model after selection. |
| `coverage_diagnostics.csv`, `.json` | Empirical coverage, interval width and interval score at each nominal level. |
| `subgroup_diagnostics.csv`, `.json` | Error and coverage per category of the subgroup fields. |
| `permutation_importance.csv`, `.json`, `permutation_summary.csv` | Permutation variable importance on the outer folds. |
| `plain_model_comparison.csv`, `.json` | Regression kriging against its base model on the same outer observations. |
| `learning_audit.csv`, `.json` | What each model learned from which observations. |
| `final_record.json` | Full record of the final model: preprocessing, selection, transformation, model and training IDs. |
| `evaluation.json` | Full evaluation record including the settings used. |
| `study_summary.json` | Summary of the study for the report. |
| `preflight.json` | Results of the preflight check: counts, missing data, depth, method, support, alignment, workload and warnings. |
| `audit.json` | Source file digests, package and code versions, run times, operating system and hardware. |

## Packages

| File | Content |
|---|---|
| `model.joblib`, `model.joblib.meta.json` | The final model and its metadata (covariates, versions, digest). The model is loaded only after its digest is checked. |
| `model_package.spsmodel.zip` | Model package: model, metadata and a manifest with SHA-256 digests. **Save model** on tab 10 exports it. |
| `study_package.spsstudy.zip` | Study package: reports, tables, maps and the model package of the run, with a manifest of member digests. **Save study package** exports it. |

## Metric definitions

Residual: e_i = p_i - y_i, prediction minus observation. n is the number of evaluation records. Definitions as implemented in `backend/sps/publication_report.py`:

| Metric | Formula | Unit |
|---|---|---|
| Root mean squared error (RMSE) | sqrt(sum(e_i^2)/n) | target unit |
| Mean squared error (MSE) | sum(e_i^2)/n | squared target unit |
| Mean absolute error (MAE) | sum(abs(e_i))/n | target unit |
| Median absolute error | median(abs(e_i)) | target unit |
| Mean error (ME) | sum(e_i)/n | target unit |
| Sample standard deviation of residuals | sqrt(sum((e_i-mean(e))^2)/(n-1)) | target unit |
| Coefficient of determination (R²) | 1-sum((p_i-y_i)^2)/sum((y_i-mean(y))^2) | dimensionless |
| Pearson correlation coefficient (r) | sum((y_i-mean(y))*(p_i-mean(p)))/sqrt(sum((y_i-mean(y))^2)*sum((p_i-mean(p))^2)) | dimensionless |
| Squared Pearson correlation coefficient (r²) | pearson_r^2 | dimensionless |
| Lin's concordance correlation coefficient (CCC) | 2*cov_n(y,p)/(var_n(y)+var_n(p)+(mean(y)-mean(p))^2) | dimensionless |
| Ratio of the observed sample standard deviation to RMSE (RPD) | s_y/RMSE; s_y=sqrt(sum((y_i-mean(y))^2)/(n-1)) | dimensionless |
| Ratio of the interquartile range (IQR) to RMSE (RPIQ) | (Q_0.75(y)-Q_0.25(y))/RMSE | dimensionless |
| Empirical coverage | sum(I(lower_i<=y_i<=upper_i))/n | dimensionless |
| Mean prediction interval width | sum(upper_i-lower_i)/n | target unit |
| Median prediction interval width | median(upper_i-lower_i) | target unit |
| Fraction below the lower bound | sum(I(y_i<lower_i))/n | dimensionless |
| Fraction above the upper bound | sum(I(y_i>upper_i))/n | dimensionless |
| Interval score | mean((upper-lower)+(2/alpha)*(lower-y)*I(y<lower)+(2/alpha)*(y-upper)*I(y>upper)); alpha=1-nominal coverage | target unit |
| Lower pinball loss | mean(max(tau*(y-q),(tau-1)*(y-q))); tau=alpha/2 | target unit |
| Upper pinball loss | mean(max(tau*(y-q),(tau-1)*(y-q))); tau=1-alpha/2 | target unit |

R² is not the squared Pearson correlation and can be negative; it is undefined for a constant target or a single observation. In Lin's coefficient the divisor of the variance and covariance is n; in standard deviations it is n-1. A result that is undefined (zero denominator, too few records, constant variable) is stored as null with a reason code; it is never replaced with zero.

The sources of CCC, RPD and RPIQ, the interval score and the pinball loss are listed in [METHODS.md](METHODS.md#sources-of-diagnostics-and-metrics).
