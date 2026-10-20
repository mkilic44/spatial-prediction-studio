using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;

namespace SpatialPredictionStudio
{
    internal sealed class ModelPackage
    {
        internal string SourcePath, Sha256, RunId, ManifestPath, ManifestSha256;
        internal string[] ProtectedPaths;
    }

    internal static class ModelExport
    {
        internal static ModelPackage ReadPackage(string manifestPath, CancellationToken cancellation = default(CancellationToken))
        { return ReadPackageKind(manifestPath, "model_bundle", cancellation); }
        internal static ModelPackage ReadStudyPackage(string manifestPath, CancellationToken cancellation = default(CancellationToken))
        { return ReadPackageKind(manifestPath, "study_package", cancellation); }
        private static ModelPackage ReadPackageKind(string manifestPath, string kind, CancellationToken cancellation)
        {
            if (String.IsNullOrWhiteSpace(manifestPath) || !File.Exists(manifestPath))
                throw new FileNotFoundException("Select a completed run first.", manifestPath);
            manifestPath = Path.GetFullPath(manifestPath);
            using (var manifestLock = new FileStream(manifestPath, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                var result = Json.Read(manifestPath);
                if (Json.Text(result, "schema_version") != "2" || Json.Text(result, "status") != "completed" || String.IsNullOrWhiteSpace(Json.Text(result, "run_id")))
                    throw new InvalidDataException("A package can only be saved from a completed, valid run record.");
                object value;
                var entries = result.TryGetValue("outputs", out value) && !(value is string) ? value as IEnumerable : null;
                if (entries == null) throw new InvalidDataException("The completed run has no output list.");
                var outputs = entries.Cast<object>().Select(item => item as Dictionary<string, object>).ToList();
                if (outputs.Any(item => item == null)) throw new InvalidDataException("The output list in the run record is invalid.");
                var packages = outputs.Where(item => Json.Text(item, "kind") == kind).ToList();
                if (packages.Count != 1) throw new InvalidDataException(kind == "study_package" ? "The full study package is missing from this record or is not unique. For an older run, run the analysis again." : "The run must contain exactly one model package. For an older run, run the model again.");
                string directory = Path.GetDirectoryName(manifestPath);
                string source = ArtifactIntegrity.ResolveOutputPath(directory, Json.Text(packages[0], "path"));
                string sha = Json.Text(packages[0], "sha256");
                ArtifactIntegrity.VerifySha256(source, sha, cancellation);
                return new ModelPackage {
                    SourcePath = source, Sha256 = sha, RunId = Json.Text(result, "run_id"),
                    ManifestPath = manifestPath, ManifestSha256 = ConfigurationIdentity.FileDigest(manifestPath),
                    ProtectedPaths = outputs.Select(item => ArtifactIntegrity.ResolveOutputPath(directory, Json.Text(item, "path")))
                        .Concat(new[] { manifestPath }).ToArray()
                };
            }
        }

        internal static void Save(ModelPackage package, string destination, bool allowOverwrite, CancellationToken cancellation = default(CancellationToken))
        {
            if (package == null) throw new InvalidDataException("Verified model package not found.");
            if (String.IsNullOrWhiteSpace(destination) || !Path.IsPathRooted(destination))
                throw new InvalidDataException("Choose the full file path for saving the model.");
            destination = Path.GetFullPath(destination);
            if (package.ProtectedPaths.Any(path => String.Equals(path, destination, StringComparison.OrdinalIgnoreCase)))
                throw new InvalidDataException("The source files of the completed run cannot be overwritten. Choose another file name.");
            string directory = Path.GetDirectoryName(destination);
            if (!Directory.Exists(directory)) throw new DirectoryNotFoundException("The folder for saving the model was not found.");
            if (File.Exists(destination) && !allowOverwrite) throw new IOException("Overwriting the existing model was not confirmed.");
            string token = Guid.NewGuid().ToString("N");
            string temporary = Path.Combine(directory, ".sps-model-" + token + ".tmp");
            string backup = Path.Combine(directory, ".sps-model-" + token + ".bak");
            bool installed = false, completed = false;
            try
            {
                // Keep both inputs read-locked across copying and destination readback.
                using (var manifestLock = new FileStream(package.ManifestPath, FileMode.Open, FileAccess.Read, FileShare.Read))
                using (var source = new FileStream(package.SourcePath, FileMode.Open, FileAccess.Read, FileShare.Read))
                {
                    ArtifactIntegrity.VerifySha256(package.ManifestPath, package.ManifestSha256, cancellation);
                    ArtifactIntegrity.VerifySha256(package.SourcePath, package.Sha256, cancellation);
                    using (var staged = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                    {
                        byte[] buffer = new byte[1024 * 1024]; int read;
                        while ((read = source.Read(buffer, 0, buffer.Length)) != 0) { cancellation.ThrowIfCancellationRequested(); staged.Write(buffer, 0, read); }
                        cancellation.ThrowIfCancellationRequested(); staged.Flush(true);
                    }
                    ArtifactIntegrity.VerifySha256(temporary, package.Sha256, cancellation);
                    ArtifactIntegrity.VerifySha256(package.SourcePath, package.Sha256, cancellation);
                    cancellation.ThrowIfCancellationRequested();
                    if (File.Exists(destination))
                    {
                        if (!allowOverwrite) throw new IOException("The destination file appeared during saving; overwriting it was not confirmed.");
                        File.Replace(temporary, destination, backup);
                    }
                    else File.Move(temporary, destination);
                    installed = true;
                    ArtifactIntegrity.VerifySha256(destination, package.Sha256, cancellation);
                    ArtifactIntegrity.VerifySha256(package.SourcePath, package.Sha256, cancellation);
                    ArtifactIntegrity.VerifySha256(package.ManifestPath, package.ManifestSha256, cancellation);
                    cancellation.ThrowIfCancellationRequested();
                    completed = true;
                }
            }
            catch (Exception failure)
            {
                if (installed)
                {
                    try
                    {
                        if (File.Exists(backup))
                        {
                            if (File.Exists(destination)) File.Replace(backup, destination, null);
                            else File.Move(backup, destination);
                        }
                        else if (File.Exists(destination)) File.Delete(destination);
                    }
                    catch (Exception recovery)
                    {
                        throw new IOException("The saved model could not be verified and the previous destination could not be restored. Kept backup: " + backup,
                            new AggregateException(failure, recovery));
                    }
                }
                throw;
            }
            finally
            {
                RemoveTemporary(temporary);
                if (completed) RemoveTemporary(backup);
            }
        }

        private static void RemoveTemporary(string path)
        {
            try { if (File.Exists(path)) File.Delete(path); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }
}
