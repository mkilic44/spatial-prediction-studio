using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Windows.Forms;

namespace SpatialPredictionStudio
{
    internal sealed class MultiChoiceControl : UserControl
    {
        private readonly TextBox summary = new TextBox { ReadOnly = true, Dock = DockStyle.Fill };
        private string[] values = new string[0];
        internal Func<IEnumerable<Choice>> Options = () => new Choice[0];
        internal event EventHandler ValueChanged;
        internal string[] Values { get { return values.ToArray(); } set { values = value ?? new string[0]; summary.Text = String.Join(", ", values); ValueChanged?.Invoke(this, EventArgs.Empty); } }
        internal MultiChoiceControl()
        {
            Height = 27; Dock = DockStyle.Fill;
            var button = new Button { Text = "Select", Dock = DockStyle.Right, Width = 50 };
            Controls.Add(summary); Controls.Add(button);
            button.Click += (s, e) => {
                using (var dialog = new Form { Text = "Selection and priority order", Width = 540, Height = 520, StartPosition = FormStartPosition.CenterParent, MinimizeBox = false, MaximizeBox = false })
                {
                    var options = Options().ToList();
                    foreach (var id in values) if (!options.Any(o => o.Id == id)) options.Add(new Choice(id, id + " (not in source)"));
                    var order = values.Select(id => options.First(o => o.Id == id)).Concat(options.Where(o => !values.Contains(o.Id))).ToList();
                    var list = new CheckedListBox { Dock = DockStyle.Fill, CheckOnClick = true };
                    foreach (var item in order) list.Items.Add(item, values.Contains(item.Id));
                    var buttons = new FlowLayoutPanel { Dock = DockStyle.Bottom, Height = 40 };
                    var up = new Button { Text = "Up", AutoSize = true }; var down = new Button { Text = "Down", AutoSize = true }; var ok = new Button { Text = "Apply", DialogResult = DialogResult.OK, AutoSize = true };
                    Action<int> move = offset => { int index = list.SelectedIndex, next = index + offset; if (index < 0 || next < 0 || next >= list.Items.Count) return; object item = list.Items[index]; bool check = list.GetItemChecked(index); list.Items.RemoveAt(index); list.Items.Insert(next, item); list.SetItemChecked(next, check); list.SelectedIndex = next; };
                    up.Click += (a, b) => move(-1); down.Click += (a, b) => move(1); buttons.Controls.AddRange(new Control[] { up, down, ok });
                    dialog.Controls.Add(list); dialog.Controls.Add(buttons); dialog.AcceptButton = ok;
                    if (dialog.ShowDialog(this) == DialogResult.OK) Values = list.CheckedItems.Cast<Choice>().Select(c => c.Id).ToArray();
                }
            };
        }
    }

    internal sealed class ParameterEditor : UserControl
    {
        private readonly Dictionary<string, Control> controls = new Dictionary<string, Control>(StringComparer.Ordinal);
        private Dictionary<string, object> specs = new Dictionary<string, object>();
        private readonly TableLayoutPanel grid = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 2 };
        internal Func<string, IEnumerable<Choice>> ChoiceProvider = name => new Choice[0];
        internal event EventHandler ValueChanged;
        internal ParameterEditor()
        {
            AutoSize = true; Dock = DockStyle.Top; grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 42)); grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 58)); Controls.Add(grid);
        }
        internal void Build(Dictionary<string, object> definitions)
        {
            specs = definitions; controls.Clear(); grid.Controls.Clear(); grid.RowStyles.Clear(); int row = 0;
            foreach (var pair in definitions)
            {
                var spec = pair.Value as Dictionary<string, object>;
                if (spec == null) throw new InvalidDataException("A parameter definition must be an object: " + pair.Key);
                string type = Json.Text(spec, "type", "string"); object defaultValue; spec.TryGetValue("default", out defaultValue);
                Control control;
                if (type == "bool" || type == "boolean") control = new CheckBox { AutoSize = true, Checked = defaultValue != null && Convert.ToBoolean(defaultValue) };
                else if (type == "int" || type == "integer" || type == "float" || type == "number")
                {
                    decimal min = spec.ContainsKey("min") ? Convert.ToDecimal(spec["min"], CultureInfo.InvariantCulture) : -1000000000;
                    decimal max = spec.ContainsKey("max") ? Convert.ToDecimal(spec["max"], CultureInfo.InvariantCulture) : 1000000000;
                    var number = new NumericUpDown { Dock = DockStyle.Fill, DecimalPlaces = type == "int" || type == "integer" ? 0 : 6, Minimum = min, Maximum = max, Increment = type == "int" || type == "integer" ? 1 : 0.01M };
                    number.Value = defaultValue == null ? Math.Max(min, 0) : Math.Min(max, Math.Max(min, Convert.ToDecimal(defaultValue, CultureInfo.InvariantCulture))); control = number;
                }
                else if (type == "enum" || type == "field")
                {
                    var combo = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Dock = DockStyle.Fill };
                    if (spec.ContainsKey("choices")) foreach (var choice in (IEnumerable)spec["choices"]) combo.Items.Add(ParseChoice(choice));
                    else foreach (var choice in ChoiceProvider(pair.Key)) combo.Items.Add(choice);
                    if (combo.Items.Count > 0) combo.SelectedIndex = 0;
                    control = combo;
                }
                else if (type == "fields" || type == "ids") control = new MultiChoiceControl { Options = () => ChoiceProvider(pair.Key) };
                else if (type == "string") control = new TextBox { Dock = DockStyle.Fill, Text = Convert.ToString(defaultValue, CultureInfo.InvariantCulture) ?? "" };
                else throw new InvalidDataException("Unsupported interface parameter type: " + type + " (" + pair.Key + ")");
                controls.Add(pair.Key, control);
                grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));
                grid.Controls.Add(new Label { Text = Json.Text(spec, "label", pair.Key), Dock = DockStyle.Fill, AutoSize = true, Padding = new Padding(2, 6, 2, 2) }, 0, row);
                grid.Controls.Add(control, 1, row++);
                if (defaultValue != null) Set(pair.Key, defaultValue, false);
                if (control is NumericUpDown) ((NumericUpDown)control).ValueChanged += Changed;
                else if (control is CheckBox) ((CheckBox)control).CheckedChanged += Changed;
                else if (control is ComboBox) ((ComboBox)control).SelectedIndexChanged += Changed;
                else if (control is MultiChoiceControl) ((MultiChoiceControl)control).ValueChanged += Changed;
                else control.TextChanged += Changed;
            }
        }
        private void Changed(object sender, EventArgs args) { ValueChanged?.Invoke(this, EventArgs.Empty); }
        private static Choice ParseChoice(object raw)
        {
            var item = raw as Dictionary<string, object>;
            if (item != null) return new Choice(Json.Text(item, "id", Json.Text(item, "value")), Json.Text(item, "label", Json.Text(item, "id", Json.Text(item, "value"))));
            return new Choice(Convert.ToString(raw, CultureInfo.InvariantCulture));
        }
        internal Dictionary<string, object> Values()
        {
            var value = new Dictionary<string, object>();
            foreach (var item in controls)
            {
                var spec = (Dictionary<string, object>)specs[item.Key]; string type = Json.Text(spec, "type", "string");
                object v;
                if (item.Value is NumericUpDown) v = type == "int" || type == "integer" ? (object)Decimal.ToInt32(((NumericUpDown)item.Value).Value) : (double)((NumericUpDown)item.Value).Value;
                else if (item.Value is CheckBox) v = ((CheckBox)item.Value).Checked;
                else if (item.Value is ComboBox) v = (((ComboBox)item.Value).SelectedItem as Choice)?.Id ?? "";
                else if (item.Value is MultiChoiceControl) v = ((MultiChoiceControl)item.Value).Values;
                else v = item.Value.Text.Trim();
                if (spec.ContainsKey("required") && Convert.ToBoolean(spec["required"]) && String.IsNullOrWhiteSpace(Convert.ToString(v, CultureInfo.InvariantCulture))) throw new InvalidDataException(Json.Text(spec, "label", item.Key) + " is required.");
                value.Add(item.Key, v);
            }
            return value;
        }
        internal void SetValues(Dictionary<string, object> values)
        {
            foreach (var item in values) Set(item.Key, item.Value, true);
        }
        internal void Set(string key, object value, bool strict = true)
        {
            Control control;
            if (!controls.TryGetValue(key, out control)) { if (strict) throw new InvalidDataException("Saved method parameter is no longer supported: " + key); return; }
            if (control is NumericUpDown)
            {
                var number = (NumericUpDown)control; decimal numeric = Convert.ToDecimal(value, CultureInfo.InvariantCulture);
                if (numeric < number.Minimum || numeric > number.Maximum) throw new InvalidDataException("Parameter outside the allowed range: " + key);
                number.Value = numeric;
            }
            else if (control is CheckBox) ((CheckBox)control).Checked = Convert.ToBoolean(value);
            else if (control is ComboBox)
            {
                var combo = (ComboBox)control; string id = Convert.ToString(value, CultureInfo.InvariantCulture);
                var chosen = combo.Items.Cast<Choice>().FirstOrDefault(c => c.Id == id);
                if (chosen == null) { if (strict) throw new InvalidDataException("Unsupported parameter choice: " + key + "=" + id); return; }
                combo.SelectedItem = chosen;
            }
            else if (control is MultiChoiceControl) ((MultiChoiceControl)control).Values = value is string ? new[] { (string)value } : ((IEnumerable)value).Cast<object>().Select(v => Convert.ToString(v, CultureInfo.InvariantCulture)).ToArray();
            else control.Text = Convert.ToString(value, CultureInfo.InvariantCulture) ?? "";
        }
    }

    internal sealed class RegistryEditor : UserControl
    {
        private readonly ComboBox methods = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Dock = DockStyle.Top, DrawMode = DrawMode.OwnerDrawFixed };
        private readonly Label description = new Label { Dock = DockStyle.Top, AutoSize = true, Padding = new Padding(3), MaximumSize = new Size(850, 0) };
        internal readonly ParameterEditor Parameters = new ParameterEditor();
        private MethodSpec selected;
        internal event EventHandler ValueChanged;
        internal string MethodId { get { return selected?.Id ?? ""; } }
        internal MethodSpec SelectedMethod { get { return selected; } }
        internal RegistryEditor(IEnumerable<MethodSpec> entries, string defaultId, Func<string, IEnumerable<Choice>> choices)
        {
            AutoSize = true; Dock = DockStyle.Top; Parameters.ChoiceProvider = choices;
            var layout = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1 };
            // A percentage column respects the docked editor width. An implicit AutoSize
            // column expands to the description's preferred width and clips the selector.
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            layout.Controls.Add(methods); layout.Controls.Add(description); layout.Controls.Add(Parameters); Controls.Add(layout);
            foreach (var entry in entries) methods.Items.Add(entry);
            methods.DrawItem += (s, e) => { e.DrawBackground(); if (e.Index >= 0) { var method = (MethodSpec)methods.Items[e.Index]; TextRenderer.DrawText(e.Graphics, method.ToString(), e.Font, e.Bounds, method.Enabled ? e.ForeColor : Color.Gray); } e.DrawFocusRectangle(); };
            methods.SelectedIndexChanged += (s, e) => {
                var next = methods.SelectedItem as MethodSpec;
                if (next == null || next == selected) return;
                if (!next.Enabled) { description.Text = next.Label + ": " + next.Reason; methods.SelectedItem = selected; return; }
                selected = next; description.Text = next.Description; Parameters.Build(next.Parameters); ValueChanged?.Invoke(this, EventArgs.Empty);
            };
            Parameters.ValueChanged += (s, e) => ValueChanged?.Invoke(this, EventArgs.Empty);
            var initial = methods.Items.Cast<MethodSpec>().FirstOrDefault(m => m.Id == defaultId && m.Enabled) ?? methods.Items.Cast<MethodSpec>().FirstOrDefault(m => m.Enabled);
            if (initial == null) { Parameters.Enabled = false; description.Text = "No verified method in this category. Click a disabled entry to read the reason."; }
            else methods.SelectedItem = initial;
        }
        internal void Select(string id)
        {
            var method = methods.Items.Cast<MethodSpec>().FirstOrDefault(m => m.Id == id);
            if (method == null || !method.Enabled) throw new InvalidDataException("Method not available: " + id + ". " + method?.Reason);
            methods.SelectedItem = method;
        }
        internal Dictionary<string, object> Configuration(string selector = "method")
        {
            if (selected == null) throw new InvalidDataException("No enabled method found.");
            var config = Parameters.Values(); if (!String.IsNullOrEmpty(selector)) config[selector] = selected.Id; return config;
        }
        internal void Restore(Dictionary<string, object> value, string selector = "method")
        {
            if (!String.IsNullOrEmpty(selector)) Select(Json.Text(value, selector));
            Parameters.SetValues(value.Where(p => p.Key != selector).ToDictionary(p => p.Key, p => p.Value));
        }
    }
}
