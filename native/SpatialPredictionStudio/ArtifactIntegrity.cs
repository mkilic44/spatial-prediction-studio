using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Threading;

namespace SpatialPredictionStudio
{
    internal static class ArtifactIntegrity
    {
        internal static string ResolveOutputPath(string directory, string relativeOrAbsolute)
        {
            if (String.IsNullOrWhiteSpace(directory) || String.IsNullOrWhiteSpace(relativeOrAbsolute)) throw new InvalidDataException("The result file path is missing.");
            string prefix = Path.GetFullPath(directory).TrimEnd('\\', '/') + Path.DirectorySeparatorChar;
            string path = Path.GetFullPath(Path.IsPathRooted(relativeOrAbsolute) ? relativeOrAbsolute : Path.Combine(directory, relativeOrAbsolute));
            if (!path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Result file is outside the job folder: " + path);
            if (!File.Exists(path)) throw new FileNotFoundException("Result file not found.", path);
            return path;
        }
        internal static void VerifySha256(string path, string expected, CancellationToken cancellation = default(CancellationToken))
        {
            if (expected == null || expected.Length != 64 || expected.Any(c => !Uri.IsHexDigit(c)))
                throw new InvalidDataException("The content digest in the result manifest is missing or invalid.");
            string actual;
            using (var stream = File.OpenRead(path))
            using (var hash = SHA256.Create())
            {
                byte[] buffer = new byte[1024 * 1024]; int read;
                while ((read = stream.Read(buffer, 0, buffer.Length)) != 0) { cancellation.ThrowIfCancellationRequested(); hash.TransformBlock(buffer, 0, read, null, 0); }
                cancellation.ThrowIfCancellationRequested(); hash.TransformFinalBlock(new byte[0], 0, 0);
                actual = BitConverter.ToString(hash.Hash).Replace("-", "").ToLowerInvariant();
            }
            if (!String.Equals(actual, expected, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("The result file content does not match the record of the completed job: " + path);
        }
    }

    internal sealed class PointScope
    {
        internal string DefinitionExpression = "";
        internal bool SelectedOnly;
        internal string[] SelectedIds = new string[0];
        internal object ToRecord()
        {
            return new { scope_version = 1, definition_expression = DefinitionExpression, selected_only = SelectedOnly, selected_ids = SelectedIds };
        }
        internal static PointScope FromRecord(object value, bool selectedOnly)
        {
            var record = value as Dictionary<string, object>;
            if (record == null || !record.ContainsKey("scope_version") || Convert.ToInt32(record["scope_version"]) != 1)
                throw new InvalidDataException("The project has no verifiable observation scope. Select the source layer and save the project again.");
            if (!record.ContainsKey("selected_only") || Convert.ToBoolean(record["selected_only"]) != selectedOnly)
                throw new InvalidDataException("The observation selection settings of the project are inconsistent.");
            var enumerable = record.ContainsKey("selected_ids") ? record["selected_ids"] as IEnumerable : null;
            if (enumerable == null || record["selected_ids"] is string)
                throw new InvalidDataException("No selected observation IDs found in the project.");
            var ids = enumerable.Cast<object>().Select(v => Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture)).ToArray();
            if (ids.Any(String.IsNullOrWhiteSpace) || ids.Distinct(StringComparer.Ordinal).Count() != ids.Length || (selectedOnly && ids.Length == 0) || (!selectedOnly && ids.Length != 0))
                throw new InvalidDataException("The selected observation IDs in the project are invalid.");
            return new PointScope { DefinitionExpression = Json.Text(record, "definition_expression"), SelectedOnly = selectedOnly, SelectedIds = ids.OrderBy(v => v, StringComparer.Ordinal).ToArray() };
        }
        internal bool EquivalentTo(PointScope other)
        {
            return other != null && SelectedOnly == other.SelectedOnly && String.Equals(DefinitionExpression, other.DefinitionExpression, StringComparison.Ordinal)
                && SelectedIds.OrderBy(v => v, StringComparer.Ordinal).SequenceEqual(other.SelectedIds.OrderBy(v => v, StringComparer.Ordinal), StringComparer.Ordinal);
        }
    }
}
