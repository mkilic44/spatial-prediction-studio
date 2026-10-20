using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using System.Windows.Forms;

namespace SpatialPredictionStudio
{
    public sealed partial class StudioControl
    {
        private bool projectLoadFailed;
        private int projectLoadDepth;
        private void SaveProject()
        {
            using (var dialog = new SaveFileDialog { Filter = "Spatial Prediction Studio project (*.sps.json)|*.sps.json", FileName = String.IsNullOrEmpty(projectPath) ? "project.sps.json" : Path.GetFileName(projectPath), InitialDirectory = String.IsNullOrEmpty(projectPath) ? localRoot : Path.GetDirectoryName(projectPath) })
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                SaveProjectFile(dialog.FileName, true);
            }
        }
        private void OpenProject()
        {
            using (var dialog = new OpenFileDialog { Filter = "Spatial Prediction Studio project (*.sps.json)|*.sps.json|JSON (*.json)|*.json", InitialDirectory = localRoot })
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                LoadProjectFile(dialog.FileName);
            }
        }
        private void LoadProjectFile(string filename)
        { LoadProjectFile(filename, CancellationToken.None); }
        private void LoadProjectFile(string filename, CancellationToken cancellation)
        {
            cancellation.ThrowIfCancellationRequested();
            var value = Json.Read(filename); cancellation.ThrowIfCancellationRequested();
            if (value.ContainsKey("window_size")) {
                decimal size = Convert.ToDecimal(value["window_size"]);
                if (size < windowSize.Minimum || size > windowSize.Maximum || size != Decimal.Truncate(size)) throw new InvalidDataException("The project's raster window size is invalid.");
            }
            Dictionary<string, object> previous = null; string previousPath = projectPath;
            try { previous = ProjectRecord(); } catch (Exception) { /* An incomplete initial panel has no restorable project. */ }
            cancellation.ThrowIfCancellationRequested();
            projectLoadDepth++;
            try {
                LoadProjectRecord(value, filename, cancellation); cancellation.ThrowIfCancellationRequested();
                projectLoadFailed = false; UpdateGate();
            }
            catch (Exception failure) {
                projectLoadFailed = true; projectPath = null;
                if (previous != null) {
                    // Recovery must finish even when the caller's request has expired.
                    try { LoadProjectRecord(previous, previousPath, CancellationToken.None); projectLoadFailed = false; }
                    catch (Exception recovery) { Log("Could not restore the previous project: " + recovery.Message); }
                }
                InvalidatePreview("The preview must be refreshed after a failed project open.");
                string message = projectLoadFailed
                    ? "Could not open the project. Incomplete settings cannot be saved or run; open a valid project."
                    : "Could not open the project. The previous project settings were restored; refresh the preview.";
                state.Text = message;
                if (failure is OperationCanceledException) throw new OperationCanceledException(message, failure, cancellation);
                throw new InvalidDataException(message + " " + failure.Message, failure);
            }
            finally { projectLoadDepth--; }
        }
        private void LoadProjectRecord(Dictionary<string, object> value, string filename)
        { LoadProjectRecord(value, filename, CancellationToken.None); }
        private void LoadProjectRecord(Dictionary<string, object> value, string filename, CancellationToken cancellation)
        {
                cancellation.ThrowIfCancellationRequested();
                int schema = value.ContainsKey("schema_version") ? Convert.ToInt32(value["schema_version"]) : 0;
                if (Json.Text(value, "project_type") != "SpatialPredictionStudio" || (schema != 1 && schema != 2)) throw new InvalidDataException("Unsupported project format.");
                InvalidatePreview("Opening the project. A saved preview is not reused."); loading = true;
                try {
                    bool csv = schema == 2 && Json.Text(value, "input_type") == "csv"; bool selectedOnly = !csv && value.ContainsKey("selected_only") && Convert.ToBoolean(value["selected_only"]);
                    var sample = schema == 2 ? (Dictionary<string, object>)value["samples"] : new Dictionary<string, object> { { "id_field", value["id_field"] }, { "target_field", value["target_field"] }, { "unit", value["unit"] } };
                    RestoreSampleMetadata(sample);
                    cancellation.ThrowIfCancellationRequested();
                    var requested = ((IEnumerable)value["covariates"]).Cast<Dictionary<string, object>>().ToList();
                    foreach (var covariate in requested) CovariateMetadata(covariate);
                    string idField = Json.Text(sample, "id_field"), targetField = Json.Text(sample, "target_field");
                    PointScope scope = null; PointLayerInfo chosenPoint = null;
                    List<PointLayerInfo> pointList; List<RasterBandInfo> rasterList; adapter.Inventory(out pointList, out rasterList, Log);
                    cancellation.ThrowIfCancellationRequested();
                    if (csv) { csvPath.Text = Json.Text(value, "csv_path"); LoadCsv(); cancellation.ThrowIfCancellationRequested(); Choose(inputType, "csv"); }
                    else {
                        object scopeRecord; value.TryGetValue("point_scope", out scopeRecord); scope = PointScope.FromRecord(scopeRecord, selectedOnly);
                        string source = Json.Text(value, "point_source");
                        foreach (var candidate in pointList.Where(p => SamePath(p.SourcePath, source)))
                        {
                            cancellation.ThrowIfCancellationRequested();
                            try { if (adapter.CapturePointScope(candidate, idField, selectedOnly).EquivalentTo(scope)) { chosenPoint = candidate; break; } }
                            catch (InvalidDataException) { } catch (InvalidOperationException) { }
                        }
                        cancellation.ThrowIfCancellationRequested();
                        if (chosenPoint == null) chosenPoint = adapter.RestorePointScope(source, Json.Text(value, "point_name", "Project points"), idField, scope);
                        cancellation.ThrowIfCancellationRequested();
                        Choose(inputType, "map");
                    }
                    foreach (var raster in requested.GroupBy(c => Json.Text(c, "path"), StringComparer.OrdinalIgnoreCase)) {
                        cancellation.ThrowIfCancellationRequested();
                        if (!rasterList.Any(r => SamePath(r.Path, raster.Key))) adapter.AddRaster(raster.Key, Path.GetFileNameWithoutExtension(raster.Key));
                        cancellation.ThrowIfCancellationRequested();
                    }
                    RefreshMap(); cancellation.ThrowIfCancellationRequested();
                    if (!csv)
                    {
                        points.SelectedIndex = -1; foreach (PointLayerInfo point in points.Items) if (Object.ReferenceEquals(point.Layer, chosenPoint.Layer)) { points.SelectedItem = point; break; }
                        if (points.SelectedIndex < 0) throw new InvalidDataException("Could not read the restored point layer from the map.");
                    }
                    UpdateFields(); cancellation.ThrowIfCancellationRequested();
                    if (!identity.Items.Contains(idField) || !target.Items.Contains(targetField)) throw new InvalidDataException("The saved target or ID field is not in the source.");
                    identity.SelectedItem = idField; target.SelectedItem = targetField; targetUnit.Text = Json.Text(sample, "unit"); selected.Checked = selectedOnly;
                    groups.Values = Strings(sample, "group_fields"); Choose(support, Json.Text(sample, "support", "point")); supportNote.Text = Json.Text(sample, "support_note"); polygonPath.Text = Json.Text(sample, "polygon_path"); polygonId.Text = Json.Text(sample, "polygon_id_field");
                    independentPath.Text = Json.Text(sample, "independent_path"); Choose(unitField, Json.Text(sample, "unit_field"));
                    if (independentPath.Text.Length > 0) { LoadIndependentFields(); independentCrs.Text = Json.Text(sample, "independent_crs"); if (sample.ContainsKey("independent_x_field")) { if (!independentX.Items.Contains(Json.Text(sample, "independent_x_field")) || !independentY.Items.Contains(Json.Text(sample, "independent_y_field"))) throw new InvalidDataException("The saved coordinate fields of the independent table no longer exist."); independentX.SelectedItem = Json.Text(sample, "independent_x_field"); independentY.SelectedItem = Json.Text(sample, "independent_y_field"); } }
                    cancellation.ThrowIfCancellationRequested();
                    Choose(depthTop, Json.Text(sample, "depth_top_field")); Choose(depthBottom, Json.Text(sample, "depth_bottom_field")); Choose(laboratoryMethod, Json.Text(sample, "method_field")); Choose(dateField, Json.Text(sample, "date_field"));
                    if (csv) { if (!csvX.Items.Contains(Json.Text(sample, "x_field")) || !csvY.Items.Contains(Json.Text(sample, "y_field"))) throw new InvalidDataException("A coordinate field of the table no longer exists."); csvX.SelectedItem = Json.Text(sample, "x_field"); csvY.SelectedItem = Json.Text(sample, "y_field"); csvCrs.Text = Json.Text(sample, "crs"); }
                    RestoreCovariateRows(requested);
                    if (schema == 2) {
                        RestoreScience((Dictionary<string, object>)value["science"]); windowSize.Value = Convert.ToDecimal(value["window_size"]);
                        if (value.ContainsKey("custom_fold_draft")) customFolds.Restore((IEnumerable)value["custom_fold_draft"]);
                        previewManifest = Json.Text(value, "preview_manifest");
                    }
                    else { model.Select(Json.Text(value, "model", "ridge")); if (model.MethodId == "ridge") model.Parameters.Set("alpha", value["alpha"]); Log("Stage A project opened. The Stage B validation and preview must be prepared again."); }
                    outputRoot.Text = Json.Text(value, "output_root", outputRoot.Text); lastManifest = Json.Text(value, "last_manifest");
                    if (scope != null && !adapter.CapturePointScope((PointLayerInfo)points.SelectedItem, idField, selectedOnly).EquivalentTo(scope)) throw new InvalidDataException("The opened observation scope does not match the saved scope.");
                    cancellation.ThrowIfCancellationRequested();
                    projectPath = filename; outputArtifacts.Clear(); outputTables.Items.Clear(); outputReports.Items.Clear(); results.DataSource = null;
                    Log("Project sources, observation scope and scientific settings restored: " + projectPath); state.Text = "Project opened. Create a current fold preview."; tabs.SelectedIndex = 0;
                } finally { loading = false; InvalidatePreview("Sources must be verified again after the project opens."); }
        }
        private static string[] Strings(Dictionary<string, object> value, string key) { return value.ContainsKey(key) && value[key] is IEnumerable && !(value[key] is string) ? ((IEnumerable)value[key]).Cast<object>().Select(Convert.ToString).ToArray() : new string[0]; }
        private static Dictionary<string, object> Except(Dictionary<string, object> value, params string[] keys) { return value.Where(p => !keys.Contains(p.Key)).ToDictionary(p => p.Key, p => p.Value); }
        private void RestoreScience(Dictionary<string, object> science)
        {
            var validate = (Dictionary<string, object>)science["validation"]; validation.Restore(Except(validate, "purpose", "independent_ids", "custom_folds")); Choose(purpose, Json.Text(validate, "purpose", "within_area")); independent.Values = Strings(validate, "independent_ids");
            if (validate.ContainsKey("custom_folds")) customFolds.Restore((IEnumerable)validate["custom_folds"]); else customFolds.Restore(new object[0]);
            preprocessing.Restore((Dictionary<string, object>)science["preprocessing"], null); targetTransform.Restore((Dictionary<string, object>)science["target_transform"]); featureSelection.Restore((Dictionary<string, object>)science["feature_selection"]); vif.Restore((Dictionary<string, object>)science["vif"], "mode");
            var modelRecord = (Dictionary<string, object>)science["model"]; model.Select(Json.Text(modelRecord, "id")); model.Parameters.SetValues((Dictionary<string, object>)modelRecord["parameters"]);
            candidates.Restore(science.ContainsKey("model_candidates") ? (IEnumerable)science["model_candidates"] : new object[0]);
            var search = (Dictionary<string, object>)science["optimization"]; optimization.Restore(Except(search, "spaces")); RefreshSpaces(); spaces.Restore((Dictionary<string, object>)search["spaces"]);
            uncertainty.Restore((Dictionary<string, object>)science["uncertainty"]);
            var mapping = (Dictionary<string, object>)science["mapping"]; align.Checked = mapping.ContainsKey("align_rasters") && Convert.ToBoolean(mapping["align_rasters"]); mask.Checked = Convert.ToBoolean(mapping["mask_extrapolation"]); Choose(figureDpi, Convert.ToString(mapping["figure_dpi"]));
            maxMissing.Value = mapping.ContainsKey("max_missing_fraction") ? Convert.ToDecimal(mapping["max_missing_fraction"]) : 0.5M;
            if (Json.Text(mapping, "reference_id").Length > 0) Choose(referenceRaster, Json.Text(mapping, "reference_id")); physicalMinUse.Checked = mapping.ContainsKey("physical_min"); physicalMaxUse.Checked = mapping.ContainsKey("physical_max"); if (physicalMinUse.Checked) physicalMin.Value = Convert.ToDecimal(mapping["physical_min"]); if (physicalMaxUse.Checked) physicalMax.Value = Convert.ToDecimal(mapping["physical_max"]);
            var diagnostics = science.ContainsKey("diagnostics") ? science["diagnostics"] as Dictionary<string, object> : new Dictionary<string, object>();
            if (diagnostics == null) throw new InvalidDataException("Saved diagnostic settings are invalid.");
            applicability.Checked = diagnostics.ContainsKey("applicability") && Convert.ToBoolean(diagnostics["applicability"]); permutation.Checked = diagnostics.ContainsKey("permutation") && Convert.ToBoolean(diagnostics["permutation"]);
            permutationRepeats.Value = diagnostics.ContainsKey("permutation_repeats") ? Convert.ToDecimal(diagnostics["permutation_repeats"]) : 5;
            minSubgroupSamples.Value = diagnostics.ContainsKey("min_subgroup_samples") ? Convert.ToDecimal(diagnostics["min_subgroup_samples"]) : 10; minSubgroupGroups.Value = diagnostics.ContainsKey("min_subgroup_groups") ? Convert.ToDecimal(diagnostics["min_subgroup_groups"]) : 3;
            subgroupFields.Values = Strings(diagnostics, "subgroup_fields");
        }
    }
}
