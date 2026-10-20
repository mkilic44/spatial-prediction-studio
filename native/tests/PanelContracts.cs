using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Linq;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class PanelContracts
{
    private sealed class LayoutHost : Form
    {
        protected override bool ShowWithoutActivation { get { return true; } }
        internal LayoutHost() { ShowInTaskbar = false; StartPosition = FormStartPosition.Manual; Location = new Point(-12000, -12000); }
    }
    private sealed class EnumerationCancellation : System.Collections.IEnumerable
    {
        private readonly System.Threading.CancellationTokenSource cancellation;
        internal EnumerationCancellation(System.Threading.CancellationTokenSource cancellation) { this.cancellation = cancellation; }
        public System.Collections.IEnumerator GetEnumerator() { cancellation.Cancel(); yield return "fixture"; }
    }
    private sealed class SerializationCancellation
    {
        private readonly System.Threading.CancellationTokenSource cancellation;
        internal int Reads;
        internal SerializationCancellation(System.Threading.CancellationTokenSource cancellation) { this.cancellation = cancellation; }
        public string Value { get { if (++Reads == 2) cancellation.Cancel(); return "fixture"; } }
    }
    private static int count;
    private static readonly BindingFlags Flags = BindingFlags.Instance | BindingFlags.NonPublic;
    private static object Call(object instance, string name, params object[] args) { try { return instance.GetType().GetMethods(Flags | BindingFlags.DeclaredOnly).Single(m => m.Name == name && m.GetParameters().Length == args.Length).Invoke(instance, args); } catch (TargetInvocationException ex) { throw ex.InnerException; } }
    private static T Field<T>(object instance, string name) { return (T)instance.GetType().GetField(name, Flags).GetValue(instance); }
    private static void Check(bool passed, string message) { if (!passed) throw new Exception(message); ++count; Console.WriteLine("PASS " + message); }
    private static IEnumerable<Control> Descendants(Control parent)
    {
        foreach (Control child in parent.Controls) { yield return child; foreach (var descendant in Descendants(child)) yield return descendant; }
    }
    private static void CheckRemote(UserControl panel, string directory)
    {
        var empty = new Dictionary<string, object>();
        var status = (Dictionary<string, object>)Call(panel, "ExecuteRemote", "status", empty);
        Check(!(bool)status["busy"] && status.ContainsKey("preview_ready"), "MCP status uses actual panel state");
        Check(Field<CheckBox>(panel, "mcpEnabled").Checked && Field<object>(panel, "mcpBridge") == null, "managed harness cannot create ArcMap MCP session without adapter");
        bool refused = false;
        try { Call(panel, "ExecuteRemote", "shell.execute", empty); } catch (InvalidDataException) { refused = true; }
        Check(refused, "MCP has no arbitrary code or shell command");
        refused = false; try { Call(panel, "ExecuteRemote", "status", new Dictionary<string, object> { { "extra", true } }); } catch (InvalidDataException) { refused = true; }
        Check(refused, "MCP command rejects unknown parameters");
        Call(panel, "SetBusy", true); refused = false;
        try { Call(panel, "ExecuteRemote", "project.get", empty); } catch (InvalidOperationException) { refused = true; }
        Call(panel, "SetBusy", false); Check(refused, "MCP refuses project changes during active work");
        foreach (string path in new[] { "C:relative.sps.json", @"\\server\share\file.sps.json", @"C:\file.sps.json:stream", "relative.sps.json" })
        {
            refused = false; try { Call(panel, "ExecuteRemote", "project.save", new Dictionary<string, object> { { "path", path } }); } catch (InvalidDataException) { refused = true; }
            Check(refused, "MCP refuses nonlocal or ambiguous path " + path);
        }
        string destination = Path.Combine(directory, "mcp-project.sps.json");
        var parameters = new Dictionary<string, object> { { "path", destination } };
        Call(panel, "ExecuteRemote", "project.save", parameters);
        var serializer = new JavaScriptSerializer { MaxJsonLength = 10000000 };
        var readback = serializer.Deserialize<Dictionary<string, object>>(File.ReadAllText(destination));
        Check(serializer.Serialize(readback) == serializer.Serialize(Call(panel, "ProjectRecord")), "MCP saved project equals shared UI project record");
        string saved = File.ReadAllText(destination); refused = false;
        try { Call(panel, "ExecuteRemote", "project.save", parameters); } catch (IOException) { refused = true; }
        Check(refused && File.ReadAllText(destination) == saved, "MCP existing project is preserved without explicit overwrite");
        parameters["overwrite"] = true; Call(panel, "ExecuteRemote", "project.save", parameters);
        Check(Directory.GetFiles(directory, "mcp-project.sps.json.*.bak").Length == 1, "MCP project replacement keeps verified previous backup");
        var handle = panel.Handle;
        string queuedPath = Path.Combine(directory, "queued-cancel.sps.json");
        using (var cancellation = new System.Threading.CancellationTokenSource())
        {
            var task = (System.Threading.Tasks.Task<object>)Call(panel, "DispatchRemote", "project.save", new Dictionary<string, object> { { "path", queuedPath } }, cancellation.Token);
            cancellation.Cancel(); Application.DoEvents();
            Check(task.IsCanceled && !File.Exists(queuedPath), "MCP cancelled queued mutation cannot execute later on UI thread");
        }
        string dispatchedPath = Path.Combine(directory, "dispatched.sps.json");
        var pending = System.Threading.Tasks.Task.Run(async () => await (System.Threading.Tasks.Task<object>)Call(panel, "DispatchRemote", "project.save", new Dictionary<string, object> { { "path", dispatchedPath } }, System.Threading.CancellationToken.None));
        var timer = System.Diagnostics.Stopwatch.StartNew();
        while (!pending.IsCompleted && timer.ElapsedMilliseconds < 5000) { Application.DoEvents(); System.Threading.Thread.Sleep(5); }
        Check(pending.Status == System.Threading.Tasks.TaskStatus.RanToCompletion && File.Exists(dispatchedPath), "MCP background request is marshalled to owner UI thread");
        panel.GetType().GetField("remoteCommandDepth", Flags).SetValue(panel, 1);
        refused = false; try { Call(panel, "Safe", new Action(() => { throw new InvalidDataException("test"); })); } catch (InvalidDataException) { refused = true; }
        panel.GetType().GetField("remoteCommandDepth", Flags).SetValue(panel, 0);
        Check(refused, "MCP nested UI validation propagates error without blocking modal");
        var previousRecord = (Dictionary<string, object>)Call(panel, "ProjectRecord"); string original = serializer.Serialize(previousRecord);
        var invalid = serializer.Deserialize<Dictionary<string, object>>(original); invalid["window_size"] = 4097;
        string invalidFile = Path.Combine(directory, "invalid-window.sps.json"); File.WriteAllText(invalidFile, serializer.Serialize(invalid));
        refused = false; try { Call(panel, "LoadProjectFile", invalidFile); } catch (InvalidDataException) { refused = true; }
        Check(refused && serializer.Serialize(Call(panel, "ProjectRecord")) == original, "invalid saved window size rejected before mutating project");
        panel.GetType().GetField("projectLoadFailed", Flags).SetValue(panel, true);
        refused = false; try { Call(panel, "ProjectRecord"); } catch (InvalidDataException) { refused = true; }
        Check(refused, "partially failed project cannot be saved or exposed as valid");
        refused = false; try { Call(panel, "PrepareRequest", "run", "blocked-load", Path.Combine(directory, "blocked-load")); } catch (InvalidDataException) { refused = true; }
        Check(refused && !Directory.Exists(Path.Combine(directory, "blocked-load")), "partially failed project cannot create a learning request");
        panel.GetType().GetField("projectLoadFailed", Flags).SetValue(panel, false);
        CheckRemoteDeadlines(panel, directory);
    }
    private static void CheckRemoteDeadlines(UserControl panel, string directory)
    {
        var serializer = new JavaScriptSerializer { MaxJsonLength = 10000000 };
        var row = Field<DataGridView>(panel, "rasters").Rows[0]; object originalMetadata = row.Tag;
        string previousPath = Field<string>(panel, "projectPath");
        string preparationDirectory = Path.Combine(directory, "cancelled-preparation");
        using (var cancellation = new System.Threading.CancellationTokenSource())
        {
            try {
                row.Tag = new Dictionary<string, object> { { "provenance", new EnumerationCancellation(cancellation) } };
                bool refused = false;
                try { Call(panel, "PrepareRequest", "preview", "cancelled-preparation", preparationDirectory, cancellation.Token); }
                catch (OperationCanceledException) { refused = true; }
                Check(refused && cancellation.IsCancellationRequested && !Directory.Exists(preparationDirectory), "deadline during actual metadata capture aborts preparation before creating job files");
            } finally { row.Tag = originalMetadata; }
            Call(panel, "StartJob", "preview", true, cancellation.Token);
            var client = Field<object>(panel, "client");
            Check(!(bool)client.GetType().GetProperty("Running", Flags).GetValue(client, null) && !Field<bool>(panel, "busy"), "expired preparation cannot start a worker or leave panel busy");
            Check((string)Field<Dictionary<string, object>>(panel, "lastJob")["status"] == "cancelled" && Field<Label>(panel, "state").Text.Contains("the worker was not started"), "cancelled preparation is not reported as a started or successful job");
            foreach (string command in new[] { "project.open", "project.save", "job.start" })
            {
                bool refused = false;
                var arguments = command == "job.start" ? new Dictionary<string, object> { { "action", "preview" } } : new Dictionary<string, object> { { "path", Path.Combine(directory, "never-created.sps.json") } };
                try { Call(panel, "ExecuteRemote", command, arguments, cancellation.Token); } catch (OperationCanceledException) { refused = true; }
                Check(refused && !File.Exists(Path.Combine(directory, "never-created.sps.json")), "expired remote token reaches " + command + " before any mutation");
            }
        }
        string destination = Path.Combine(directory, "cancelled-save.sps.json"), originalBytes = "previous project bytes";
        File.WriteAllText(destination, originalBytes);
        using (var cancellation = new System.Threading.CancellationTokenSource())
        {
            var trigger = new SerializationCancellation(cancellation);
            try {
                row.Tag = new Dictionary<string, object> { { "provenance", trigger } };
                bool refused = false;
                try { Call(panel, "ExecuteRemote", "project.save", new Dictionary<string, object> { { "path", destination }, { "overwrite", true } }, cancellation.Token); }
                catch (OperationCanceledException) { refused = true; }
                Check(refused && trigger.Reads == 2 && cancellation.IsCancellationRequested, "remote deadline during staged project serialization propagates as cancellation");
                Check(File.ReadAllText(destination) == originalBytes && Field<string>(panel, "projectPath") == previousPath, "cancelled staged save preserves destination bytes and active project identity");
                Check(Directory.GetFiles(directory, "cancelled-save.sps.json.*.tmp").Length == 0 && Directory.GetFiles(directory, "cancelled-save.sps.json.*.bak").Length == 0, "cancelled staged save removes temporary file without creating replacement backup");
            } finally { row.Tag = originalMetadata; }
        }
        var previousRecord = (Dictionary<string, object>)Call(panel, "ProjectRecord");
        var loadRecord = serializer.Deserialize<Dictionary<string, object>>(serializer.Serialize(previousRecord));
        ((Dictionary<string, object>)loadRecord["samples"])["excluded"] = new object[] { new Dictionary<string, object> { { "id", "S1" }, { "reason", "cancel-load-fixture" } } };
        string source = Path.Combine(directory, "cancelled-load.sps.json"); File.WriteAllText(source, serializer.Serialize(loadRecord));
        var exclusions = Field<TextBox>(panel, "sampleExclusions"); string previousExclusions = exclusions.Text;
        using (var cancellation = new System.Threading.CancellationTokenSource())
        {
            EventHandler cancelAfterMutation = (sender, args) => { if (exclusions.Text.Contains("cancel-load-fixture")) cancellation.Cancel(); };
            exclusions.TextChanged += cancelAfterMutation;
            try {
                bool refused = false;
                try { Call(panel, "ExecuteRemote", "project.open", new Dictionary<string, object> { { "path", source } }, cancellation.Token); }
                catch (OperationCanceledException) { refused = true; }
                Check(refused && cancellation.IsCancellationRequested && exclusions.Text == previousExclusions, "deadline after project mutation invokes recovery with an uncancelled token");
                // This managed harness intentionally has no ArcObjects adapter, so full map recovery cannot finish.
                Check(Field<bool>(panel, "projectLoadFailed"), "unavailable ArcObjects recovery locks the incomplete project instead of reporting success");
                bool locked = false; try { Call(panel, "ProjectRecord"); } catch (InvalidDataException) { locked = true; }
                Check(locked, "cancelled load with incomplete recovery cannot expose a valid project record");
            } finally {
                exclusions.TextChanged -= cancelAfterMutation;
                Call(panel, "RestoreSampleMetadata", previousRecord["samples"]);
                panel.GetType().GetField("projectLoadFailed", Flags).SetValue(panel, false);
                panel.GetType().GetField("projectPath", Flags).SetValue(panel, previousPath);
            }
        }
    }
    private static void CheckMetadataCarry(UserControl panel, string directory)
    {
        var row = Field<DataGridView>(panel, "rasters").Rows[0];
        var original = ((List<Dictionary<string, object>>)Call(panel, "GetSelectedCovariates", false))[0];
        var serializer = new JavaScriptSerializer();
        row.Tag = new Dictionary<string, object> { { "resampling", "bilinear" }, { "sha256", new string('a', 64) }, { "band_description", null },
            { "time_representation", "2026-01" }, { "original_resolution", new object[] { 20, 30.5 } },
            { "provenance", new Dictionary<string, object> { { "source", "synthetic" }, { "verified", true } } }, { "target_derived", false } };
        var saved = ((List<Dictionary<string, object>>)Call(panel, "GetSelectedCovariates", false))[0];
        Check(saved.ContainsKey("resampling") && saved.ContainsKey("target_derived") && saved.ContainsKey("provenance"), "all optional covariate metadata survive row serialization");
        string serialized = serializer.Serialize(saved);
        var restored = serializer.Deserialize<Dictionary<string, object>>(serialized);
        row.Tag = null; Call(panel, "RestoreCovariateRows", new List<Dictionary<string, object>> { restored });
        var bandType = panel.GetType().Assembly.GetType("SpatialPredictionStudio.RasterBandInfo");
        var band = Activator.CreateInstance(bandType, true);
        foreach (var pair in new Dictionary<string, object> { { "Id", "inventory_id" }, { "Name", "inventory name" }, { "Path", original["path"] }, { "Band", 1 }, { "Unit", "inventory unit" } }) bandType.GetField(pair.Key).SetValue(band, pair.Value);
        var inventory = Array.CreateInstance(bandType, 1); inventory.SetValue(band, 0);
        Call(panel, "RefreshRasterRows", inventory);
        var refreshed = ((List<Dictionary<string, object>>)Call(panel, "GetSelectedCovariates", true))[0];
        foreach (string key in new[] { "resampling", "sha256", "band_description", "time_representation", "original_resolution", "provenance", "target_derived" })
            Check(refreshed.ContainsKey(key) && serializer.Serialize(refreshed[key]) == serializer.Serialize(saved[key]), "load refresh save preserves typed covariate metadata " + key);
        Check(refreshed["provenance"] is Dictionary<string, object> && refreshed["original_resolution"] is object[] && refreshed["target_derived"] is bool && refreshed["band_description"] == null, "structured metadata and explicit null retain their JSON types");
        Check(serializer.Serialize(restored) == serialized, "restoring metadata does not mutate source project record");
        var grid = Field<DataGridView>(panel, "rasters"); row = grid.Rows[0]; grid.CurrentCell = row.Cells["name"]; Call(panel, "UpdateMetadataDetails");
        var detail = Field<TextBox>(panel, "covariateMetadata");
        Check(detail.ReadOnly && detail.Text.Contains("2026-01") && detail.Text.Contains("20") && detail.Text.Contains("synthetic") && detail.Text.Contains("Derived from target observations: No" + Environment.NewLine), "panel displays time resolution provenance and target-derived metadata read only");
        row.Cells["use"].Value = false; Call(panel, "RefreshRasterRows", inventory); row = grid.Rows[0];
        Check(!Convert.ToBoolean(row.Cells["use"].Value) && serializer.Serialize(((Dictionary<string, object>)Call(panel, "ReadCovariateRow", row))["provenance"]) == serializer.Serialize(saved["provenance"]), "refresh preserves metadata even while raster selection is disabled");
        row.Cells["use"].Value = true;
        var exclusions = new object[] { new Dictionary<string, object> { { "id", "S1" }, { "reason", "synthetic exclusion reason" } } };
        var sampleRecord = new Dictionary<string, object> { { "excluded", exclusions } }; Call(panel, "RestoreSampleMetadata", sampleRecord);
        Check(Field<TextBox>(panel, "sampleExclusions").ReadOnly && Field<TextBox>(panel, "sampleExclusions").Text.Contains("S1: synthetic exclusion reason"), "panel displays excluded IDs and reasons read only");
        var project = new Dictionary<string, object> { { "samples", Call(panel, "SampleSettings") }, { "covariates", Call(panel, "GetSelectedCovariates", true) } };
        string projectPath = Path.Combine(directory, "metadata_project_roundtrip.json"); File.WriteAllText(projectPath, serializer.Serialize(project));
        var projectBack = serializer.Deserialize<Dictionary<string, object>>(File.ReadAllText(projectPath));
        Call(panel, "RestoreSampleMetadata", projectBack["samples"]);
        Call(panel, "RestoreCovariateRows", ((System.Collections.IEnumerable)projectBack["covariates"]).Cast<Dictionary<string, object>>().ToList());
        Call(panel, "RefreshRasterRows", inventory);
        var request = (Dictionary<string, object>)Call(panel, "PrepareRequest", "preview", "metadata", Path.Combine(directory, "metadata-request"));
        Check(serializer.Serialize(((Dictionary<string, object>)request["samples"])["excluded"]) == serializer.Serialize(exclusions), "load refresh save request preserves sample exclusions exactly");
        var requestCovariate = ((List<Dictionary<string, object>>)request["covariates"])[0];
        Check(serializer.Serialize(requestCovariate) == serialized, "all seven covariate metadata fields reach preview request unchanged");
        File.WriteAllText(Path.Combine(directory, "metadata_preview_request.json"), serializer.Serialize(request));
        ((Dictionary<string, object>)exclusions[0])["reason"] = "changed external object";
        Check(serializer.Serialize(((Dictionary<string, object>)Call(panel, "SampleSettings"))["excluded"]).Contains("synthetic exclusion reason"), "exclusion state is isolated from caller mutation");
        row = grid.Rows[0]; string before = (string)Call(panel, "CurrentSignature"); ((Dictionary<string, object>)row.Tag)["target_derived"] = true;
        Check(before != (string)Call(panel, "CurrentSignature"), "target-derived metadata participates in preview signature");
        Check(Convert.ToBoolean(((List<Dictionary<string, object>>)Call(panel, "GetSelectedCovariates", true))[0]["target_derived"]), "blocked target-derived setting remains saveable without silent removal");
        foreach (string action in new[] { "preview", "run" })
        {
            string blocked = Path.Combine(directory, "metadata-blocked-" + action); bool refused = false;
            try { Call(panel, "PrepareRequest", action, "blocked", blocked); } catch (InvalidDataException ex) { refused = ex.Message.Contains("TARGET_LEAKAGE"); }
            Check(refused && !Directory.Exists(blocked), "TARGET_LEAKAGE rejects " + action + " before request or output directory creation");
        }
        ((Dictionary<string, object>)row.Tag)["target_derived"] = false;
        row.Cells["kind"].Value = "categorical"; bool categoricalRefused = false;
        try { Call(panel, "GetSelectedCovariates", true); } catch (InvalidDataException) { categoricalRefused = true; }
        Check(categoricalRefused, "categorical bilinear resampling is rejected instead of silently reset"); row.Cells["kind"].Value = "continuous";
        var unknown = new Dictionary<string, object>(saved) { { "unsupported_metadata", "nonempty" } }; bool unknownRefused = false;
        try { Call(panel, "RestoreCovariateRows", new List<Dictionary<string, object>> { unknown }); } catch (InvalidDataException) { unknownRefused = true; }
        Check(unknownRefused, "unknown covariate settings are rejected instead of silently discarded");
        foreach (object invalid in new object[] { null, "S1", new object[] { new Dictionary<string, object> { { "id", "S1" } } }, new object[] { new Dictionary<string, object> { { "id", 1 }, { "reason", "reason" } } }, new object[] { new Dictionary<string, object> { { "id", "S1" }, { "reason", "reason" }, { "unknown", true } } } })
        {
            bool refused = false; try { Call(panel, "RestoreSampleMetadata", new Dictionary<string, object> { { "excluded", invalid } }); } catch (InvalidDataException) { refused = true; }
            Check(refused, "malformed exclusions are rejected rather than discarded");
        }
        bool unknownSampleRefused = false; try { Call(panel, "RestoreSampleMetadata", new Dictionary<string, object> { { "unknown_sample_setting", "nonempty" } }); } catch (InvalidDataException) { unknownSampleRefused = true; }
        Check(unknownSampleRefused, "unknown sample settings are rejected instead of silently discarded");
        Call(panel, "RestoreSampleMetadata", new Dictionary<string, object> { { "excluded", new object[0] } });
        Check(((Dictionary<string, object>)Call(panel, "SampleSettings")).ContainsKey("excluded"), "explicit empty exclusion list remains explicit");
        Call(panel, "RestoreSampleMetadata", new Dictionary<string, object>()); Call(panel, "RestoreCovariateRows", new List<Dictionary<string, object>> { original });
        Check(!((Dictionary<string, object>)Call(panel, "SampleSettings")).ContainsKey("excluded"), "legacy project without exclusions gains no default exclusion key");
        Check(serializer.Serialize(((List<Dictionary<string, object>>)Call(panel, "GetSelectedCovariates", true))[0]) == serializer.Serialize(original), "legacy project without optional raster metadata roundtrips without added defaults");
        Check(detail.Text.Contains("Time representation: Not recorded") && detail.Text.Contains("Derived from target observations: Not recorded"), "missing raster metadata is explicitly displayed as unavailable");
        File.WriteAllText(Path.Combine(directory, "metadata_contract_result.json"), serializer.Serialize(new { status = "passed", preview_target_leakage_rejected_before_output = true, run_target_leakage_rejected_before_output = true, typed_optional_metadata_roundtrip = true, exclusions_roundtrip = true, scope = "managed production panel helpers; actual ArcMap project open and refresh remain separate UI acceptance" }));
    }
    private static void CheckReportExport(UserControl panel, string directory)
    {
        var type = panel.GetType();
        string reportRoot = Path.Combine(directory, "report-export"); Directory.CreateDirectory(reportRoot);
        string manifest = Path.Combine(reportRoot, "result.json");
        var artifacts = new List<Dictionary<string, object>>();
        foreach (string name in new[] { "summary.html", "model_package.spsmodel.zip", "study_report_index.html", "study_report.html", "report.html" })
        {
            string path = Path.Combine(reportRoot, name); File.WriteAllText(path, "verified fixture " + name);
            string sha; using (var hash = System.Security.Cryptography.SHA256.Create())
                sha = BitConverter.ToString(hash.ComputeHash(File.ReadAllBytes(path))).Replace("-", "").ToLowerInvariant();
            artifacts.Add(new Dictionary<string, object> { { "path", name }, { "kind", name.EndsWith(".zip") ? "model_bundle" : "report" }, { "sha256", sha }, { "label", "backend label" } });
        }
        var result = new Dictionary<string, object> { { "schema_version", 2 }, { "status", "completed" }, { "run_id", "report-export" }, { "outputs", artifacts }, { "manifest_path", manifest } };
        File.WriteAllText(manifest, new JavaScriptSerializer().Serialize(result));
        type.GetField("lastManifest", Flags).SetValue(panel, manifest);
        type.GetField("currentResult", Flags).SetValue(panel, true);
        Call(panel, "PopulateOutputs", result); Call(panel, "UpdateGate");
        Check(Field<ComboBox>(panel, "outputReports").SelectedItem.ToString() == "Detailed study report", "detailed study report is preferred even when another report appears first");
        Check(Path.GetFileName((string)Call(panel, "VerifiedOutput", Field<ComboBox>(panel, "outputReports"))) == "study_report_index.html", "light report index wins over full report regardless of output order");
        Check(Field<ComboBox>(panel, "outputReports").Items.Count == 2, "index jobs hide both full HTML reports from the report dropdown");
        Check(Field<Dictionary<string, Dictionary<string, object>>>(panel, "outputArtifacts").Count == 5, "hidden full HTML reports remain in verified output inventory");
        Check(Field<Button>(panel, "saveModel").Text == "Save model" && Field<Button>(panel, "saveModel").Enabled, "completed report page exposes model export action");
        Call(panel, "SetBusy", true); Check(!Field<Button>(panel, "saveModel").Enabled, "model export is disabled while a worker runs"); Call(panel, "SetBusy", false);
        Call(panel, "InvalidatePreview", "test scientific settings changed");
        Check(Field<Label>(panel, "modelExportState").Text.Contains("current settings were not applied") && Field<Button>(panel, "saveModel").Enabled, "historical model remains exportable with explicit changed-settings notice");
        Check(Field<string>(panel, "lastManifest") == manifest, "model export remains tied to successful run when settings change");
        artifacts.RemoveAt(2); Call(panel, "PopulateOutputs", result);
        Check(Path.GetFileName((string)Call(panel, "VerifiedOutput", Field<ComboBox>(panel, "outputReports"))) == "study_report.html", "full detailed report remains fallback for legacy jobs without index");
        artifacts.RemoveAt(2); Call(panel, "PopulateOutputs", result);
        Check(Field<ComboBox>(panel, "outputReports").SelectedIndex == 0, "legacy report list retains its available report fallback");
    }
    private static void CheckEditorLayout(UserControl panel, Form form, TabControl tabs, string directory)
    {
        var evidence = new List<object>();
        form.Show();
        foreach (int width in new[] { 680, 560, 680 })
        {
            form.ClientSize = new Size(width, 1000);
            foreach (var scenario in new[] { new[] { "model", "lightgbm", "5" }, new[] { "validation", "knndm", "2" },
                new[] { "model", "tabm", "5" }, new[] { "model", "realmlp", "5" }, new[] { "model", "tabpfn", "5" },
                new[] { "validation", "temporal", "2" }, new[] { "uncertainty", "spatial_conformal", "7" } })
            {
                tabs.SelectedIndex = Int32.Parse(scenario[2]);
                var page = tabs.SelectedTab; page.AutoScrollPosition = Point.Empty;
                var editor = Field<Control>(panel, scenario[0]); Call(editor, "Select", scenario[1]);
                form.PerformLayout(); panel.PerformLayout(); page.PerformLayout(); Application.DoEvents();
                var selector = Field<ComboBox>(editor, "methods");
                var inputs = Descendants(editor).Where(c => c is ComboBox || c is NumericUpDown || c is TextBox || c is CheckBox || c is Button).ToArray();
                Check(inputs.Length > 1, scenario[1] + " has live selector and parameter controls at " + width + "px");
                foreach (var input in inputs)
                {
                    var bounds = page.RectangleToClient(input.RectangleToScreen(input.ClientRectangle));
                    evidence.Add(new { panel_width = panel.Width, method = scenario[1], control = input.GetType().Name, left = bounds.Left, right = bounds.Right, viewport_right = page.ClientSize.Width, width = input.Width });
                    Check(bounds.Left >= 0 && bounds.Right <= page.ClientSize.Width && input.Width >= 18,
                        scenario[1] + " " + input.GetType().Name + " right edge accessible at " + width + "px (" + bounds.Right + " <= " + page.ClientSize.Width + ")");
                }
                var description = Field<Label>(editor, "description");
                var labelBounds = page.RectangleToClient(description.RectangleToScreen(description.ClientRectangle));
                Check(labelBounds.Right <= page.ClientSize.Width && description.Height > description.Font.Height + description.Padding.Vertical,
                    scenario[1] + " full description wraps within viewport at " + width + "px");
                page.ScrollControlIntoView(selector); Application.DoEvents();
                var arrow = selector.PointToScreen(new Point(selector.ClientSize.Width - 4, selector.ClientSize.Height / 2));
                Check(page.ClientRectangle.Contains(page.PointToClient(arrow)), scenario[1] + " dropdown arrow lies in visible page at " + width + "px");
            }
            tabs.SelectedIndex = 1; form.PerformLayout(); panel.PerformLayout(); tabs.SelectedTab.PerformLayout(); Application.DoEvents();
            foreach (var control in new Control[] { Field<TextBox>(panel, "covariateMetadata"), Field<TextBox>(panel, "sampleExclusions") })
            {
                var bounds = tabs.SelectedTab.RectangleToClient(control.RectangleToScreen(control.ClientRectangle));
                Check(bounds.Left >= 0 && bounds.Right <= tabs.SelectedTab.ClientSize.Width && control.Width >= 100,
                    "metadata detail control remains inside data viewport at " + width + "px");
            }
            tabs.SelectedTab = Field<TabPage>(panel, "reportPage");
            form.PerformLayout(); panel.PerformLayout(); tabs.SelectedTab.PerformLayout(); Application.DoEvents();
            foreach (var control in new Control[] { Field<Button>(panel, "saveModel"), Field<Label>(panel, "modelExportState") })
            {
                var bounds = tabs.SelectedTab.RectangleToClient(control.RectangleToScreen(control.ClientRectangle));
                Check(bounds.Left >= 0 && bounds.Right <= tabs.SelectedTab.ClientSize.Width,
                    "model export control remains inside report viewport at " + width + "px");
            }
        }
        File.WriteAllText(Path.Combine(directory, "layout_bounds.json"), new JavaScriptSerializer().Serialize(evidence));
        form.Hide();
    }
    [STAThread]
    private static int Main(string[] args)
    {
        try {
            string directory = Path.GetFullPath(args[2]); Directory.CreateDirectory(directory); var assembly = Assembly.LoadFrom(Path.GetFullPath(args[0]));
            var registryType = assembly.GetType("SpatialPredictionStudio.MethodRegistry"); var registry = registryType.GetMethod("Load", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, new object[] { Path.GetFullPath(args[1]) });
            var type = assembly.GetType("SpatialPredictionStudio.StudioControl");
            using (var panel = (UserControl)Activator.CreateInstance(type, Flags, null, new[] { registry, directory }, null))
            using (var form = new LayoutHost { Width = 940, Height = 1000 })
            {
                form.Controls.Add(panel); panel.CreateControl(); var tabs = Field<TabControl>(panel, "tabs"); Check(tabs.TabCount == 10, "panel exposes ten workflow steps");
                Check(!Field<Button>(panel, "run").Enabled, "run is initially disabled without preview");
                string csv = Path.Combine(directory, "samples.csv"); File.WriteAllText(csv, "sample_id,soil_value,x,y,profile,top,bottom,method,date\nS1,10,36,37,P1,0,20,m1,2026-01-01\nS2,20,36.1,37.1,P2,0,20,m1,2026-01-01\n");
                Field<TextBox>(panel, "csvPath").Text = csv; Call(panel, "LoadCsv");
                Check(Field<ComboBox>(panel, "target").Items.Contains("soil_value") && Field<ComboBox>(panel, "identity").Items.Contains("sample_id"), "CSV field selectors use actual headers");
                Field<ComboBox>(panel, "csvX").SelectedItem = "x"; Field<ComboBox>(panel, "csvY").SelectedItem = "y";
                string raster = Path.Combine(directory, "fixture.tif"); File.WriteAllText(raster, "only native request construction; not a real raster");
                Field<DataGridView>(panel, "rasters").Rows.Add(true, "Synthetic covariate", 1, "1", "r1", "continuous", "nearest", raster);
                var request = (Dictionary<string, object>)Call(panel, "PrepareRequest", "preview", "managed_fixture", Path.Combine(directory, "request"));
                var samples = (Dictionary<string, object>)request["samples"];
                Check((int)request["schema_version"] == 2 && (string)request["action"] == "preview", "panel creates schema 2 preview request");
                Check((string)samples["path"] == csv && (string)samples["x_field"] == "x" && (string)samples["y_field"] == "y", "CSV coordinates and original source retained");
                Check(!request.ContainsKey("preview_fingerprint"), "preview request has no fabricated fingerprint");
                CheckMetadataCarry(panel, directory);
                string held = Path.Combine(directory, "independent.csv"); File.WriteAllText(held, "sample_id,soil_value,east,north\nH1,15,500000,4200000\n"); Field<TextBox>(panel, "independentPath").Text = held; Call(panel, "LoadIndependentFields");
                Field<ComboBox>(panel, "independentX").SelectedItem = "east"; Field<ComboBox>(panel, "independentY").SelectedItem = "north";
                bool missingCrsRefused = false; try { Call(panel, "SampleSettings"); } catch (InvalidDataException) { missingCrsRefused = true; } Check(missingCrsRefused, "independent CSV requires its own explicit coordinate system");
                Field<TextBox>(panel, "independentCrs").Text = "EPSG:32637"; var heldSettings = (Dictionary<string, object>)Call(panel, "SampleSettings");
                Check((string)heldSettings["independent_x_field"] == "east" && (string)heldSettings["independent_y_field"] == "north" && (string)heldSettings["independent_crs"] == "EPSG:32637", "independent coordinates and CRS are separate from main sample settings");
                object featureEditor = Field<object>(panel, "featureSelection"); Call(featureEditor, "Select", "pearson"); object parameters = Field<object>(featureEditor, "Parameters"); Call(parameters, "Set", "compare_without_selection", true, true);
                var selectionSettings = (Dictionary<string, object>)Call(panel, "ScienceSettings"); Check(Convert.ToBoolean(((Dictionary<string, object>)selectionSettings["feature_selection"])["compare_without_selection"]), "registry comparison checkbox reaches scientific request");
                var defaultDiagnostics = (Dictionary<string, object>)selectionSettings["diagnostics"];
                Check(!Convert.ToBoolean(defaultDiagnostics["applicability"]) && !Convert.ToBoolean(defaultDiagnostics["permutation"]) && ((string[])defaultDiagnostics["subgroup_fields"]).Length == 0 && (int)defaultDiagnostics["permutation_repeats"] == 5 && (int)defaultDiagnostics["min_subgroup_samples"] == 10 && (int)defaultDiagnostics["min_subgroup_groups"] == 3, "stage C diagnostics start disabled with specified defaults");
                object subgroups = Field<object>(panel, "subgroupFields"); var options = (Delegate)subgroups.GetType().GetField("Options", Flags).GetValue(subgroups);
                Check(((System.Collections.IEnumerable)options.DynamicInvoke()).Cast<object>().Any(v => v.ToString() == "profile"), "subgroup options come from actual loaded sample fields");
                type.GetField("previewFingerprint", Flags).SetValue(panel, "previous_preview"); Call(panel, "UpdateGate"); Field<CheckBox>(panel, "applicability").Checked = true;
                Check(!Field<Button>(panel, "run").Enabled && Field<string>(panel, "previewFingerprint") == null, "applicability change invalidates preview");
                Field<CheckBox>(panel, "permutation").Checked = true; Field<NumericUpDown>(panel, "permutationRepeats").Value = 7;
                Field<NumericUpDown>(panel, "minSubgroupSamples").Value = 12; Field<NumericUpDown>(panel, "minSubgroupGroups").Value = 4; subgroups.GetType().GetProperty("Values", Flags).SetValue(subgroups, new[] { "profile", "method" });
                var stageC = (Dictionary<string, object>)Call(panel, "ScienceSettings"); var diagnostics = (Dictionary<string, object>)stageC["diagnostics"];
                Check(Convert.ToBoolean(diagnostics["applicability"]) && Convert.ToBoolean(diagnostics["permutation"]) && (int)diagnostics["permutation_repeats"] == 7 && ((string[])diagnostics["subgroup_fields"]).SequenceEqual(new[] { "profile", "method" }) && (int)diagnostics["min_subgroup_samples"] == 12 && (int)diagnostics["min_subgroup_groups"] == 4, "all stage C diagnostics serialize with backend names");
                var oldB = new Dictionary<string, object>(stageC); oldB.Remove("diagnostics"); Call(panel, "RestoreScience", oldB);
                Check(!Field<CheckBox>(panel, "applicability").Checked && !Field<CheckBox>(panel, "permutation").Checked && Field<NumericUpDown>(panel, "permutationRepeats").Value == 5 && ((string[])subgroups.GetType().GetProperty("Values", Flags).GetValue(subgroups)).Length == 0, "older B projects restore disabled C defaults");
                Call(panel, "RestoreScience", stageC); var restoredC = (Dictionary<string, object>)((Dictionary<string, object>)Call(panel, "ScienceSettings"))["diagnostics"];
                Check(new JavaScriptSerializer().Serialize(diagnostics) == new JavaScriptSerializer().Serialize(restoredC), "stage C diagnostic project settings roundtrip");
                Call(panel, "SetBusy", true); Check(new[] { "applicability", "permutation", "permutationRepeats", "subgroupFields", "minSubgroupSamples", "minSubgroupGroups" }.All(name => !Field<Control>(panel, name).Enabled), "all stage C controls disabled while worker runs"); Call(panel, "SetBusy", false);
                var labels = (Dictionary<string, string>)assembly.GetType("SpatialPredictionStudio.ArcMapAdapter").GetField("RasterLabels", BindingFlags.Static | BindingFlags.NonPublic).GetValue(null);
                Check(labels.ContainsKey("applicability") && labels.ContainsKey("dissimilarity") && labels["applicability"].Contains("1: inside"), "stage C output raster labels define 0 and 1 semantics");
                Check(labels.ContainsKey("interval_available") && labels.ContainsKey("interval_effective_n") && labels.ContainsKey("interval_effective_group_n") && labels["interval_available"].Contains("1: available"), "stage D local support layers have distinct labels");
                Check(Field<NumericUpDown>(panel, "permutationRepeats").Minimum == 2 && Field<NumericUpDown>(panel, "permutationRepeats").Maximum == 100 && Field<NumericUpDown>(panel, "minSubgroupSamples").Minimum == 3 && Field<NumericUpDown>(panel, "minSubgroupGroups").Minimum == 2, "stage C numeric bounds match request contract");
                var serializer = new JavaScriptSerializer { MaxJsonLength = 10000000 }; File.WriteAllText(Path.Combine(directory, "managed_preview_request.json"), serializer.Serialize(request));
                var science = (Dictionary<string, object>)Call(panel, "ScienceSettings"); string signature = (string)Call(panel, "CurrentSignature"); Call(panel, "RestoreScience", science); Check(signature == (string)Call(panel, "CurrentSignature"), "all scientific settings roundtrip without signature drift");
                type.GetField("previewFingerprint", Flags).SetValue(panel, "backend_identity_fixture"); Call(panel, "UpdateGate"); Check(Field<Button>(panel, "run").Enabled, "verified preview identity opens run gate");
                type.GetField("lastManifest", Flags).SetValue(panel, "previous_success.json"); Field<TextBox>(panel, "targetUnit").Text = "mg/kg";
                Check(!Field<Button>(panel, "run").Enabled && Field<string>(panel, "previewFingerprint") == null, "scientific edit invalidates preview immediately");
                Check(Field<string>(panel, "lastManifest") == "previous_success.json", "scientific edit preserves historical successful result");
                CheckReportExport(panel, directory);
                CheckRemote(panel, directory);
                CheckEditorLayout(panel, form, tabs, directory);
            }
            File.WriteAllText(Path.Combine(directory, "panel_contract_result.json"), new JavaScriptSerializer().Serialize(new { status = "passed", assertions = count, scope = "managed panel and request contracts; ArcMap COM integration not exercised" }));
            Console.WriteLine("PASS assertions=" + count); return 0;
        } catch (Exception ex) { Console.Error.WriteLine(ex); return 1; }
    }
}
