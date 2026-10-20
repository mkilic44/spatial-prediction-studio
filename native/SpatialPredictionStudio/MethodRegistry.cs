using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;

namespace SpatialPredictionStudio
{
    internal sealed class MethodSpec
    {
        internal string Id, Label, Description, Reason;
        internal bool Enabled;
        internal Dictionary<string, object> Parameters;
        public override string ToString() { return Label + (Enabled ? "" : " (kapalı)"); }
    }
    internal sealed class MethodRegistry
    {
        internal readonly Dictionary<string, List<MethodSpec>> Categories = new Dictionary<string, List<MethodSpec>>(StringComparer.Ordinal);
        internal string Digest;
        internal static MethodRegistry Load(string path)
        {
            if (!File.Exists(path)) throw new FileNotFoundException("Yöntem kaydı bulunamadı. Paket yapılandırmasını kontrol edin.", path);
            var root = Json.Read(path);
            var categories = root.ContainsKey("categories") ? root["categories"] as Dictionary<string, object> : null;
            if (categories == null) throw new InvalidDataException("Yöntem kaydında categories nesnesi yok.");
            var registry = new MethodRegistry { Digest = ConfigurationIdentity.FileDigest(path) };
            foreach (var category in categories)
            {
                var methods = new List<MethodSpec>(); var ids = new HashSet<string>(StringComparer.Ordinal);
                foreach (object value in (IEnumerable)category.Value)
                {
                    var item = value as Dictionary<string, object>;
                    if (item == null) throw new InvalidDataException("Yöntem kaydı nesne olmalıdır.");
                    string id = Json.Text(item, "id");
                    if (String.IsNullOrWhiteSpace(id) || !ids.Add(id)) throw new InvalidDataException("Geçersiz veya yinelenen yöntem kimliği: " + category.Key);
                    methods.Add(new MethodSpec { Id = id, Label = Json.Text(item, "label", id), Description = Json.Text(item, "description"), Reason = Json.Text(item, "reason"), Enabled = item.ContainsKey("enabled") && Convert.ToBoolean(item["enabled"]), Parameters = item.ContainsKey("parameters") ? item["parameters"] as Dictionary<string, object> ?? new Dictionary<string, object>() : new Dictionary<string, object>() });
                }
                registry.Categories.Add(category.Key, methods);
            }
            return registry;
        }
        internal List<MethodSpec> Category(string name)
        {
            List<MethodSpec> methods;
            if (!Categories.TryGetValue(name, out methods)) throw new InvalidDataException("Yöntem kaydında kategori eksik: " + name);
            return methods;
        }
    }
    internal sealed class Choice
    {
        internal string Id, Label;
        internal Choice(string id, string label = null) { Id = id; Label = label ?? id; }
        public override string ToString() { return Label; }
    }
    internal static class ConfigurationIdentity
    {
        internal static string FileDigest(string path)
        {
            using (var input = File.OpenRead(path)) using (var hash = SHA256.Create()) return BitConverter.ToString(hash.ComputeHash(input)).Replace("-", "").ToLowerInvariant();
        }
        internal static string Digest(object value)
        {
            var normalized = Json.Serializer().DeserializeObject(Json.Serializer().Serialize(value));
            using (var hash = SHA256.Create()) return BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(Json.Serializer().Serialize(Canonical(normalized))))).Replace("-", "").ToLowerInvariant();
        }
        private static object Canonical(object value)
        {
            var dictionary = value as IDictionary<string, object>;
            if (dictionary != null) return new SortedDictionary<string, object>(dictionary.ToDictionary(p => p.Key, p => Canonical(p.Value)), StringComparer.Ordinal);
            if (value is IEnumerable && !(value is string)) return ((IEnumerable)value).Cast<object>().Select(Canonical).ToArray();
            return value;
        }
        internal static string SourceStamp(IEnumerable<string> paths)
        {
            var members = new SortedSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var original in paths.Where(p => !String.IsNullOrWhiteSpace(p)))
            {
                string path = Path.GetFullPath(original);
                if (Path.GetExtension(path).Equals(".shp", StringComparison.OrdinalIgnoreCase))
                    foreach (string member in Directory.GetFiles(Path.GetDirectoryName(path), Path.GetFileNameWithoutExtension(path) + ".*").Where(p => !p.EndsWith(".lock", StringComparison.OrdinalIgnoreCase))) members.Add(member);
                else if (path.IndexOf(".gdb", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    string directory = path.Substring(0, path.IndexOf(".gdb", StringComparison.OrdinalIgnoreCase) + 4);
                    foreach (string member in Directory.GetFiles(directory).Where(p => !p.EndsWith(".lock", StringComparison.OrdinalIgnoreCase))) members.Add(member);
                }
                else
                {
                    members.Add(path);
                    foreach (string suffix in new[] { ".aux.xml", ".msk", ".ovr" }) if (File.Exists(path + suffix)) members.Add(path + suffix);
                }
            }
            return Digest(members.Select(path => { var info = new FileInfo(path); return new { path, exists = info.Exists, size = info.Exists ? info.Length : -1, ticks = info.Exists ? info.LastWriteTimeUtc.Ticks : -1 }; }).ToArray());
        }
    }
}
