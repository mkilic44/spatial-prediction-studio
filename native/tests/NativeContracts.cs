using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using System.Threading;
using System.Threading.Tasks;

namespace SpatialPredictionStudio
{
    internal static class NativeContracts
    {
        private static int assertions;
        private static void Check(bool condition, string label) { if (!condition) throw new Exception(label); ++assertions; Console.WriteLine("PASS " + label); }
        private static void Reject(Action action, string label)
        {
            try { action(); } catch (InvalidDataException) { Check(true, label); return; }
            throw new Exception("Expected InvalidDataException: " + label);
        }
        private static void ExpectCancelled(Task task)
        {
            try { if (!task.Wait(5000)) throw new Exception("Worker did not exit within 5 seconds"); }
            catch (AggregateException ex) { if (!(ex.GetBaseException() is OperationCanceledException)) throw; return; }
            throw new Exception("Expected observed worker cancellation");
        }
        private static string Digest(string path)
        {
            using (var stream = File.OpenRead(path)) using (var hash = SHA256.Create()) return BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
        }
        private static Task<Dictionary<string, object>> Start(JobClient client, RuntimeSettings runtime, string directory, string mode, int schema = 1)
        {
            Directory.CreateDirectory(directory);
            string requestPath = Path.Combine(directory, "request.json");
            string output = Path.Combine(directory, "output");
            Json.Write(requestPath, new { schema_version = schema, run_id = Path.GetFileName(directory), output_dir = output, mode = mode });
            return client.RunAsync(runtime, requestPath, output, Path.GetFileName(directory), message => Console.WriteLine(message), (a, b, c) => { });
        }
        [STAThread]
        private static int Main(string[] args)
        {
            try
            {
                string root = Path.GetFullPath(args[2]); Directory.CreateDirectory(root);
                var scope = new PointScope { DefinitionExpression = "soil_value > 10", SelectedOnly = true, SelectedIds = new[] { "S002", "S001" } };
                string scopeFile = Path.Combine(root, "scope.json"); Json.Write(scopeFile, scope.ToRecord());
                Check(scope.EquivalentTo(PointScope.FromRecord(Json.Read(scopeFile), true)), "scope roundtrip preserves query and stable IDs");
                Check(!scope.EquivalentTo(new PointScope { DefinitionExpression = "soil_value > 20", SelectedOnly = true, SelectedIds = scope.SelectedIds }), "changed query does not match saved scope");
                Reject(() => PointScope.FromRecord(null, false), "legacy project scope refused");
                Reject(() => PointScope.FromRecord(Json.Read(scopeFile), false), "selection flag mismatch refused");
                var duplicate = Json.Read(scopeFile); duplicate["selected_ids"] = new[] { "S001", "S001" };
                Reject(() => PointScope.FromRecord(duplicate, true), "duplicate saved stable IDs refused");
                string artifact = Path.Combine(root, "artifact.tif"); File.WriteAllText(artifact, "verified bytes");
                string digest = Digest(artifact); ArtifactIntegrity.VerifySha256(artifact, digest.ToUpperInvariant()); Check(true, "matching digest accepted case-insensitively");
                File.AppendAllText(artifact, " modified"); Reject(() => ArtifactIntegrity.VerifySha256(artifact, digest), "modified output rejected");
                Reject(() => ArtifactIntegrity.VerifySha256(artifact, ""), "missing output digest rejected");
                Check(ArtifactIntegrity.ResolveOutputPath(root, "artifact.tif") == artifact, "relative output stays in result directory");
                Reject(() => ArtifactIntegrity.ResolveOutputPath(root, "..\\outside.tif"), "output path traversal refused before read");
                if (args.Length > 3) assertions += RegistryContracts.Run(Path.GetFullPath(args[3]), root);
                assertions += ModelExportContracts.Run(root);
                assertions += ArtifactViewerContracts.Run(root);
                assertions += McpBridgeContracts.Run(root);
                var runtime = new RuntimeSettings { PythonPath = Path.GetFullPath(args[0]), WorkerPath = Path.GetFullPath(args[1]) };
                var prepared = new JobClient(); string prepareDirectory = Path.Combine(root, "cancelled-before-worker");
                Directory.CreateDirectory(prepareDirectory); string prepareRequest = Path.Combine(prepareDirectory, "request.json");
                Json.Write(prepareRequest, new { schema_version = 1 });
                using (var cancelled = new CancellationTokenSource()) {
                    cancelled.Cancel(); bool rejected = false;
                    try { prepared.RunAsync(runtime, prepareRequest, Path.Combine(prepareDirectory, "output"), "not-started", message => { }, (a,b,c) => { }, cancelled.Token); }
                    catch (OperationCanceledException) { rejected = true; }
                    Check(rejected && !prepared.Running && !Directory.Exists(Path.Combine(prepareDirectory, "output")), "cancelled preparation cannot cross worker admission boundary");
                }
                var normal = new JobClient(); var normalResult = Start(normal, runtime, Path.Combine(root, "normal"), "complete").Result;
                Check(Json.Text(normalResult, "status") == "completed" && !normal.Running, "normal worker completion read from manifest");
                var versionTwo = new JobClient(); var versionTwoResult = Start(versionTwo, runtime, Path.Combine(root, "schema_two"), "complete", 2).Result;
                Check(Convert.ToInt32(versionTwoResult["schema_version"]) == 2, "schema 2 result matched to actual request");
                var mismatch = new JobClient(); bool refused = false;
                try { Start(mismatch, runtime, Path.Combine(root, "schema_mismatch"), "wrong_schema", 2).Wait(); } catch (AggregateException ex) { refused = ex.GetBaseException() is InvalidDataException; }
                Check(refused, "wrong worker schema refused");
                using (var independent = Process.Start(new ProcessStartInfo { FileName = runtime.PythonPath, Arguments = "-c \"import time; time.sleep(30)\"", UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden }))
                {
                    try
                    {
                        var held = new JobClient(); string heldDirectory = Path.Combine(root, "held");
                        var task = Start(held, runtime, heldDirectory, "hold");
                        string pidPath = Path.Combine(heldDirectory, "output", "pid.txt");
                        var deadline = Stopwatch.StartNew(); while (!File.Exists(pidPath) && deadline.ElapsedMilliseconds < 10000) Thread.Sleep(20);
                        Check(File.Exists(pidPath), "owned worker actually started");
                        int pid = Int32.Parse(File.ReadAllText(pidPath));
                        var closeTimer = Stopwatch.StartNew(); held.StopForShutdown(); closeTimer.Stop();
                        Check(closeTimer.ElapsedMilliseconds < 2000, "shutdown does not block for cancellation grace");
                        ExpectCancelled(task); Check(!held.Running, "shutdown completed owned process task");
                        bool exited; try { using (var process = Process.GetProcessById(pid)) exited = process.HasExited; } catch (ArgumentException) { exited = true; }
                        Check(exited, "exact owned worker process exited");
                        Check(!independent.HasExited, "unrelated Python process remains running");
                    }
                    finally { if (!independent.HasExited) independent.Kill(); }
                }
                var race = new JobClient(); var racingTask = Start(race, runtime, Path.Combine(root, "startup_race"), "hold"); race.StopForShutdown(); ExpectCancelled(racingTask);
                Check(!race.Running, "shutdown/startup race leaves no active job");
                Json.Write(Path.Combine(root, "native_contract_result.json"), new { status = "passed", assertions = assertions, scope = "managed helpers and real child process lifecycle; no ArcObjects UI acceptance" });
                Console.WriteLine("PASS assertions=" + assertions); return 0;
            }
            catch (Exception ex) { Console.Error.WriteLine(ex); return 1; }
        }
    }
}
