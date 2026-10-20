using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace SpatialPredictionStudio
{
    internal static class ModelExportContracts
    {
        private static int count;
        private static void Check(bool condition, string label) { if (!condition) throw new Exception(label); ++count; Console.WriteLine("PASS " + label); }
        private static void Reject(Action action, string label)
        {
            try { action(); } catch (InvalidDataException) { Check(true, label); return; } catch (IOException) { Check(true, label); return; }
            throw new Exception("Expected rejected export: " + label);
        }
        internal static int Run(string root)
        {
            count = 0;
            string sourceRoot = Path.Combine(root, "model-source"), destinationRoot = Path.Combine(root, "model-export");
            Directory.CreateDirectory(sourceRoot); Directory.CreateDirectory(destinationRoot);
            string source = Path.Combine(sourceRoot, "model_package.spsmodel.zip"), manifest = Path.Combine(sourceRoot, "result.json");
            File.WriteAllText(source, "locally generated model package fixture");
            string expected = ConfigurationIdentity.FileDigest(source);
            var entry = new Dictionary<string, object> { { "kind", "model_bundle" }, { "path", Path.GetFileName(source) }, { "sha256", expected } };
            var result = new Dictionary<string, object> { { "schema_version", 2 }, { "status", "completed" }, { "run_id", "accepted-run" }, { "outputs", new[] { entry } } };
            Json.Write(manifest, result);
            var package = ModelExport.ReadPackage(manifest);
            Check(package.SourcePath == source && package.RunId == "accepted-run" && package.Sha256 == expected, "model export resolves completed run and verified package");
            string destination = Path.Combine(destinationRoot, "saved.spsmodel.zip");
            ModelExport.Save(package, destination, false);
            Check(ConfigurationIdentity.FileDigest(destination) == expected && ConfigurationIdentity.FileDigest(source) == expected, "new model export is hash-equivalent and preserves source");
            File.WriteAllText(destination, "previous user model");
            Reject(() => ModelExport.Save(package, destination, false), "existing model needs explicit overwrite authorization");
            Check(File.ReadAllText(destination) == "previous user model", "unapproved overwrite preserves original destination");
            ModelExport.Save(package, destination, true);
            Check(ConfigurationIdentity.FileDigest(destination) == expected, "authorized model replacement is verified after atomic replacement");
            Reject(() => ModelExport.Save(package, source, true), "export refuses overwriting its own run package");
            Reject(() => ModelExport.Save(package, manifest, true), "export refuses overwriting its own result manifest");
            string report = Path.Combine(sourceRoot, "study_report.html"); File.WriteAllText(report, "completed report");
            result["outputs"] = new[] { entry, new Dictionary<string, object> { { "kind", "report" }, { "path", Path.GetFileName(report) }, { "sha256", ConfigurationIdentity.FileDigest(report) } } };
            Json.Write(manifest, result); package = ModelExport.ReadPackage(manifest);
            Reject(() => ModelExport.Save(package, report, true), "export protects other artifacts of the successful run");
            File.WriteAllText(destination, "locked previous user model");
            using (var locked = new FileStream(destination, FileMode.Open, FileAccess.Read, FileShare.Read))
                Reject(() => ModelExport.Save(package, destination, true), "locked destination fails without deleting previous model");
            Check(File.ReadAllText(destination) == "locked previous user model", "failed atomic replacement preserves previous target");
            Check(Directory.GetFiles(destinationRoot).SequenceEqual(new[] { destination }), "failed and successful exports leave no staging files");
            File.AppendAllText(source, " changed");
            Reject(() => ModelExport.Save(package, destination, true), "source changed after dialog preparation is rejected");
            Check(File.ReadAllText(destination) == "locked previous user model", "changed source cannot replace previous target");
            File.WriteAllText(source, "locally generated model package fixture");
            result["run_id"] = "changed-run"; Json.Write(manifest, result);
            Reject(() => ModelExport.Save(package, destination, true), "changed manifest after dialog preparation is rejected");
            foreach (string status in new[] { "failed", "cancelled", "running" })
            {
                result["status"] = status; Json.Write(manifest, result);
                Reject(() => ModelExport.ReadPackage(manifest), "model package cannot be exported from " + status + " run");
            }
            result["status"] = "completed"; result["outputs"] = new object[0]; Json.Write(manifest, result);
            Reject(() => ModelExport.ReadPackage(manifest), "legacy successful run without package is reported explicitly");
            result["outputs"] = new[] { entry, entry }; Json.Write(manifest, result);
            Reject(() => ModelExport.ReadPackage(manifest), "ambiguous duplicate model packages are rejected");
            entry["path"] = "..\\outside.spsmodel.zip"; result["outputs"] = new[] { entry }; Json.Write(manifest, result);
            Reject(() => ModelExport.ReadPackage(manifest), "model package cannot escape output directory");
            entry["path"] = Path.GetFileName(source); entry["sha256"] = new string('0', 64); Json.Write(manifest, result);
            Reject(() => ModelExport.ReadPackage(manifest), "wrong package digest is rejected");
            string studySource = Path.Combine(sourceRoot, "study_package.spsstudy.zip");
            File.WriteAllText(studySource, "full study package fixture");
            result["outputs"] = new[] { new Dictionary<string, object> { { "kind", "study_package" }, { "path", Path.GetFileName(studySource) }, { "sha256", ConfigurationIdentity.FileDigest(studySource) } } };
            Json.Write(manifest, result);
            var study = ModelExport.ReadStudyPackage(manifest); string savedStudy = Path.Combine(destinationRoot, "saved.spsstudy.zip");
            ModelExport.Save(study, savedStudy, false);
            Check(ConfigurationIdentity.FileDigest(studySource) == ConfigurationIdentity.FileDigest(savedStudy), "full study export selects correct kind and verifies saved archive");
            Reject(() => ModelExport.ReadPackage(manifest), "full study archive is not silently treated as standalone model");
            File.AppendAllText(studySource, "changed");
            Reject(() => ModelExport.ReadStudyPackage(manifest), "full study export rejects changed archive");
            using (var stream = new FileStream(studySource, FileMode.Create, FileAccess.Write)) stream.SetLength(64 * 1024 * 1024);
            result["outputs"] = new[] { new Dictionary<string, object> { { "kind", "study_package" }, { "path", Path.GetFileName(studySource) }, { "sha256", ConfigurationIdentity.FileDigest(studySource) } } };
            Json.Write(manifest, result); study = ModelExport.ReadStudyPackage(manifest);
            File.WriteAllText(savedStudy, "previous study");
            using (var cancellation = new System.Threading.CancellationTokenSource())
            {
                cancellation.Cancel(); bool cancelled = false;
                try { ModelExport.Save(study, savedStudy, true, cancellation.Token); } catch (OperationCanceledException) { cancelled = true; }
                Check(cancelled && File.ReadAllText(savedStudy) == "previous study", "cancelled study export preserves existing destination");
            }
            using (var cancellation = new System.Threading.CancellationTokenSource())
            {
                var copying = System.Threading.Tasks.Task.Run(() => ModelExport.Save(study, savedStudy, true, cancellation.Token));
                var timer = System.Diagnostics.Stopwatch.StartNew();
                while (!Directory.GetFiles(destinationRoot, ".sps-model-*.tmp").Any() && !copying.IsCompleted && timer.ElapsedMilliseconds < 10000) System.Threading.Thread.Sleep(1);
                Check(!copying.IsCompleted && Directory.GetFiles(destinationRoot, ".sps-model-*.tmp").Any(), "large study export actually entered staged copy");
                cancellation.Cancel(); bool cancelled = false;
                try { copying.GetAwaiter().GetResult(); } catch (OperationCanceledException) { cancelled = true; }
                Check(cancelled && File.ReadAllText(savedStudy) == "previous study" && !Directory.GetFiles(destinationRoot, ".sps-model-*").Any(), "in-progress study export cancellation removes staging and preserves previous file");
            }
            return count;
        }
    }
}
