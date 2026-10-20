using System;
using System.Collections;
using System.Collections.Generic;
using System.Data;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Windows.Forms;
using Microsoft.VisualBasic.FileIO;

namespace SpatialPredictionStudio
{
    internal sealed class CandidateEditor : UserControl
    {
        private readonly CheckedListBox list = new CheckedListBox { Dock = DockStyle.Top, Height = 155, CheckOnClick = true };
        private readonly ParameterEditor parameters = new ParameterEditor();
        private readonly Label reason = new Label { AutoSize = true, Dock = DockStyle.Top };
        private readonly Dictionary<string, Dictionary<string, object>> stored = new Dictionary<string, Dictionary<string, object>>();
        private MethodSpec editing;
        private bool restoring;
        internal event EventHandler ValueChanged;
        internal CandidateEditor(IEnumerable<MethodSpec> methods)
        {
            Dock = DockStyle.Top; AutoSize = true;
            var layout = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1 };
            layout.Controls.Add(new Label { Text = "İsteğe bağlı aday aileler. Seçilirse aile seçimi iç katlarda yapılır. Bir satıra tıklayarak parametrelerini düzenleyin.", AutoSize = true, MaximumSize = new Size(850, 0) });
            layout.Controls.Add(list); layout.Controls.Add(reason); layout.Controls.Add(parameters); Controls.Add(layout);
            foreach (var method in methods) list.Items.Add(method);
            list.ItemCheck += (s, e) => { var spec = (MethodSpec)list.Items[e.Index]; if (!spec.Enabled) { e.NewValue = CheckState.Unchecked; reason.Text = spec.Reason; } else if (!restoring) BeginInvoke(new Action(() => ValueChanged?.Invoke(this, EventArgs.Empty))); };
            list.SelectedIndexChanged += (s, e) => {
                if (editing != null) stored[editing.Id] = parameters.Values();
                editing = list.SelectedItem as MethodSpec;
                if (editing == null) return;
                reason.Text = editing.Enabled ? editing.Description : editing.Reason;
                parameters.Build(editing.Parameters); parameters.Enabled = editing.Enabled;
                Dictionary<string, object> saved; if (stored.TryGetValue(editing.Id, out saved)) parameters.SetValues(saved);
            };
            parameters.ValueChanged += (s, e) => { if (!restoring) ValueChanged?.Invoke(this, EventArgs.Empty); };
        }
        internal List<Dictionary<string, object>> Values()
        {
            if (editing != null) stored[editing.Id] = parameters.Values();
            return list.CheckedItems.Cast<MethodSpec>().Select(m => new Dictionary<string, object> { { "id", m.Id }, { "parameters", ParametersFor(m) } }).ToList();
        }
        private Dictionary<string, object> ParametersFor(MethodSpec method)
        {
            Dictionary<string, object> value; if (stored.TryGetValue(method.Id, out value)) return value;
            return method.Parameters.ToDictionary(p => p.Key, p => ((Dictionary<string, object>)p.Value).ContainsKey("default") ? ((Dictionary<string, object>)p.Value)["default"] : null);
        }
        internal void Restore(IEnumerable values)
        {
            restoring = true;
            try {
                editing = null; stored.Clear(); for (int i = 0; i < list.Items.Count; ++i) list.SetItemChecked(i, false);
                foreach (Dictionary<string, object> value in values)
                {
                    string id = Json.Text(value, "id"); var method = list.Items.Cast<MethodSpec>().SingleOrDefault(m => m.Id == id);
                    if (method == null || !method.Enabled) throw new InvalidDataException("Kayıtlı aday model kullanılamıyor: " + id);
                    using (var check = new ParameterEditor()) { check.Build(method.Parameters); check.SetValues((Dictionary<string, object>)value["parameters"]); stored[id] = check.Values(); }
                    list.SetItemChecked(list.Items.IndexOf(method), true);
                }
                list.SelectedIndex = -1;
            } finally { restoring = false; }
        }
    }

    internal sealed class SearchSpaceEditor : UserControl
    {
        private readonly DataGridView grid = new DataGridView { Dock = DockStyle.Fill, AllowUserToAddRows = false, RowHeadersVisible = false, Height = 210, BackgroundColor = Color.White };
        private readonly Dictionary<string, string> valueTypes = new Dictionary<string, string>();
        internal event EventHandler ValueChanged;
        internal SearchSpaceEditor()
        {
            Dock = DockStyle.Top; Height = 250;
            var note = new Label { Dock = DockStyle.Top, Height = 35, Text = "Arama alanları: kullan seçeneğini işaretleyin. Kategorik değerleri noktalı virgülle ayırın. Sayılarda ondalık nokta kullanın." };
            grid.Columns.Add(new DataGridViewCheckBoxColumn { Name = "use", HeaderText = "Kullan", Width = 45 });
            grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "parameter", HeaderText = "Parametre", Width = 190, ReadOnly = true });
            grid.Columns.Add(new DataGridViewComboBoxColumn { Name = "type", HeaderText = "Aralık türü", Width = 95, DataSource = new[] { "categorical", "int", "float" } });
            grid.Columns.Add("low", "Alt sınır"); grid.Columns.Add("high", "Üst sınır"); grid.Columns.Add("choices", "Değerler (;)");
            grid.Columns.Add(new DataGridViewCheckBoxColumn { Name = "log", HeaderText = "Logaritmik", Width = 75 });
            grid.CurrentCellDirtyStateChanged += (s, e) => { if (grid.IsCurrentCellDirty) grid.CommitEdit(DataGridViewDataErrorContexts.Commit); };
            grid.CellValueChanged += (s, e) => ValueChanged?.Invoke(this, EventArgs.Empty);
            Controls.Add(grid); Controls.Add(note);
        }
        internal void SetParameters(Dictionary<string, Dictionary<string, object>> parameters)
        {
            var previous = Values(false); grid.Rows.Clear(); valueTypes.Clear();
            foreach (var pair in parameters)
            {
                string type = Json.Text(pair.Value, "type"); if (!(new[] { "int", "float", "bool", "enum" }).Contains(type)) continue;
                valueTypes[pair.Key] = type;
                object defaultValue; pair.Value.TryGetValue("default", out defaultValue);
                string choices = Convert.ToString(defaultValue, CultureInfo.InvariantCulture);
                if (pair.Value.ContainsKey("choices")) choices = String.Join(";", ((IEnumerable)pair.Value["choices"]).Cast<object>().Select(v => v is Dictionary<string, object> ? Json.Text((Dictionary<string, object>)v, "id") : Convert.ToString(v, CultureInfo.InvariantCulture)));
                if (type == "bool") choices = "true;false";
                grid.Rows.Add(false, pair.Key, type == "int" || type == "float" ? type : "categorical", pair.Value.ContainsKey("min") ? pair.Value["min"] : defaultValue, defaultValue, choices, false);
            }
            Restore(previous, false);
        }
        internal Dictionary<string, object> Values(bool strict = true)
        {
            grid.EndEdit(); var result = new Dictionary<string, object>();
            foreach (DataGridViewRow row in grid.Rows)
            {
                if (!Convert.ToBoolean(row.Cells["use"].Value ?? false)) continue;
                string name = Convert.ToString(row.Cells["parameter"].Value), type = Convert.ToString(row.Cells["type"].Value);
                try {
                    var spec = new Dictionary<string, object> { { "type", type } };
                    if (type == "categorical")
                    {
                        string[] choices = Convert.ToString(row.Cells["choices"].Value).Split(';').Select(s => s.Trim()).Where(s => s.Length > 0).ToArray();
                        if (choices.Length == 0) throw new InvalidDataException("En az bir aday değer gerekir: " + name);
                        spec["choices"] = choices.Select(v => Parse(v, valueTypes[name])).ToArray();
                    }
                    else
                    {
                        double low = Double.Parse(Convert.ToString(row.Cells["low"].Value, CultureInfo.InvariantCulture), NumberStyles.Float, CultureInfo.InvariantCulture), high = Double.Parse(Convert.ToString(row.Cells["high"].Value, CultureInfo.InvariantCulture), NumberStyles.Float, CultureInfo.InvariantCulture);
                        bool logarithmic = Convert.ToBoolean(row.Cells["log"].Value ?? false);
                        if (low > high || (logarithmic && low <= 0) || (type == "int" && (low != Math.Truncate(low) || high != Math.Truncate(high)))) throw new InvalidDataException("Geçersiz arama sınırları: " + name);
                        spec["low"] = type == "int" ? (object)Convert.ToInt32(low) : low; spec["high"] = type == "int" ? (object)Convert.ToInt32(high) : high; spec["log"] = logarithmic;
                    }
                    result[name] = spec;
                } catch (Exception) { if (strict) throw; }
            }
            return result;
        }
        private static object Parse(string text, string type)
        {
            if (type == "int") return Int32.Parse(text, CultureInfo.InvariantCulture);
            if (type == "float") return Double.Parse(text, NumberStyles.Float, CultureInfo.InvariantCulture);
            if (type == "bool") return Boolean.Parse(text);
            return text;
        }
        internal void Restore(Dictionary<string, object> values, bool strict = true)
        {
            foreach (var pair in values)
            {
                var row = grid.Rows.Cast<DataGridViewRow>().FirstOrDefault(r => Convert.ToString(r.Cells["parameter"].Value) == pair.Key);
                if (row == null) { if (strict) throw new InvalidDataException("Arama parametresi seçilen modelde yok: " + pair.Key); continue; }
                var spec = (Dictionary<string, object>)pair.Value; row.Cells["use"].Value = true; row.Cells["type"].Value = Json.Text(spec, "type");
                foreach (string key in new[] { "low", "high", "log" }) if (spec.ContainsKey(key)) row.Cells[key].Value = spec[key];
                if (spec.ContainsKey("choices")) row.Cells["choices"].Value = String.Join(";", ((IEnumerable)spec["choices"]).Cast<object>().Select(v => Convert.ToString(v, CultureInfo.InvariantCulture)));
            }
        }
    }

    internal sealed class CustomFoldsEditor : UserControl
    {
        private readonly DataGridView grid = new DataGridView { Dock = DockStyle.Fill, RowHeadersVisible = false, BackgroundColor = Color.White };
        internal event EventHandler ValueChanged;
        internal CustomFoldsEditor()
        {
            Dock = DockStyle.Top; Height = 250;
            grid.Columns.Add("repeat", "Tekrar (0 tabanlı)"); grid.Columns.Add("fold", "Kat (0 tabanlı)"); grid.Columns.Add("sample_id", "Gözlem kimliği");
            grid.Columns.Add(new DataGridViewComboBoxColumn { Name = "role", HeaderText = "Rol", DataSource = new[] { "train", "test" } });
            var top = new FlowLayoutPanel { Dock = DockStyle.Top, Height = 38 };
            var load = new Button { Text = "Kat tablosunu içe aktar", AutoSize = true };
            load.Click += (s, e) => {
                try { using (var dialog = new OpenFileDialog { Filter = "Virgülle ayrılmış tablo (*.csv)|*.csv" }) if (dialog.ShowDialog(this) == DialogResult.OK) { var table = TabularFiles.Read(dialog.FileName); foreach (string name in new[] { "repeat", "fold", "sample_id", "role" }) if (!table.Columns.Contains(name)) throw new InvalidDataException("Tablo sütunu eksik: " + name); grid.Rows.Clear(); foreach (DataRow row in table.Rows) grid.Rows.Add(row["repeat"], row["fold"], row["sample_id"], row["role"]); } }
                catch (Exception ex) { MessageBox.Show(this, ex.Message, "Kat tablosu", MessageBoxButtons.OK, MessageBoxIcon.Error); }
            };
            top.Controls.Add(load); top.Controls.Add(new Label { Text = "Her gözlem ve rol için ayrı satır", AutoSize = true, Padding = new Padding(0, 7, 0, 0) });
            grid.CellValueChanged += (s, e) => ValueChanged?.Invoke(this, EventArgs.Empty); grid.RowsRemoved += (s, e) => ValueChanged?.Invoke(this, EventArgs.Empty);
            Controls.Add(grid); Controls.Add(top);
        }
        internal List<Dictionary<string, object>> Values()
        {
            grid.EndEdit(); var groups = new Dictionary<string, Dictionary<string, object>>();
            foreach (DataGridViewRow row in grid.Rows)
            {
                if (row.IsNewRow) continue;
                int repeat = Int32.Parse(Convert.ToString(row.Cells["repeat"].Value), CultureInfo.InvariantCulture), fold = Int32.Parse(Convert.ToString(row.Cells["fold"].Value), CultureInfo.InvariantCulture);
                string id = Convert.ToString(row.Cells["sample_id"].Value).Trim(), role = Convert.ToString(row.Cells["role"].Value);
                if (repeat < 0 || fold < 0 || id.Length == 0 || (role != "train" && role != "test")) throw new InvalidDataException("Özel kat satırında tekrar, kat, gözlem kimliği veya rol geçersiz.");
                string key = repeat + ":" + fold; Dictionary<string, object> entry;
                if (!groups.TryGetValue(key, out entry)) groups[key] = entry = new Dictionary<string, object> { { "repeat", repeat }, { "fold", fold }, { "train_ids", new List<string>() }, { "test_ids", new List<string>() } };
                var ids = (List<string>)entry[role + "_ids"]; if (ids.Contains(id)) throw new InvalidDataException("Özel kat içinde yinelenen gözlem: " + id); ids.Add(id);
            }
            foreach (var group in groups.Values) if (((List<string>)group["train_ids"]).Count == 0 || ((List<string>)group["test_ids"]).Count == 0 || ((List<string>)group["train_ids"]).Intersect((List<string>)group["test_ids"]).Any()) throw new InvalidDataException("Her özel katta eğitim ve sınama kimlikleri dolu ve ayrık olmalıdır.");
            return groups.Values.OrderBy(v => (int)v["repeat"]).ThenBy(v => (int)v["fold"]).ToList();
        }
        internal void Restore(IEnumerable values)
        {
            grid.Rows.Clear(); foreach (Dictionary<string, object> fold in values) foreach (string role in new[] { "train", "test" }) foreach (object id in (IEnumerable)fold[role + "_ids"]) grid.Rows.Add(fold["repeat"], fold["fold"], id, role);
        }
    }
    internal static class TabularFiles
    {
        internal static DataTable Read(string path)
        {
            var table = new DataTable();
            using (var parser = new TextFieldParser(path, System.Text.Encoding.UTF8))
            {
                parser.TextFieldType = FieldType.Delimited; parser.SetDelimiters(","); parser.HasFieldsEnclosedInQuotes = true;
                if (parser.EndOfData) throw new InvalidDataException("Tablo boş.");
                foreach (string name in parser.ReadFields()) table.Columns.Add(name);
                int count = 0; while (!parser.EndOfData) { if (++count > 1000000) throw new InvalidDataException("Tablo görünümü bir milyon satırla sınırlıdır."); table.Rows.Add(parser.ReadFields()); }
            }
            return table;
        }
    }
}
