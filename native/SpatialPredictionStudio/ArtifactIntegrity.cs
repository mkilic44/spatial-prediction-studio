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
            if (String.IsNullOrWhiteSpace(directory) || String.IsNullOrWhiteSpace(relativeOrAbsolute)) throw new InvalidDataException("Sonuç dosyasının yolu eksik.");
            string prefix = Path.GetFullPath(directory).TrimEnd('\\', '/') + Path.DirectorySeparatorChar;
            string path = Path.GetFullPath(Path.IsPathRooted(relativeOrAbsolute) ? relativeOrAbsolute : Path.Combine(directory, relativeOrAbsolute));
            if (!path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Sonuç dosyası iş dizininin dışında: " + path);
            if (!File.Exists(path)) throw new FileNotFoundException("Sonuç dosyası bulunamadı.", path);
            return path;
        }
        internal static void VerifySha256(string path, string expected, CancellationToken cancellation = default(CancellationToken))
        {
            if (expected == null || expected.Length != 64 || expected.Any(c => !Uri.IsHexDigit(c)))
                throw new InvalidDataException("Sonuç bildirimindeki içerik özeti eksik veya geçersiz.");
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
                throw new InvalidDataException("Sonuç dosyasının içeriği tamamlanan işin kaydıyla uyuşmuyor: " + path);
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
                throw new InvalidDataException("Projede doğrulanabilir gözlem kapsamı yok. Kaynak katmanı seçip projeyi yeniden kaydedin.");
            if (!record.ContainsKey("selected_only") || Convert.ToBoolean(record["selected_only"]) != selectedOnly)
                throw new InvalidDataException("Projenin gözlem seçimi ayarları tutarsız.");
            var enumerable = record.ContainsKey("selected_ids") ? record["selected_ids"] as IEnumerable : null;
            if (enumerable == null || record["selected_ids"] is string)
                throw new InvalidDataException("Projede seçili gözlem kimlikleri bulunamadı.");
            var ids = enumerable.Cast<object>().Select(v => Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture)).ToArray();
            if (ids.Any(String.IsNullOrWhiteSpace) || ids.Distinct(StringComparer.Ordinal).Count() != ids.Length || (selectedOnly && ids.Length == 0) || (!selectedOnly && ids.Length != 0))
                throw new InvalidDataException("Projedeki seçili gözlem kimlikleri geçersiz.");
            return new PointScope { DefinitionExpression = Json.Text(record, "definition_expression"), SelectedOnly = selectedOnly, SelectedIds = ids.OrderBy(v => v, StringComparer.Ordinal).ToArray() };
        }
        internal bool EquivalentTo(PointScope other)
        {
            return other != null && SelectedOnly == other.SelectedOnly && String.Equals(DefinitionExpression, other.DefinitionExpression, StringComparison.Ordinal)
                && SelectedIds.OrderBy(v => v, StringComparer.Ordinal).SequenceEqual(other.SelectedIds.OrderBy(v => v, StringComparer.Ordinal), StringComparer.Ordinal);
        }
    }
}
