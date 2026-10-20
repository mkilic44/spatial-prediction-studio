using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace SpatialPredictionStudio
{
    public sealed partial class StudioControl
    {
        private McpPipeBridge mcpBridge;
        private readonly CheckBox mcpEnabled = Check("Model Context Protocol (MCP) yerel bağlantısını aç");
        private readonly Label mcpState = Note("MCP bağlantısı kapalı.");
        private Dictionary<string, object> lastJob;
        private int remoteCommandDepth;

        private void UpdateMcpBridge()
        {
            if (adapter == null || disposed || !IsHandleCreated) return;
            if (!mcpEnabled.Checked) { mcpBridge?.Dispose(); mcpBridge = null; mcpState.Text = "MCP bağlantısı kapalı."; return; }
            if (mcpBridge != null) return;
            try { mcpBridge = new McpPipeBridge(localRoot, DispatchRemote, Log); mcpState.Text = "MCP hazır. Bağlantı yalnız bu Windows kullanıcısına açıktır."; }
            catch (Exception ex) { mcpState.Text = "MCP başlatılamadı: " + ex.Message; Log(mcpState.Text); }
        }
        private Task<object> DispatchRemote(string method, Dictionary<string, object> parameters, CancellationToken token)
        {
            var completion = new TaskCompletionSource<object>(TaskCreationOptions.RunContinuationsAsynchronously);
            var registration = token.Register(() => completion.TrySetCanceled());
            completion.Task.ContinueWith(t => registration.Dispose(), TaskScheduler.Default);
            try { BeginInvoke(new Action(async () => {
                if (token.IsCancellationRequested || disposed || !mcpEnabled.Checked) { completion.TrySetCanceled(); return; }
                try {
                    if (new[] { "result.list", "result.export_model", "result.export_report" }.Contains(method))
                    {
                        string manifest = lastManifest;
                        var result = await Task.Run(() => ExecuteFileRemote(method, parameters, manifest, token), token);
                        completion.TrySetResult(result);
                    }
                    else { remoteCommandDepth++; try { completion.TrySetResult(ExecuteRemote(method, parameters, token)); } finally { remoteCommandDepth--; } }
                }
                catch (Exception ex) { completion.TrySetException(ex); }
            })); }
            catch (Exception ex) { completion.TrySetException(ex); }
            return completion.Task;
        }
        private void RequireIdle() { if (busy || client.Running) throw new InvalidOperationException("BUSY: Çalışan iş tamamlanmadan proje değiştirilemez."); }
        private static string LocalPath(Dictionary<string, object> parameters)
        {
            string path = Json.Text(parameters, "path");
            if (!System.Text.RegularExpressions.Regex.IsMatch(path, @"^[A-Za-z]:[\\/]") || path.Substring(2).Contains(":") || path.Length > 32000) throw new InvalidDataException("LOCAL_PATH_REQUIRED: Yerel mutlak dosya yolu gereklidir.");
            return Path.GetFullPath(path);
        }
        private static bool Overwrite(Dictionary<string, object> p)
        {
            object value; if (!p.TryGetValue("overwrite", out value)) return false;
            if (!(value is bool)) throw new InvalidDataException("overwrite mantıksal değer olmalıdır."); return (bool)value;
        }
        private Dictionary<string, object> RemoteStatus()
        {
            return new Dictionary<string, object> { { "busy", busy || client.Running }, { "state", state.Text },
                { "preview_ready", run.Enabled }, { "last_job", lastJob == null ? null : new Dictionary<string, object>(lastJob) }, { "project_path", projectPath },
                { "last_manifest", lastManifest }, { "session_id", mcpBridge?.SessionId },
                { "version", GetType().Assembly.GetName().Version.ToString() }, { "progress_percent", progress.Value } };
        }
        private object ExecuteRemote(string method, Dictionary<string, object> p)
        { return ExecuteRemote(method, p, CancellationToken.None); }
        private object ExecuteRemote(string method, Dictionary<string, object> p, CancellationToken cancellation)
        {
            // This method is entered only on the ArcMap owner thread, or by the managed contract harness.
            cancellation.ThrowIfCancellationRequested();
            string[] keys = method == "project.open" ? new[] { "path" } :
                new[] { "project.save", "result.export_model", "result.export_report" }.Contains(method) ? new[] { "path", "overwrite" } :
                method == "job.start" ? new[] { "action" } : new string[0];
            CheckKeys(p, keys, "MCP");
            switch (method)
            {
                case "status": return RemoteStatus();
                case "layers":
                    List<PointLayerInfo> pointLayers; List<RasterBandInfo> rasterBands; adapter.Inventory(out pointLayers, out rasterBands, Log);
                    return new { points = pointLayers.Select(x => new { name = x.Name, path = x.SourcePath, fields = adapter.Fields(x, false) }).ToArray(),
                        raster_bands = rasterBands.Select(x => new { id = x.Id, name = x.Name, path = x.Path, band = x.Band, unit = x.Unit }).ToArray(),
                        scope = "Spatial Prediction Studio tarafından desteklenen nokta katmanları ve dosya tabanlı raster bantları" };
                case "project.get": RequireIdle(); return ProjectRecord();
                case "project.open":
                    RequireIdle(); string source = LocalPath(p);
                    if (!source.EndsWith(".json", StringComparison.OrdinalIgnoreCase) || !File.Exists(source) || new FileInfo(source).Length > 16 * 1024 * 1024) throw new InvalidDataException("PROJECT_FILE: En çok 16 MiB yerel proje dosyası gereklidir.");
                    LoadProjectFile(source, cancellation); return RemoteStatus();
                case "project.save":
                    RequireIdle(); string destination = LocalPath(p);
                    if (!destination.EndsWith(".sps.json", StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("PROJECT_EXTENSION: .sps.json uzantısı gereklidir.");
                    SaveProjectFile(destination, Overwrite(p), cancellation); return new { path = destination, sha256 = ConfigurationIdentity.FileDigest(destination) };
                case "job.start":
                    RequireIdle(); string action = Json.Text(p, "action");
                    if (action != "preview" && action != "run") throw new InvalidDataException("JOB_ACTION: preview veya run seçilmelidir.");
                    StartJob(action, true, cancellation);
                    cancellation.ThrowIfCancellationRequested();
                    if (lastJob != null && Json.Text(lastJob, "status") == "cancelled") throw new OperationCanceledException(Json.Text(lastJob, "error"), cancellation);
                    if (lastJob == null || Json.Text(lastJob, "status") == "failed") throw new InvalidOperationException(lastJob == null ? "JOB_NOT_STARTED" : Json.Text(lastJob, "error"));
                    return new Dictionary<string, object>(lastJob);
                case "job.cancel":
                    if (!busy && !client.Running) return new { status = "idle", last_job = lastJob };
                    Cancel(); return new { status = "cancellation_requested", last_job = lastJob };
                case "result.list":
                case "result.export_model":
                case "result.export_report": return ExecuteFileRemote(method, p, lastManifest, cancellation);
                default: throw new InvalidDataException("UNKNOWN_COMMAND: Desteklenmeyen MCP işlemi.");
            }
        }
        private Dictionary<string, object> ProjectRecord()
        {
            if (projectLoadFailed) throw new InvalidDataException("PROJECT_INCOMPLETE: Önce geçerli bir proje açın.");
            var point = points.SelectedItem as PointLayerInfo;
            if (!IsCsv && point == null) throw new InvalidDataException("Kaydedilecek nokta katmanı seçilmedi.");
            var samples = SampleSettings(); var science = ScienceSettings(); var scope = IsCsv ? null : adapter.CapturePointScope(point, identity.Text, selected.Checked);
            return new Dictionary<string, object> {
                { "schema_version", 2 }, { "project_type", "SpatialPredictionStudio" }, { "input_type", ChoiceId(inputType) },
                { "point_source", IsCsv ? "" : point.SourcePath }, { "point_name", IsCsv ? "" : point.Name }, { "point_scope", scope?.ToRecord() },
                { "csv_path", csvPath.Text }, { "samples", samples }, { "selected_only", !IsCsv && selected.Checked }, { "science", science },
                { "covariates", GetSelectedCovariates() }, { "output_root", outputRoot.Text }, { "window_size", (int)windowSize.Value }, { "last_manifest", lastManifest ?? "" },
                { "preview_fingerprint", previewFingerprint ?? "" }, { "preview_manifest", previewManifest ?? "" }, { "preview_signature", previewSignature ?? "" }, { "preview_sources", previewSources ?? "" },
                { "registry_digest", registry.Digest }, { "custom_fold_draft", customFolds.Values() }
            };
        }
        private void SaveProjectFile(string destination, bool overwrite)
        { SaveProjectFile(destination, overwrite, CancellationToken.None); }
        private void SaveProjectFile(string destination, bool overwrite, CancellationToken cancellation)
        {
            cancellation.ThrowIfCancellationRequested();
            var record = ProjectRecord(); string expected = ConfigurationIdentity.Digest(record);
            cancellation.ThrowIfCancellationRequested();
            if (!Directory.Exists(Path.GetDirectoryName(destination))) throw new DirectoryNotFoundException("Proje klasörü bulunamadı.");
            if (File.Exists(destination) && !overwrite) throw new IOException("FILE_EXISTS: Üzerine yazma seçilmedi.");
            var protectedPaths = GetSelectedCovariates().Select(c => Json.Text(c, "path")).Concat(new[] { csvPath.Text, independentPath.Text, lastManifest, previewManifest });
            if (protectedPaths.Any(x => !String.IsNullOrWhiteSpace(x) && SamePath(Path.GetFullPath(x), destination))) throw new IOException("PROTECTED_SOURCE: Girdi veya sonuç üzerine yazılamaz.");
            string temporary = destination + "." + Guid.NewGuid().ToString("N") + ".tmp", backup = destination + "." + Guid.NewGuid().ToString("N") + ".bak";
            bool installed = false;
            try {
                cancellation.ThrowIfCancellationRequested();
                Json.Write(temporary, record); cancellation.ThrowIfCancellationRequested();
                if (ConfigurationIdentity.Digest(Json.Read(temporary)) != expected) throw new IOException("PROJECT_READBACK_FAILED");
                cancellation.ThrowIfCancellationRequested();
                if (File.Exists(destination)) { if (!overwrite) throw new IOException("FILE_EXISTS"); File.Replace(temporary, destination, backup); } else File.Move(temporary, destination);
                installed = true; cancellation.ThrowIfCancellationRequested();
                if (ConfigurationIdentity.Digest(Json.Read(destination)) != expected) throw new IOException("PROJECT_READBACK_FAILED");
                cancellation.ThrowIfCancellationRequested();
                projectPath = destination; Log("Proje kaydedildi ve geri okundu: " + destination);
            } catch { if (installed) { if (File.Exists(backup)) File.Replace(backup, destination, null); else File.Delete(destination); } throw; }
            finally { if (File.Exists(temporary)) File.Delete(temporary); }
            // An overwritten project backup is intentionally retained for the user.
        }
        private static object ExecuteFileRemote(string method, Dictionary<string, object> p, string manifest, CancellationToken cancellation)
        {
            CheckKeys(p, method == "result.list" ? new string[0] : new[] { "path", "overwrite" }, "MCP");
            cancellation.ThrowIfCancellationRequested();
            if (method == "result.list") return RemoteArtifacts(manifest, cancellation);
            return RemoteExport(p, method == "result.export_report", manifest, cancellation);
        }
        private static object RemoteArtifacts(string lastManifest, CancellationToken cancellation)
        {
            if (String.IsNullOrWhiteSpace(lastManifest)) throw new InvalidOperationException("NO_COMPLETED_RESULT");
            var result = Json.Read(lastManifest); if (Json.Text(result, "status") != "completed") throw new InvalidDataException("RESULT_NOT_COMPLETED");
            string directory = Path.GetDirectoryName(Path.GetFullPath(lastManifest));
            var artifacts = ((IEnumerable)result["outputs"]).Cast<Dictionary<string, object>>().Select(e => {
                cancellation.ThrowIfCancellationRequested();
                string path = ArtifactIntegrity.ResolveOutputPath(directory, Json.Text(e, "path")); ArtifactIntegrity.VerifySha256(path, Json.Text(e, "sha256"), cancellation);
                return new { name = path.Substring(directory.Length + 1).Replace('\\', '/'), path, sha256 = Json.Text(e, "sha256"), kind = Json.Text(e, "kind"), label = Json.Text(e, "label"), bytes = new FileInfo(path).Length };
            }).ToArray();
            return new { run_id = Json.Text(result, "run_id"), manifest_path = lastManifest, artifacts };
        }
        private static object RemoteExport(Dictionary<string, object> p, bool study, string lastManifest, CancellationToken cancellation)
        {
            string path = LocalPath(p); var package = study ? ModelExport.ReadStudyPackage(lastManifest, cancellation) : ModelExport.ReadPackage(lastManifest, cancellation);
            if (!path.EndsWith(study ? ".spsstudy.zip" : ".spsmodel.zip", StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("PACKAGE_EXTENSION: Paket türüne uygun dosya uzantısı gereklidir.");
            ModelExport.Save(package, path, Overwrite(p), cancellation); return new { path, sha256 = package.Sha256, run_id = package.RunId, bytes = new FileInfo(path).Length };
        }
        private void ExportStudy()
        {
            var package = ModelExport.ReadStudyPackage(lastManifest);
            using (var dialog = new SaveFileDialog { Title = "Tam çalışma paketini kaydet", Filter = "Çalışma arşivi (*.spsstudy.zip)|*.spsstudy.zip", FileName = Path.GetFileName(package.SourcePath), OverwritePrompt = true })
                if (dialog.ShowDialog(this) == DialogResult.OK) { ModelExport.Save(package, dialog.FileName, true); Log("Tam çalışma paketi kaydedildi ve dosya özeti doğrulandı: " + dialog.FileName); }
        }
    }
}
