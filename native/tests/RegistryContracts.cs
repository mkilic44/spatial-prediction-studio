using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace SpatialPredictionStudio
{
    internal static class RegistryContracts
    {
        private static int count;
        private static void Check(bool condition, string label) { if (!condition) throw new Exception(label); ++count; Console.WriteLine("PASS " + label); }
        internal static int Run(string registryPath, string root)
        {
            string originalHash = ConfigurationIdentity.FileDigest(registryPath);
            var raw = Json.Read(registryPath); var categories = (Dictionary<string, object>)raw["categories"];
            foreach (IEnumerable list in categories.Values) foreach (Dictionary<string, object> method in list) if (Json.Text(method, "stage") == "B") method["enabled"] = true;
            string fixturePath = Path.Combine(root, "accepted_methods.fixture.json"); Json.Write(fixturePath, raw); var registry = MethodRegistry.Load(fixturePath);
            Check(registry.Category("model").Any(m => m.Id == "ridge" && m.Enabled), "registry fixture enables B without changing production catalog");
            foreach (var category in registry.Categories)
                foreach (var method in category.Value.Where(m => m.Enabled))
                    using (var editor = new RegistryEditor(category.Value, method.Id, key => new[] { new Choice("r1"), new Choice("r2") }))
                    {
                        string selector = category.Key == "vif" ? "mode" : category.Key == "preprocessing" ? null : "method";
                        var config = editor.Configuration(selector); editor.Restore(config, selector);
                        Check(ConfigurationIdentity.Digest(config) == ConfigurationIdentity.Digest(editor.Configuration(selector)), "dynamic parameter roundtrip " + category.Key + "/" + method.Id);
                    }
            var disabledFixture = registry.Category("model").ToList();
            disabledFixture.Add(new MethodSpec { Id = "disabled_fixture", Label = "Disabled test method", Enabled = false, Reason = "Test only", Parameters = new Dictionary<string, object>() });
            using (var editor = new RegistryEditor(disabledFixture, "ridge", key => new Choice[0]))
            {
                string disabledId = "disabled_fixture";
                bool rejected = false; try { editor.Select(disabledId); } catch (InvalidDataException) { rejected = true; }
                Check(rejected && editor.MethodId == "ridge", "disabled method cannot replace current model");
                editor.Parameters.Set("alpha", 3.25); Check(Convert.ToDouble(editor.Parameters.Values()["alpha"]) == 3.25, "numeric model parameter preserved");
            }
            using (var editor = new SearchSpaceEditor())
            {
                editor.SetParameters(new Dictionary<string, Dictionary<string, object>> { { "ridge.alpha", new Dictionary<string, object> { { "type", "float" }, { "min", .001 }, { "default", 1 } } }, { "feature_selection.k", new Dictionary<string, object> { { "type", "int" }, { "min", 1 }, { "default", 1 } } } });
                var value = new Dictionary<string, object> { { "ridge.alpha", new Dictionary<string, object> { { "type", "float" }, { "low", .01 }, { "high", 10 }, { "log", true } } }, { "feature_selection.k", new Dictionary<string, object> { { "type", "categorical" }, { "choices", new[] { 1, 2 } } } } };
                editor.Restore(value); Check(ConfigurationIdentity.Digest(value) == ConfigurationIdentity.Digest(editor.Values()), "typed family and pipeline search spaces roundtrip");
            }
            using (var editor = new CustomFoldsEditor())
            {
                var folds = new[] { new Dictionary<string, object> { { "repeat", 0 }, { "fold", 0 }, { "train_ids", new[] { "A", "B" } }, { "test_ids", new[] { "C" } } } };
                editor.Restore(folds); Check(ConfigurationIdentity.Digest(folds) == ConfigurationIdentity.Digest(editor.Values()), "custom fold train and test table roundtrip");
                folds[0]["test_ids"] = new[] { "A" }; editor.Restore(folds); bool rejected = false; try { editor.Values(); } catch (InvalidDataException) { rejected = true; } Check(rejected, "custom fold overlap refused");
            }
            var scope = new PointScope { DefinitionExpression = "soil > 0", SelectedOnly = true, SelectedIds = new[] { "A" } };
            var project = new Dictionary<string, object> { { "point_scope", scope.ToRecord() }, { "science", new Dictionary<string, object> { { "z", 1.0 }, { "a", new[] { 3, 2, 1 } } } } };
            string file = Path.Combine(root, "project_fixture.json"); Json.Write(file, project); Check(ConfigurationIdentity.Digest(project) == ConfigurationIdentity.Digest(Json.Read(file)), "project digest stable after JSON readback and anonymous records");
            var change = Json.Read(file); ((Dictionary<string, object>)change["science"])["z"] = 2; Check(ConfigurationIdentity.Digest(project) != ConfigurationIdentity.Digest(change), "scientific change changes preview signature");
            Check(ConfigurationIdentity.FileDigest(registryPath) == originalHash, "production registry remains byte identical");
            string report = Path.Combine(root, "report.html"), local = Path.Combine(root, "local.csv"); File.WriteAllText(local, "id,value\nS1,1\n");
            File.WriteAllText(report, "<html><body><a href=\"local.csv\">Local table</a></body></html>");
            var hashes = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase) { { report, ConfigurationIdentity.FileDigest(report) }, { local, ConfigurationIdentity.FileDigest(local) } };
            ArtifactViewer.ValidateHtml(report, hashes); Check(true, "embedded report accepts hash verified local linked outputs");
            File.WriteAllText(report, "<html><body><img src=\"https://example.invalid/remote.png\"></body></html>"); hashes[report] = ConfigurationIdentity.FileDigest(report);
            bool remoteRejected = false; try { ArtifactViewer.ValidateHtml(report, hashes); } catch (InvalidDataException) { remoteRejected = true; } Check(remoteRejected, "embedded report refuses network resources");
            File.WriteAllText(report, "<html><body><script>alert(1)</script></body></html>"); hashes[report] = ConfigurationIdentity.FileDigest(report);
            bool scriptRejected = false; try { ArtifactViewer.ValidateHtml(report, hashes); } catch (InvalidDataException) { scriptRejected = true; } Check(scriptRejected, "embedded report refuses active scripts");
            return count;
        }
    }
}
