using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Threading.Tasks;

namespace SpatialPredictionStudio
{
    internal static class OwnedProcessContracts
    {
        private static void Check(bool condition, string text)
        {
            if (!condition) throw new Exception(text);
            Console.WriteLine("PASS " + text);
        }
        private static bool Exited(int pid)
        {
            try { using (var process = Process.GetProcessById(pid)) return process.HasExited; }
            catch (ArgumentException) { return true; }
        }
        private static void AwaitFile(string path)
        {
            var elapsed = Stopwatch.StartNew();
            while (!File.Exists(path) && elapsed.ElapsedMilliseconds < 10000) Thread.Sleep(20);
            Check(File.Exists(path), "worker produced " + Path.GetFileName(path));
        }
        private static int Main(string[] args)
        {
            try
            {
                var root = Path.GetFullPath(args[2]); Directory.CreateDirectory(root);
                var runtime = new RuntimeSettings { PythonPath = Path.GetFullPath(args[0]), WorkerPath = Path.GetFullPath(args[1]), ModelsPath = Path.Combine(root, "external_models") };
                using (var independent = Process.Start(new ProcessStartInfo {
                    FileName = runtime.PythonPath, Arguments = "-c \"import time; time.sleep(60)\"",
                    UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden }))
                {
                    try
                    {
                        foreach (var mode in new[] { "shutdown", "normal", "cancel" })
                        {
                            var directory = Path.Combine(root, mode); Directory.CreateDirectory(directory);
                            var output = Path.Combine(directory, "output");
                            var request = Path.Combine(directory, "request.json");
                            Json.Write(request, new { schema_version = 2, run_id = mode, output_dir = output, mode = mode });
                            var client = new JobClient();
                            var task = client.RunAsync(runtime, request, output, mode, Console.WriteLine, (a, b, c) => { });
                            var pidPath = Path.Combine(output, "child.pid"); AwaitFile(pidPath);
                            int child = Int32.Parse(File.ReadAllText(pidPath));
                            Check(File.ReadAllText(Path.Combine(output, "models.txt")) == runtime.ModelsPath, "external model directory reaches worker");
                            Check(File.ReadAllText(Path.Combine(output, "gate.txt")) == "passed", "native assignment gate released worker");
                            if (mode == "shutdown") client.StopForShutdown();
                            if (mode == "cancel") client.Cancel();
                            bool cancelled = false;
                            try { Check(task.Wait(mode == "cancel" ? 15000 : 5000), "worker task completed without inherited pipe deadlock"); }
                            catch (AggregateException ex) { cancelled = ex.GetBaseException() is OperationCanceledException; if (!cancelled) throw; }
                            Check(mode == "normal" ? !cancelled : cancelled, "expected completion or cancellation status");
                            var elapsed = Stopwatch.StartNew();
                            while (!Exited(child) && elapsed.ElapsedMilliseconds < 5000) Thread.Sleep(20);
                            Check(Exited(child), "owned descendant exited after " + mode);
                            Check(!independent.HasExited, "unrelated process survives " + mode);
                        }
                    }
                    finally { if (!independent.HasExited) { independent.Kill(); independent.WaitForExit(5000); } }
                }
                Json.Write(Path.Combine(root, "result.json"), new { status = "passed", scenarios = new[] { "shutdown_tree", "normal_orphan_cleanup", "cancel_timeout_tree", "stdin_gate", "unrelated_process_preserved" } });
                return 0;
            }
            catch (Exception ex) { Console.Error.WriteLine(ex); return 1; }
        }
    }
}
