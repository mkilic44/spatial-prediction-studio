using System;
using System.Collections;
using System.Collections.Generic;
using System.Data;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Threading;
using System.Windows.Forms;

namespace SpatialPredictionStudio
{
    public sealed partial class StudioControl
    {
        private readonly Dictionary<string, Dictionary<string, object>> outputArtifacts = new Dictionary<string, Dictionary<string, object>>();
        private static readonly string[] CovariateMetadataKeys = { "resampling", "sha256", "band_description", "time_representation", "original_resolution", "provenance", "target_derived" };
        private object savedExclusions;
        private bool hasSavedExclusions;
        private static object CopyJsonValue(object value)
        {
            var dictionary = value as IDictionary<string, object>;
            if (dictionary != null) return dictionary.ToDictionary(p => p.Key, p => CopyJsonValue(p.Value));
            var sequence = value as IEnumerable;
            if (sequence != null && !(value is string)) return sequence.Cast<object>().Select(CopyJsonValue).ToArray();
            return value;
        }
        private static void CheckKeys(Dictionary<string, object> value, IEnumerable<string> allowed, string where)
        {
            var unknown = value.Keys.Except(allowed).ToArray();
            if (unknown.Length > 0) throw new InvalidDataException(where + ": unsupported settings cannot be used because they would not be preserved: " + String.Join(", ", unknown));
        }
        private static Dictionary<string, object> CovariateMetadata(Dictionary<string, object> value)
        {
            CheckKeys(value, new[] { "id", "name", "path", "band", "unit", "kind", "sampling" }.Concat(CovariateMetadataKeys), "Raster");
            object targetDerived;
            if (value.TryGetValue("target_derived", out targetDerived) && targetDerived != null && !(targetDerived is bool)) throw new InvalidDataException("target_derived must be a Boolean value: true or false.");
            if (value.ContainsKey("resampling") && !new[] { "nearest", "bilinear" }.Contains(value["resampling"] as string)) throw new InvalidDataException("Unsupported raster resampling method.");
            return value.Where(p => CovariateMetadataKeys.Contains(p.Key)).ToDictionary(p => p.Key, p => CopyJsonValue(p.Value));
        }
        private void RestoreSampleMetadata(Dictionary<string, object> sample)
        {
            CheckKeys(sample, new[] { "path", "independent_path", "independent_x_field", "independent_y_field", "independent_crs", "id_field", "target_field", "unit", "unit_field", "x_field", "y_field", "crs", "group_fields", "depth_top_field", "depth_bottom_field", "method_field", "date_field", "support", "support_note", "excluded", "polygon_path", "polygon_id_field" }, "Observations");
            object exclusions; bool present = sample.TryGetValue("excluded", out exclusions);
            if (present)
            {
                if (!(exclusions is IEnumerable) || exclusions is string || exclusions is IDictionary) throw new InvalidDataException("Excluded observations must be a list of IDs and reasons.");
                foreach (object entry in (IEnumerable)exclusions)
                {
                    var item = entry as Dictionary<string, object>;
                    if (item == null) throw new InvalidDataException("Each excluded observation must have an ID and a reason.");
                    CheckKeys(item, new[] { "id", "reason" }, "Excluded observation");
                    if (!item.ContainsKey("id") || !(item["id"] is string) || String.IsNullOrWhiteSpace((string)item["id"]) || !item.ContainsKey("reason") || !(item["reason"] is string) || String.IsNullOrWhiteSpace((string)item["reason"])) throw new InvalidDataException("The ID and reason of an excluded observation must be nonempty text.");
                }
            }
            hasSavedExclusions = present; savedExclusions = present ? CopyJsonValue(exclusions) : null;
            sampleExclusions.Text = !present ? "No saved exclusions." : String.Join(Environment.NewLine, ((IEnumerable)savedExclusions).Cast<Dictionary<string, object>>().Select(e => (string)e["id"] + ": " + (string)e["reason"]));
            if (present && sampleExclusions.Text.Length == 0) sampleExclusions.Text = "The saved exclusion list is empty.";
        }
        private void UpdateMetadataDetails()
        {
            var row = rasters.CurrentRow;
            var value = row == null ? new Dictionary<string, object>() : row.Tag as Dictionary<string, object> ?? new Dictionary<string, object>();
            var labels = new Dictionary<string, string> { { "time_representation", "Time representation" }, { "original_resolution", "Original resolution" }, { "provenance", "Data source and processing history" }, { "target_derived", "Derived from target observations" }, { "resampling", "Resampling for alignment" }, { "sha256", "Source file digest" }, { "band_description", "Recorded band description" } };
            covariateMetadata.Text = String.Join(Environment.NewLine, labels.Select(p => p.Value + ": " + (!value.ContainsKey(p.Key) ? "Not recorded" : value[p.Key] == null ? "Not recorded (null)" : value[p.Key] is bool ? ((bool)value[p.Key] ? "Yes" : "No") : value[p.Key] is string ? (string)value[p.Key] : Json.Serializer().Serialize(value[p.Key]))));
        }
        private Dictionary<string, object> ReadCovariateRow(DataGridViewRow row)
        {
            Func<string, string> text = key => Convert.ToString(row.Cells[key].Value, CultureInfo.InvariantCulture)?.Trim() ?? "";
            var value = new Dictionary<string, object> { { "id", text("id") }, { "name", text("name") }, { "path", text("path") }, { "band", Convert.ToInt32(row.Cells["band"].Value) }, { "unit", text("unit") }, { "kind", text("kind") }, { "sampling", text("sampling") } };
            var metadata = row.Tag as Dictionary<string, object>;
            if (metadata != null) foreach (var pair in CovariateMetadata(metadata)) value[pair.Key] = pair.Value;
            return value;
        }
        private void RefreshRasterRows(IEnumerable<RasterBandInfo> rasterList)
        {
            rasters.EndEdit();
            var saved = rasters.Rows.Cast<DataGridViewRow>().Select(r => Tuple.Create(ReadCovariateRow(r), Convert.ToBoolean(r.Cells["use"].Value ?? false))).ToArray();
            bool before = loading; loading = true;
            try {
                rasters.Rows.Clear();
                foreach (var raster in rasterList)
                {
                    var old = saved.FirstOrDefault(c => SamePath(Json.Text(c.Item1, "path"), raster.Path) && Convert.ToInt32(c.Item1["band"]) == raster.Band);
                    var value = old == null ? null : old.Item1;
                    int index = rasters.Rows.Add(old != null && old.Item2, value == null ? raster.Name : Json.Text(value, "name"), raster.Band, value == null ? raster.Unit : Json.Text(value, "unit"), value == null ? raster.Id : Json.Text(value, "id"), value == null ? "continuous" : Json.Text(value, "kind", "continuous"), value == null ? "nearest" : Json.Text(value, "sampling", "nearest"), raster.Path);
                    rasters.Rows[index].Tag = value == null ? null : CovariateMetadata(value);
                }
                UpdateMetadataDetails();
            } finally { loading = before; }
        }
        private void RestoreCovariateRows(List<Dictionary<string, object>> requested)
        {
            foreach (var value in requested) CovariateMetadata(value);
            bool before = loading; loading = true;
            try {
                foreach (DataGridViewRow row in rasters.Rows)
                {
                    var match = requested.FirstOrDefault(c => SamePath(Json.Text(c, "path"), Convert.ToString(row.Cells["path"].Value)) && Convert.ToInt32(c["band"]) == Convert.ToInt32(row.Cells["band"].Value));
                    row.Cells["use"].Value = match != null; row.Tag = match == null ? null : CovariateMetadata(match);
                    if (match != null) foreach (string field in new[] { "name", "id", "unit", "kind", "sampling" }) row.Cells[field].Value = Json.Text(match, field, field == "kind" ? "continuous" : field == "sampling" ? "nearest" : "");
                }
                if (GetSelectedCovariates(false).Count != requested.Count) throw new InvalidDataException("Not all saved raster bands were found.");
                UpdateMetadataDetails(); RefreshCovariateChoices();
            } finally { loading = before; }
        }
        private void RefreshMap()
        {
            Safe(() => {
                bool before = loading; loading = true;
                try {
                    var old = points.SelectedItem as PointLayerInfo;
                    List<PointLayerInfo> pointList; List<RasterBandInfo> rasterList; adapter.Inventory(out pointList, out rasterList, Log);
                    points.Items.Clear(); foreach (var point in pointList) points.Items.Add(point);
                    points.SelectedIndex = pointList.FindIndex(p => old != null && Object.ReferenceEquals(p.Layer, old.Layer));
                    if (points.SelectedIndex < 0) points.SelectedIndex = pointList.FindIndex(p => old != null && String.Equals(p.SourcePath, old.SourcePath, StringComparison.OrdinalIgnoreCase));
                    if (points.SelectedIndex < 0 && points.Items.Count > 0) points.SelectedIndex = 0;
                    RefreshRasterRows(rasterList);
                    RefreshCovariateChoices(); Log("Map read. Point layers: " + pointList.Count + ", raster bands: " + rasterList.Count + ". Group layers are included.");
                } finally { loading = before; }
                InvalidatePreview("Map sources were read again.");
            });
        }
        private static bool SamePath(string a, string b) { return String.Equals(a, b, StringComparison.OrdinalIgnoreCase); }
        private void LoadCsv()
        {
            if (!File.Exists(csvPath.Text)) throw new FileNotFoundException("Observation table not found.", csvPath.Text);
            csvTable = TabularFiles.Read(csvPath.Text); Choose(inputType, "csv"); UpdateFields(); Log("Observation table read. Rows: " + csvTable.Rows.Count + ", fields: " + csvTable.Columns.Count + "."); InvalidatePreview("Observation table was read again.");
        }
        private void LoadIndependentFields()
        {
            independentX.Items.Clear(); independentY.Items.Clear();
            if (!File.Exists(independentPath.Text)) throw new FileNotFoundException("Independent test file not found.", independentPath.Text);
            if (Path.GetExtension(independentPath.Text).Equals(".csv", StringComparison.OrdinalIgnoreCase))
            {
                var table = TabularFiles.Read(independentPath.Text); foreach (DataColumn column in table.Columns) { independentX.Items.Add(column.ColumnName); independentY.Items.Add(column.ColumnName); }
                foreach (string name in new[] { "x", "longitude" }) if (independentX.Items.Contains(name)) { independentX.SelectedItem = name; break; }
                foreach (string name in new[] { "y", "latitude" }) if (independentY.Items.Contains(name)) { independentY.SelectedItem = name; break; }
            }
            InvalidatePreview("Independent test source was read again.");
        }
        private IEnumerable<Choice> FieldChoices(bool numeric)
        {
            if (IsCsv) return csvTable == null ? new Choice[0] : csvTable.Columns.Cast<DataColumn>().Select(c => new Choice(c.ColumnName)).ToArray();
            var point = points.SelectedItem as PointLayerInfo; return point == null ? new Choice[0] : adapter.Fields(point, numeric).Select(f => new Choice(f)).ToArray();
        }
        private IEnumerable<Choice> SampleChoices()
        {
            if (String.IsNullOrEmpty(identity.Text)) throw new InvalidDataException("Select the observation ID field first.");
            if (IsCsv)
            {
                if (!File.Exists(csvPath.Text)) throw new FileNotFoundException("Observation table not found.");
                var table = TabularFiles.Read(csvPath.Text); if (!table.Columns.Contains(identity.Text)) throw new InvalidDataException("ID field not found in the table.");
                return table.Rows.Cast<DataRow>().Select(row => Convert.ToString(row[identity.Text]).Trim()).Distinct().OrderBy(v => v, StringComparer.Ordinal).Select(v => new Choice(v)).ToArray();
            }
            var point = points.SelectedItem as PointLayerInfo; if (point == null) throw new InvalidDataException("Select a point layer.");
            return adapter.SampleIds(point, identity.Text, selected.Checked).Select(v => new Choice(v)).ToArray();
        }
        private void UpdateFields()
        {
            Safe(() => {
                string oldTarget = target.Text, oldId = identity.Text, oldX = csvX.Text, oldY = csvY.Text;
                var optional = new[] { depthTop, depthBottom, laboratoryMethod, dateField, unitField }; var previous = optional.Select(ChoiceId).ToArray();
                target.Items.Clear(); identity.Items.Clear(); csvX.Items.Clear(); csvY.Items.Clear();
                foreach (var field in FieldChoices(true)) target.Items.Add(field.Id);
                var names = FieldChoices(false).Select(c => c.Id).ToArray(); foreach (string name in names) { identity.Items.Add(name); csvX.Items.Add(name); csvY.Items.Add(name); }
                SelectItem(target, oldTarget, "soil_value"); SelectItem(identity, oldId, "sample_id"); SelectItem(csvX, oldX, "x", "longitude"); SelectItem(csvY, oldY, "y", "latitude");
                for (int i = 0; i < optional.Length; ++i) { optional[i].Items.Clear(); AddChoices(optional[i], new Choice("", "Not selected")); foreach (string name in names) optional[i].Items.Add(new Choice(name)); if (names.Contains(previous[i])) Choose(optional[i], previous[i]); }
            });
        }
        private static void SelectItem(ComboBox combo, params string[] values) { foreach (string value in values) if (!String.IsNullOrWhiteSpace(value) && combo.Items.Contains(value)) { combo.SelectedItem = value; return; } if (combo.Items.Count > 0) combo.SelectedIndex = 0; }
        private IEnumerable<Choice> CovariateChoices() { return GetSelectedCovariates(false).Select(c => new Choice(Json.Text(c, "id"), Json.Text(c, "name") + " [" + Json.Text(c, "id") + "]")).ToArray(); }
        private void RefreshCovariateChoices()
        {
            string old = ChoiceId(referenceRaster); referenceRaster.Items.Clear(); foreach (var item in CovariateChoices()) referenceRaster.Items.Add(item);
            var existing = referenceRaster.Items.Cast<Choice>().FirstOrDefault(c => c.Id == old); if (existing != null) referenceRaster.SelectedItem = existing; else if (referenceRaster.Items.Count > 0) referenceRaster.SelectedIndex = 0;
        }
        private List<Dictionary<string, object>> GetSelectedCovariates(bool validate = true)
        {
            rasters.EndEdit(); var values = new List<Dictionary<string, object>>(); var ids = new HashSet<string>(StringComparer.Ordinal);
            foreach (DataGridViewRow row in rasters.Rows)
            {
                if (!Convert.ToBoolean(row.Cells["use"].Value ?? false)) continue;
                Func<string, string> text = key => Convert.ToString(row.Cells[key].Value, CultureInfo.InvariantCulture)?.Trim() ?? "";
                string id = text("id"), path = text("path"), kind = text("kind"), sampling = text("sampling");
                if (validate && (id.Length == 0 || !ids.Add(id))) throw new InvalidDataException("Raster IDs must be nonempty and unique.");
                if (validate && !File.Exists(path)) throw new FileNotFoundException("Raster file not found.", path);
                if (validate && (text("name").Length == 0 || text("unit").Length == 0)) throw new InvalidDataException("Each raster band needs a scientific name and a unit.");
                if (validate && kind == "categorical" && sampling != "nearest") throw new InvalidDataException("A categorical raster can only be sampled with the nearest cell: " + id);
                var value = ReadCovariateRow(row);
                if (validate && kind == "categorical" && Json.Text(value, "resampling", "nearest") != "nearest") throw new InvalidDataException("For alignment, a categorical raster can only be resampled with the nearest cell: " + id);
                values.Add(value);
            }
            if (validate && values.Count == 0) throw new InvalidDataException("Select at least one raster band."); return values;
        }
        private void RefreshSpaces()
        {
            if (model == null || candidates == null) return;
            var definitions = new Dictionary<string, Dictionary<string, object>>(); var choices = candidates.Values();
            if (choices.Count == 0 && model.SelectedMethod != null) choices.Add(new Dictionary<string, object> { { "id", model.MethodId } });
            foreach (var choice in choices)
            {
                var spec = registry.Category("model").Single(m => m.Id == Json.Text(choice, "id"));
                foreach (var parameter in spec.Parameters) definitions[(choices.Count > 1 ? spec.Id + "." : "") + parameter.Key] = (Dictionary<string, object>)parameter.Value;
            }
            foreach (var entry in new[] { new { Prefix = "feature_selection", Editor = featureSelection }, new { Prefix = "vif", Editor = vif }, new { Prefix = "preprocessing", Editor = preprocessing } })
                if (entry.Editor?.SelectedMethod != null) foreach (var parameter in entry.Editor.SelectedMethod.Parameters) if (new[] { "k", "threshold", "knn_neighbors" }.Contains(parameter.Key)) definitions[entry.Prefix + "." + parameter.Key] = (Dictionary<string, object>)parameter.Value;
            spaces.SetParameters(definitions);
        }
        private Dictionary<string, object> SampleSettings()
        {
            if (target.Text.Length == 0 || identity.Text.Length == 0 || target.Text == identity.Text) throw new InvalidDataException("Target and ID must be different fields.");
            if (targetUnit.Text.Trim().Length == 0) throw new InvalidDataException("Enter the target unit.");
            var value = new Dictionary<string, object> { { "id_field", identity.Text }, { "target_field", target.Text }, { "unit", targetUnit.Text.Trim() }, { "group_fields", groups.Values }, { "support", ChoiceId(support) }, { "support_note", supportNote.Text.Trim() } };
            foreach (var item in new[] { new { Key = "depth_top_field", Control = depthTop }, new { Key = "depth_bottom_field", Control = depthBottom }, new { Key = "method_field", Control = laboratoryMethod }, new { Key = "date_field", Control = dateField } }) if (ChoiceId(item.Control).Length > 0) value[item.Key] = ChoiceId(item.Control);
            if (ChoiceId(support) == "parcel_mean") { value["polygon_path"] = polygonPath.Text.Trim(); value["polygon_id_field"] = polygonId.Text.Trim(); if (!File.Exists(polygonPath.Text)) throw new FileNotFoundException("Parcel file not found.", polygonPath.Text); }
            if (!String.IsNullOrWhiteSpace(independentPath.Text)) {
                if (!File.Exists(independentPath.Text)) throw new FileNotFoundException("Independent test file not found.", independentPath.Text); value["independent_path"] = Path.GetFullPath(independentPath.Text);
                if (Path.GetExtension(independentPath.Text).Equals(".csv", StringComparison.OrdinalIgnoreCase)) {
                    if (independentX.Text.Length == 0 || independentY.Text.Length == 0 || independentX.Text == independentY.Text || independentCrs.Text.Trim().Length == 0) throw new InvalidDataException("The independent table needs distinct X and Y fields and an explicit coordinate reference system (CRS).");
                    value["independent_x_field"] = independentX.Text; value["independent_y_field"] = independentY.Text;
                }
                if (independentCrs.Text.Trim().Length > 0) value["independent_crs"] = independentCrs.Text.Trim();
            }
            if (ChoiceId(unitField).Length > 0) value["unit_field"] = ChoiceId(unitField);
            if (IsCsv) { value["x_field"] = csvX.Text; value["y_field"] = csvY.Text; value["crs"] = csvCrs.Text.Trim(); if (csvX.Text == csvY.Text || csvX.Text.Length == 0 || csvY.Text.Length == 0 || csvCrs.Text.Trim().Length == 0) throw new InvalidDataException("The table needs distinct X and Y fields and a coordinate reference system (CRS)."); }
            if (hasSavedExclusions) value["excluded"] = CopyJsonValue(savedExclusions);
            return value;
        }
        private Dictionary<string, object> ScienceSettings()
        {
            var validate = validation.Configuration(); validate["purpose"] = ChoiceId(purpose); validate["independent_ids"] = independent.Values;
            if (validation.MethodId == "custom") validate["custom_folds"] = customFolds.Values();
            var search = optimization.Configuration(); search["spaces"] = spaces.Values();
            var mapping = new Dictionary<string, object> { { "align_rasters", align.Checked }, { "reference_id", ChoiceId(referenceRaster) }, { "mask_extrapolation", mask.Checked }, { "figure_dpi", Int32.Parse(ChoiceId(figureDpi), CultureInfo.InvariantCulture) }, { "max_missing_fraction", (double)maxMissing.Value } };
            if (physicalMinUse.Checked) mapping["physical_min"] = (double)physicalMin.Value; if (physicalMaxUse.Checked) mapping["physical_max"] = (double)physicalMax.Value;
            if (physicalMinUse.Checked && physicalMaxUse.Checked && physicalMin.Value >= physicalMax.Value) throw new InvalidDataException("The physical lower bound must be less than the physical upper bound.");
            var candidateValues = candidates.Values();
            var result = new Dictionary<string, object> { { "validation", validate }, { "preprocessing", preprocessing.Configuration(null) }, { "target_transform", targetTransform.Configuration() }, { "feature_selection", featureSelection.Configuration() }, { "vif", vif.Configuration("mode") }, { "model", new Dictionary<string, object> { { "id", model.MethodId }, { "parameters", model.Parameters.Values() } } }, { "optimization", search }, { "uncertainty", uncertainty.Configuration() }, { "mapping", mapping } };
            result["diagnostics"] = new Dictionary<string, object> { { "applicability", applicability.Checked }, { "permutation", permutation.Checked }, { "permutation_repeats", (int)permutationRepeats.Value }, { "subgroup_fields", subgroupFields.Values }, { "min_subgroup_samples", (int)minSubgroupSamples.Value }, { "min_subgroup_groups", (int)minSubgroupGroups.Value } };
            if (candidateValues.Count > 0) result["model_candidates"] = candidateValues;
            return result;
        }
        private string CurrentSignature()
        {
            return ConfigurationIdentity.Digest(new Dictionary<string, object> { { "source", IsCsv ? csvPath.Text : ((PointLayerInfo)points.SelectedItem)?.SourcePath }, { "input_type", ChoiceId(inputType) }, { "selected_only", !IsCsv && selected.Checked }, { "samples", SampleSettings() }, { "covariates", GetSelectedCovariates() }, { "science", ScienceSettings() }, { "window_size", (int)windowSize.Value }, { "registry", registry.Digest } });
        }
        private string CurrentSources()
        {
            string source = IsCsv ? csvPath.Text : ((PointLayerInfo)points.SelectedItem)?.SourcePath; if (String.IsNullOrEmpty(source)) throw new InvalidDataException("No observation source selected.");
            var paths = GetSelectedCovariates(false).Select(c => Json.Text(c, "path")).Concat(new[] { source, RuntimeSettings.Load().RegistryPath, independentPath.Text, ChoiceId(support) == "parcel_mean" ? polygonPath.Text : "" });
            return ConfigurationIdentity.Digest(new Dictionary<string, object> { { "files", ConfigurationIdentity.SourceStamp(paths) }, { "scope", IsCsv ? "csv_all_rows" : adapter.ScopeStamp((PointLayerInfo)points.SelectedItem, selected.Checked) } });
        }
        private Dictionary<string, object> PrepareRequest(string action, string runId, string directory)
        { return PrepareRequest(action, runId, directory, CancellationToken.None); }
        private Dictionary<string, object> PrepareRequest(string action, string runId, string directory, CancellationToken cancellation)
        {
            cancellation.ThrowIfCancellationRequested();
            if (projectLoadFailed) throw new InvalidDataException("PROJECT_INCOMPLETE: Open a valid project first.");
            var samples = SampleSettings(); var covariates = GetSelectedCovariates();
            cancellation.ThrowIfCancellationRequested();
            if (covariates.Any(c => c.ContainsKey("target_derived") && c["target_derived"] is bool && (bool)c["target_derived"])) throw new InvalidDataException("TARGET_LEAKAGE: A raster derived from target observations cannot be used without evidence of independence.");
            var science = ScienceSettings(); cancellation.ThrowIfCancellationRequested(); Directory.CreateDirectory(directory);
            if (IsCsv) { if (!File.Exists(csvPath.Text)) throw new FileNotFoundException("Observation table not found.", csvPath.Text); samples["path"] = Path.GetFullPath(csvPath.Text); }
            else {
                var point = points.SelectedItem as PointLayerInfo; if (point == null) throw new InvalidDataException("Select a point layer.");
                string path = Path.Combine(directory, "samples.geojson"); var exported = adapter.ExportPoints(point, identity.Text, target.Text, selected.Checked, path);
                // ArcObjects must finish on its owner thread; a timed-out request must not start a worker afterwards.
                cancellation.ThrowIfCancellationRequested(); samples["path"] = path; samples["crs"] = exported.Crs;
                Log(exported.Count + " observations exported with all attributes on the main thread. Selected records only: " + selected.Checked);
            }
            science["schema_version"] = 2; science["task"] = "regression"; science["action"] = action; science["run_id"] = runId; science["samples"] = samples; science["covariates"] = covariates; science["output_dir"] = Path.Combine(directory, "output"); science["window_size"] = (int)windowSize.Value;
            if (action == "run") science["preview_fingerprint"] = previewFingerprint;
            cancellation.ThrowIfCancellationRequested(); return science;
        }
        private void Preflight()
        {
            RuntimeSettings.Load().Validate(); string directory = Path.Combine(localRoot, "preflight", Guid.NewGuid().ToString("N"));
            Json.Write(Path.Combine(Directory.CreateDirectory(directory).FullName, "request.json"), PrepareRequest("preview", "preflight", directory));
            state.Text = "Basic preflight check passed. Use Show folds to inspect the grid and fold boundaries."; Log(state.Text + " Saved to: " + directory);
        }
        private void StartJob(string action, bool remote = false)
        { StartJob(action, remote, CancellationToken.None); }
        private async void StartJob(string action, bool remote, CancellationToken cancellation)
        {
            if (busy || client.Running) return;
            lastJob = new Dictionary<string, object> { { "action", action }, { "status", "starting" } };
            bool workerAccepted = false;
            try {
                cancellation.ThrowIfCancellationRequested();
                var runtime = RuntimeSettings.Load(); runtime.Validate();
                if (!Path.IsPathRooted(outputRoot.Text)) throw new InvalidDataException("The output folder must be an absolute path.");
                string signature = CurrentSignature(); cancellation.ThrowIfCancellationRequested();
                string sources = CurrentSources(); cancellation.ThrowIfCancellationRequested();
                if (action == "run" && (String.IsNullOrEmpty(previewFingerprint) || signature != previewSignature || sources != previewSources)) { InvalidatePreview("Settings or data changed after the preview."); throw new InvalidOperationException("Create a new fold preview before running the model."); }
                if (action == "preview") InvalidatePreview("Preparing a new preview.");
                string runId = DateTime.Now.ToString("yyyyMMdd_HHmmss") + "_" + Guid.NewGuid().ToString("N").Substring(0, 8), directory = Path.Combine(Path.GetFullPath(outputRoot.Text), runId);
                if (Directory.Exists(directory)) throw new IOException("The job folder already exists.");
                var request = PrepareRequest(action, runId, directory, cancellation); cancellation.ThrowIfCancellationRequested();
                string requestPath = Path.Combine(directory, "request.json"); Json.Write(requestPath, request); cancellation.ThrowIfCancellationRequested();
                lastJob = new Dictionary<string, object> { { "id", runId }, { "action", action }, { "directory", directory }, { "status", "running" } };
                SetBusy(true); progress.Value = 0; state.Text = action == "preview" ? "Preparing fold preview..." : "Running model..."; tabs.SelectedTab = reportPage; Log("Starting job: " + action + "; " + requestPath);
                cancellation.ThrowIfCancellationRequested();
                // The request deadline protects preparation only. An accepted job has its own explicit cancellation lifecycle.
                var running = client.RunAsync(runtime, requestPath, Path.Combine(directory, "output"), runId, Log, (done, total, message) => OnUi(() => { if (total > 0) progress.Value = Math.Max(0, Math.Min(100, (int)(100.0 * done / total))); state.Text = message; }), cancellation);
                workerAccepted = true;
                var result = await running;
                // RunAsync resumes on the owning WinForms context. Keep failures in the job catch below.
                    string status = Json.Text(result, "status");
                    lastJob["status"] = status;
                    if (result.ContainsKey("error")) lastJob["error"] = result["error"];
                    if (status == "completed" && action == "preview")
                    {
                        if (CurrentSources() != sources || CurrentSignature() != signature) throw new InvalidOperationException("An observation or raster source changed while the job was running. The preview must be refreshed.");
                        int count = adapter.AddPreview(result, Path.Combine(directory, "arcmap_preview"));
                        string summary = PreviewSummary(result);
                        previewFingerprint = Json.Text(result, "preview_fingerprint"); previewSignature = signature; previewSources = sources; previewManifest = Json.Text(result, "manifest_path");
                        previewState.Text = summary; state.Text = "Fold preview added to the map. The model can now be run."; progress.Value = 100; Log(previewState.Text);
                        Log(state.Text + " Fold records: " + count + "; this is not the number of independent observations. Preview ID: " + previewFingerprint);
                    }
                    else if (status == "completed")
                    {
                        int count = adapter.AddResult(result); lastManifest = Json.Text(result, "manifest_path"); currentResult = CurrentSources() == sources && CurrentSignature() == signature;
                        resultState.Text = currentResult ? "Result completed with the current settings: " + runId : "A source changed during the job; the result was kept as a previous run.";
                        if (!currentResult) InvalidatePreview("A source changed during the job.");
                        PopulateOutputs(result); progress.Value = 100; state.Text = "Completed. Rasters added to the map: " + count + "."; Log(state.Text + " Result group read back from the map. Manifest: " + lastManifest);
                    }
                    else if (status == "cancelled") { state.Text = "Job canceled. The previous successful result is kept."; Log(state.Text); }
                    else { state.Text = "Job failed. The previous successful result is kept."; Log(state.Text + " " + (result.ContainsKey("error") ? Json.Serializer().Serialize(result["error"]) : "No error details.")); }
            } catch (OperationCanceledException ex) { OnUi(() => { lastJob["status"] = "cancelled"; lastJob["error"] = ex.Message; state.Text = workerAccepted ? "Job canceled; the worker was closed after a timeout." : "Job preparation canceled; the worker was not started."; Log(state.Text + " " + ex.Message); }); }
            catch (Exception ex) { OnUi(() => { lastJob["status"] = "failed"; lastJob["error"] = ex.Message; if (remote) { state.Text = "Error: " + ex.Message; Log(ex.ToString()); } else Fail(ex); }); }
            finally { OnUi(() => SetBusy(false)); }
        }
        private void Cancel() { client.Cancel(); cancel.Enabled = false; state.Text = "Cancellation requested; waiting for the worker to close."; Log(state.Text); }
        private string PreviewSummary(Dictionary<string, object> result)
        {
            var output = ((IEnumerable)result["outputs"]).Cast<Dictionary<string, object>>().SingleOrDefault(o => Path.GetFileName(Json.Text(o, "path")) == "preflight.json");
            if (output == null) throw new InvalidDataException("Preview data summary not found.");
            string path = ArtifactIntegrity.ResolveOutputPath(Path.GetDirectoryName(Json.Text(result, "manifest_path")), Json.Text(output, "path")); ArtifactIntegrity.VerifySha256(path, Json.Text(output, "sha256")); var data = Json.Read(path);
            var folds = ((IEnumerable)data["folds"]).Cast<Dictionary<string, object>>().ToList(); var tested = new HashSet<string>(folds.SelectMany(f => ((IEnumerable)f["test_ids"]).Cast<object>().Select(Convert.ToString)));
            int count = Convert.ToInt32(data["sample_count"]), independentCount = data.ContainsKey("independent_test_count") ? Convert.ToInt32(data["independent_test_count"]) : count - tested.Count;
            string summary = "Folds are up to date. Observations: " + count + "; independent groups: " + data["independent_group_count"] + "; independent test: " + independentCount + "; outer folds: " + folds.Count + ".";
            if (data.ContainsKey("initial_training_count")) summary += Environment.NewLine + "Observations tested in outer folds: " + tested.Count + "; only in the initial training period: " + data["initial_training_count"] + ".";
            if (data.ContainsKey("workload")) {
                var workload = (Dictionary<string, object>)data["workload"];
                summary += Environment.NewLine + "Estimated upper bound on model fits: " + Json.Text(workload, "estimated_pipeline_fits_upper_bound") + "; candidate arms: " + Json.Text(workload, "candidate_arms") + "; trial budget: " + Json.Text(workload, "trial_budget") + "; inner folds: " + Json.Text(workload, "inner_folds") + ".";
                summary += Environment.NewLine + "Extra fits for variable selection are not included in this number. Threads per fit: " + Json.Text(workload, "threads_per_fit") + ".";
            }
            if (data.ContainsKey("missing_fraction")) { double missing = ((IEnumerable)data["missing_fraction"]).Cast<object>().Select(Convert.ToDouble).DefaultIfEmpty(0).Max(); summary += Environment.NewLine + "Highest missing value fraction of a covariate: " + missing.ToString("P1") + "."; }
            summary += Environment.NewLine + "Fold records do not increase the number of independent observations. Inspect the fold and buffer boundaries on the map."; return summary;
        }
        private void LoadExample()
        {
            var runtime = RuntimeSettings.Load(); string directory = Path.Combine(localRoot, "examples", Guid.NewGuid().ToString("N")); string source = adapter.LoadExamples(runtime.ExamplesPath, directory); Choose(inputType, "map"); RefreshMap();
            foreach (PointLayerInfo point in points.Items) if (SamePath(point.SourcePath, source)) { points.SelectedItem = point; break; }
            SelectItem(target, "soil_value"); SelectItem(identity, "sample_id"); targetUnit.Text = "g/kg";
            foreach (DataGridViewRow row in rasters.Rows) { bool use = SamePath(Convert.ToString(row.Cells["path"].Value), Path.Combine(runtime.ExamplesPath, "covariates.tif")); row.Cells["use"].Value = use; if (use) { int band = Convert.ToInt32(row.Cells["band"].Value); row.Cells["unit"].Value = band == 1 ? "m" : "1"; row.Cells["name"].Value = band == 1 ? "Synthetic elevation" : "Synthetic environmental covariate"; } }
            state.Text = "Example loaded. Use Show folds to inspect the validation."; Log(state.Text + " " + directory); tabs.SelectedIndex = 1;
        }
        private void PopulateOutputs(Dictionary<string, object> result)
        {
            outputTables.Items.Clear(); outputReports.Items.Clear(); outputArtifacts.Clear(); results.DataSource = null;
            Choice detailedReport = null, reportIndex = null;
            var entries = ((IEnumerable)result["outputs"]).Cast<Dictionary<string, object>>().ToList();
            bool hasIndex = entries.Any(entry => Json.Text(entry, "kind") == "report" && Path.GetFileName(Json.Text(entry, "path")).Equals("study_report_index.html", StringComparison.OrdinalIgnoreCase));
            foreach (Dictionary<string, object> entry in entries)
            {
                string kind = Json.Text(entry, "kind");
                string path = ArtifactIntegrity.ResolveOutputPath(Path.GetDirectoryName(Json.Text(result, "manifest_path")), Json.Text(entry, "path")); ArtifactIntegrity.VerifySha256(path, Json.Text(entry, "sha256"));
                string key = outputArtifacts.Count.ToString(CultureInfo.InvariantCulture); var copy = new Dictionary<string, object>(entry); copy["absolute_path"] = path; outputArtifacts.Add(key, copy);
                bool detailed = kind == "report" && Path.GetFileName(path).Equals("study_report.html", StringComparison.OrdinalIgnoreCase);
                bool index = kind == "report" && Path.GetFileName(path).Equals("study_report_index.html", StringComparison.OrdinalIgnoreCase);
                bool fullReport = detailed || Path.GetFileName(path).Equals("report.html", StringComparison.OrdinalIgnoreCase);
                var choice = new Choice(key, index || detailed ? "Detailed study report" : Json.Text(entry, "label", Path.GetFileName(path))); if (kind == "table" && Path.GetExtension(path).Equals(".csv", StringComparison.OrdinalIgnoreCase)) outputTables.Items.Add(choice); else if (kind == "report" && !(hasIndex && fullReport)) outputReports.Items.Add(choice);
                if (detailed) detailedReport = choice;
                if (index) reportIndex = choice;
            }
            if (outputTables.Items.Count > 0) outputTables.SelectedIndex = 0;
            if (reportIndex != null) outputReports.SelectedItem = reportIndex; else if (detailedReport != null) outputReports.SelectedItem = detailedReport; else if (outputReports.Items.Count > 0) outputReports.SelectedIndex = 0;
            UpdateGate();
        }
        private string VerifiedOutput(ComboBox combo)
        {
            string id = ChoiceId(combo); if (!outputArtifacts.ContainsKey(id)) throw new InvalidOperationException("Select an output of a completed job first.");
            var entry = outputArtifacts[id]; string path = Json.Text(entry, "absolute_path"); ArtifactIntegrity.VerifySha256(path, Json.Text(entry, "sha256")); return path;
        }
        private void ShowTable() { if (outputTables.SelectedItem == null) return; results.DataSource = TabularFiles.Read(VerifiedOutput(outputTables)); }
        private void OpenReport()
        {
            string path = VerifiedOutput(outputReports);
            var hashes = outputArtifacts.Values.ToDictionary(v => Json.Text(v, "absolute_path"), v => Json.Text(v, "sha256"), StringComparer.OrdinalIgnoreCase);
            var viewer = ArtifactViewer.Open(this, path, hashes); ownedViewers.Add(viewer); viewer.FormClosed += (s, e) => ownedViewers.Remove(viewer); Log("Verified local report opened in ArcMap: " + path);
        }
        private void ReloadResult()
        {
            if (String.IsNullOrWhiteSpace(lastManifest) || !File.Exists(lastManifest)) throw new FileNotFoundException("Open a completed job or a saved project first.");
            var result = Json.Read(lastManifest); result["manifest_path"] = lastManifest; int count = adapter.AddResult(result); PopulateOutputs(result); Log("Rasters added from the previous successful result: " + count + "; file digests and map group verified.");
        }
        private void ExportModel()
        {
            var package = ModelExport.ReadPackage(lastManifest);
            if (!currentResult) Log("The model to save belongs to a previous run; the current settings were not applied to this model. Run: " + package.RunId);
            using (var dialog = new SaveFileDialog {
                Title = "Save the model of the completed run: " + package.RunId,
                Filter = "Spatial Prediction Studio model (*.spsmodel.zip)|*.spsmodel.zip",
                DefaultExt = "spsmodel.zip", AddExtension = true, SupportMultiDottedExtensions = true, OverwritePrompt = true, CheckPathExists = true,
                FileName = Path.GetFileName(package.SourcePath)
            })
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                ModelExport.Save(package, dialog.FileName, true);
                state.Text = "Model package saved and file digest verified.";
                Log(state.Text + " Run: " + package.RunId + "; destination: " + dialog.FileName + "; SHA-256: " + package.Sha256);
            }
        }
        private void ExportLog() { using (var dialog = new SaveFileDialog { Filter = "Log (*.log)|*.log", FileName = Path.GetFileName(logPath) }) if (dialog.ShowDialog(this) == DialogResult.OK) { File.Copy(logPath, dialog.FileName, true); Log("Log exported: " + dialog.FileName); } }
    }
}
