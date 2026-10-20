using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace SpatialPredictionStudio
{
    internal static class Json
    {
        internal static JavaScriptSerializer Serializer() { return new JavaScriptSerializer { MaxJsonLength = 64 * 1024 * 1024, RecursionLimit = 100 }; }
        internal static Dictionary<string, object> Read(string path) { return Serializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(path, Encoding.UTF8)); }
        internal static void Write(string path, object value) { File.WriteAllText(path, Serializer().Serialize(value), new UTF8Encoding(false)); }
        internal static string Text(Dictionary<string, object> value, string key, string fallback = "") { object obj; return value.TryGetValue(key, out obj) && obj != null ? Convert.ToString(obj, CultureInfo.InvariantCulture) : fallback; }
    }

    internal sealed class RuntimeSettings
    {
        public string PythonPath;
        public string WorkerPath;
        public string ExamplesPath;
        public string ModelsPath;
        internal string RegistryPath { get { return Path.Combine(Path.GetDirectoryName(WorkerPath), "sps", "registry.json"); } }
        internal static RuntimeSettings Load()
        {
            var directory = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            var path = Path.Combine(directory, "runtime.json");
            if (!File.Exists(path)) throw new FileNotFoundException("runtime.json eklenti yanında bulunamadı.", path);
            var value = Json.Read(path);
            Func<string, string> resolve = key => {
                var p = Json.Text(value, key);
                if (String.IsNullOrWhiteSpace(p)) throw new InvalidDataException("runtime.json alanı eksik: " + key);
                return Path.GetFullPath(Path.IsPathRooted(p) ? p : Path.Combine(directory, p));
            };
            return new RuntimeSettings { PythonPath = resolve("python_executable"), WorkerPath = resolve("worker_path"), ExamplesPath = resolve("examples_dir"),
                ModelsPath = String.IsNullOrWhiteSpace(Json.Text(value, "models_dir")) ? null : resolve("models_dir") };
        }
        internal void Validate()
        {
            if (!File.Exists(PythonPath)) throw new FileNotFoundException("İzole Python yorumlayıcısı bulunamadı.", PythonPath);
            if (!File.Exists(WorkerPath)) throw new FileNotFoundException("Python işçisi bulunamadı.", WorkerPath);
        }
    }

    internal sealed class JobClient
    {
        private string outputDirectory;
        private readonly object processGate = new object();
        private Process activeProcess;
        private OwnedProcessJob activeJob;
        private Action<string> activeLog;
        private int generation;
        private bool forcedTermination;
        private bool shutdownRequested;
        internal bool Running { get; private set; }
        internal Task<Dictionary<string, object>> RunAsync(RuntimeSettings runtime, string requestPath, string outputDir, string runId, Action<string> log, Action<int, int, string> progress, CancellationToken preparation = default(CancellationToken))
        {
            if (Running) throw new InvalidOperationException("Bir iş zaten çalışıyor.");
            lock (processGate) if (shutdownRequested) throw new InvalidOperationException("Kapatılan panel yeni iş başlatamaz.");
            runtime.Validate();
            var request = Json.Read(requestPath);
            int schema = request.ContainsKey("schema_version") ? Convert.ToInt32(request["schema_version"]) : 1;
            if (schema != 1 && schema != 2) throw new InvalidDataException("Desteklenmeyen iş şeması.");
            preparation.ThrowIfCancellationRequested();
            outputDirectory = outputDir;
            Running = true;
            lock (processGate) { ++generation; forcedTermination = false; activeLog = log; }
            return Task.Run(() => {
                try
                {
                    var info = new ProcessStartInfo {
                        FileName = runtime.PythonPath,
                        Arguments = "-u " + Quote(runtime.WorkerPath) + " " + Quote(requestPath),
                        WorkingDirectory = Path.GetDirectoryName(runtime.WorkerPath),
                        UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden,
                        RedirectStandardOutput = true, RedirectStandardError = true, RedirectStandardInput = true,
                        StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
                    };
                    info.EnvironmentVariables.Remove("PYTHONPATH");
                    info.EnvironmentVariables.Remove("PYTHONHOME");
                    info.EnvironmentVariables["PYTHONUTF8"] = "1";
                    info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                    info.EnvironmentVariables["PROJ_NETWORK"] = "OFF";
                    info.EnvironmentVariables["TABPFN_DISABLE_TELEMETRY"] = "1";
                    info.EnvironmentVariables["DO_NOT_TRACK"] = "1";
                    info.EnvironmentVariables["HF_HUB_OFFLINE"] = "1";
                    if (!String.IsNullOrWhiteSpace(runtime.ModelsPath)) info.EnvironmentVariables["SPS_MODELS_DIR"] = runtime.ModelsPath;
                    else info.EnvironmentVariables.Remove("SPS_MODELS_DIR");
                    var startToken = Guid.NewGuid().ToString("N");
                    info.EnvironmentVariables["SPS_START_GATE"] = startToken;
                    using (var ownedJob = new OwnedProcessJob())
                    using (var process = new Process { StartInfo = info })
                    {
                        process.OutputDataReceived += (s, e) => {
                            if (String.IsNullOrWhiteSpace(e.Data)) return;
                            log(e.Data);
                            try {
                                var item = Json.Serializer().Deserialize<Dictionary<string, object>>(e.Data);
                                if (Json.Text(item, "event") == "progress" && Json.Text(item, "run_id") == runId)
                                    progress(Convert.ToInt32(item["completed"]), Convert.ToInt32(item["total"]), Json.Text(item, "message"));
                            } catch (Exception ex) { log("İlerleme kaydı çözümlenemedi: " + ex.Message); }
                        };
                        process.ErrorDataReceived += (s, e) => { if (!String.IsNullOrWhiteSpace(e.Data)) log("İşçi: " + e.Data); };
                        lock (processGate)
                        {
                            preparation.ThrowIfCancellationRequested();
                            if (shutdownRequested) throw new OperationCanceledException("Panel kapandığı için işçi başlatılmadı.");
                            if (!process.Start()) throw new InvalidOperationException("İşçi süreci başlatılamadı.");
                            activeProcess = process;
                            try
                            {
                                // The worker waits on stdin before importing or spawning anything.
                                ownedJob.Assign(process);
                                activeJob = ownedJob;
                                process.StandardInput.WriteLine(startToken);
                                process.StandardInput.Flush();
                                process.StandardInput.Close();
                            }
                            catch
                            {
                                ownedJob.Dispose();
                                if (!process.HasExited) process.Kill();
                                process.WaitForExit(5000);
                                throw;
                            }
                        }
                        log("İşçi başladı. Süreç kimliği: " + process.Id);
                        process.BeginOutputReadLine(); process.BeginErrorReadLine();
                        while (!process.WaitForExit(250)) { }
                        // Close the job before draining redirected pipes: a remaining child
                        // could otherwise keep stdout open after its parent has exited.
                        lock (processGate) { ownedJob.Dispose(); activeJob = null; }
                        process.WaitForExit();
                        log("İşçi çıkış kodu: " + process.ExitCode);
                        lock (processGate)
                        {
                            if (forcedTermination) throw new OperationCanceledException("İşçi ve bu işe ait alt süreçler kapatıldı; tamamlanmamış çıktılar haritaya eklenmedi.");
                        }
                        var resultPath = Path.Combine(outputDir, "result.json");
                        if (!File.Exists(resultPath)) throw new InvalidDataException("İşçi sonuç bildirimi üretmedi. Çıkış kodu: " + process.ExitCode);
                        var result = Json.Read(resultPath);
                        if (Json.Text(result, "run_id") != runId || Convert.ToInt32(result["schema_version"]) != schema)
                            throw new InvalidDataException("Sonuç kimliği veya şema sürümü istekle eşleşmiyor.");
                        if (process.ExitCode != 0 && Json.Text(result, "status") == "completed")
                            throw new InvalidDataException("İşçi hata koduyla kapandı; başarılı sonuç kabul edilmedi.");
                        result["manifest_path"] = resultPath;
                        return result;
                    }
                }
                finally { lock (processGate) { activeJob?.Dispose(); activeJob = null; activeProcess = null; activeLog = null; Running = false; } }
            });
        }
        internal void Cancel()
        {
            if (!Running || String.IsNullOrEmpty(outputDirectory)) return;
            Directory.CreateDirectory(outputDirectory);
            File.WriteAllText(Path.Combine(outputDirectory, "cancel.request"), DateTime.UtcNow.ToString("O"), new UTF8Encoding(false));
            int cancelledGeneration;
            lock (processGate) cancelledGeneration = generation;
            Task.Delay(10000).ContinueWith(task => {
                lock (processGate)
                {
                    if (!Running || cancelledGeneration != generation || activeProcess == null) return;
                    try
                    {
                        if (!activeProcess.HasExited)
                        {
                            int processId = activeProcess.Id;
                            forcedTermination = true; activeJob?.Dispose();
                            if (!activeProcess.HasExited) activeProcess.Kill();
                            activeLog?.Invoke("İptal zaman aşımı: işçi ve sahipli alt süreçler kapatıldı. Süreç kimliği: " + processId);
                        }
                    }
                    catch (InvalidOperationException) { }
                    catch (System.ComponentModel.Win32Exception ex) { activeLog?.Invoke("İşçi sonlandırılamadı: " + ex.Message); }
                }
            });
        }
        internal void StopForShutdown()
        {
            // Do not leave a delayed cancellation callback dependent on ArcMap staying open.
            lock (processGate)
            {
                shutdownRequested = true;
                if (activeProcess == null) return;
                try
                {
                    if (!activeProcess.HasExited) { forcedTermination = true; activeJob?.Dispose(); if (!activeProcess.HasExited) activeProcess.Kill(); }
                }
                catch (InvalidOperationException) { }
                catch (System.ComponentModel.Win32Exception ex) { activeLog?.Invoke("Panel kapanırken işçi sonlandırılamadı: " + ex.Message); }
            }
        }
        private static string Quote(string value)
        {
            if (value.IndexOf('"') >= 0) throw new ArgumentException("Dosya yolu çift tırnak içeremez.");
            return "\"" + value + "\"";
        }
    }
}
