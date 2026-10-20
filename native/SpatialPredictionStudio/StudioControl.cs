using System;
using System.Collections;
using System.Collections.Generic;
using System.Data;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using ESRI.ArcGIS.Framework;
using Button = System.Windows.Forms.Button;

namespace SpatialPredictionStudio
{
    public sealed partial class StudioControl : UserControl
    {
        private readonly ArcMapAdapter adapter;
        private readonly JobClient client = new JobClient();
        private MethodRegistry registry;
        private RegistryEditor validation, preprocessing, targetTransform, featureSelection, vif, model, optimization, uncertainty;
        private CandidateEditor candidates;
        private readonly SearchSpaceEditor spaces = new SearchSpaceEditor();
        private readonly CustomFoldsEditor customFolds = new CustomFoldsEditor();
        private readonly ComboBox points = Combo(), target = Combo(), identity = Combo(), purpose = Combo(), support = Combo(), depthTop = Combo(), depthBottom = Combo(), laboratoryMethod = Combo(), dateField = Combo(), referenceRaster = Combo(), figureDpi = Combo(), inputType = Combo(), csvX = Combo(), csvY = Combo();
        private readonly MultiChoiceControl groups = new MultiChoiceControl(), independent = new MultiChoiceControl();
        private readonly TextBox targetUnit = Input("g/kg"), outputRoot = Input(), supportNote = Input(), polygonPath = Input(), polygonId = Input(), csvPath = Input(), csvCrs = Input("EPSG:4326");
        private DataTable csvTable;
        private readonly TextBox independentPath = Input();
        private readonly TextBox independentCrs = Input();
        private readonly ComboBox independentX = Combo(), independentY = Combo();
        private readonly ComboBox unitField = Combo();
        private readonly NumericUpDown maxMissing = Number(0, 1, 0.5M, 3);
        private readonly CheckBox applicability = Check("Compute the model's area of applicability (AOA)"), permutation = Check("Compute permutation variable importance");
        private readonly NumericUpDown permutationRepeats = Number(2, 100, 5), minSubgroupSamples = Number(3, 1000, 10), minSubgroupGroups = Number(2, 1000, 3);
        private readonly MultiChoiceControl subgroupFields = new MultiChoiceControl();
        private readonly CheckBox selected = Check("Only points selected on the map"), align = Check("Align rasters to the reference grid"), mask = Check("Mask predictions outside the training range"), physicalMinUse = Check("Physical lower bound"), physicalMaxUse = Check("Physical upper bound");
        private readonly NumericUpDown physicalMin = Number(-1000000000, 1000000000, 0, 4), physicalMax = Number(-1000000000, 1000000000, 100, 4), windowSize = Number(16, 4096, 256);
        private readonly DataGridView rasters = new DataGridView { Dock = DockStyle.Fill, AllowUserToAddRows = false, AllowUserToDeleteRows = false, RowHeadersVisible = false, SelectionMode = DataGridViewSelectionMode.FullRowSelect, Height = 240, BackgroundColor = Color.White };
        private readonly TextBox covariateMetadata = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill, WordWrap = true, Height = 145 };
        private readonly TextBox sampleExclusions = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill, WordWrap = true, Height = 80, Text = "No saved exclusions." };
        private readonly TextBox log = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Both, Dock = DockStyle.Fill, WordWrap = false, Font = new Font("Consolas", 9) };
        private readonly Label state = Note("A new fold preview is required."), previewState = Note("No fold preview has been created yet."), resultState = Note("No completed model yet.");
        private readonly ProgressBar progress = new ProgressBar { Dock = DockStyle.Fill, Height = 18 };
        private readonly Button run = ActionButton("Run model", false), cancel = ActionButton("Cancel", false), preview = ActionButton("Show folds"), preflight = ActionButton("Preflight check");
        private readonly Button saveModel = ActionButton("Save model", false);
        private readonly Label modelExportState = Note("Saving a model requires a completed run.");
        private readonly ComboBox outputTables = Combo(), outputReports = Combo();
        private readonly DataGridView results = new DataGridView { Dock = DockStyle.Fill, ReadOnly = true, AllowUserToAddRows = false, BackgroundColor = Color.White, RowHeadersVisible = false, AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.DisplayedCells };
        private readonly List<Control> guarded = new List<Control>();
        private readonly List<Form> ownedViewers = new List<Form>();
        private readonly System.Windows.Forms.Timer sourceTimer = new System.Windows.Forms.Timer { Interval = 2000 };
        private readonly string localRoot, logPath;
        private string lastManifest, projectPath, previewFingerprint, previewSignature, previewSources, previewManifest;
        private bool disposed, loading = true, busy, currentResult;
        private TabControl tabs;
        private TabPage reportPage, evaluationPage;
        private bool IsCsv { get { return ChoiceId(inputType) == "csv"; } }

        public StudioControl(IApplication application)
        {
            Dock = DockStyle.Fill; Font = new Font("Segoe UI", 9);
            localRoot = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SpatialPredictionStudio");
            Directory.CreateDirectory(Path.Combine(localRoot, "logs"));
            logPath = Path.Combine(localRoot, "logs", "arcmap_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + "_" + System.Diagnostics.Process.GetCurrentProcess().Id + ".log");
            outputRoot.Text = Path.Combine(localRoot, "runs"); adapter = new ArcMapAdapter(application);
            try { registry = MethodRegistry.Load(RuntimeSettings.Load().RegistryPath); BuildUi(); }
            catch (Exception ex) { Controls.Add(Note("Could not load the panel configuration: " + ex.Message)); Log(ex.ToString()); return; }
            loading = false;
            Load += (s, e) => { Log("Spatial prediction panel opened. ArcMap process ID: " + System.Diagnostics.Process.GetCurrentProcess().Id + "; main thread: " + Thread.CurrentThread.ManagedThreadId); RefreshMap(); sourceTimer.Start(); UpdateMcpBridge(); };
            sourceTimer.Tick += (s, e) => {
                if (busy || String.IsNullOrEmpty(previewFingerprint)) return;
                try { if (CurrentSources() != previewSources) InvalidatePreview("Source data, query or selected observations changed."); }
                catch (Exception ex) { InvalidatePreview("Could not recheck the source: " + ex.Message); }
            };
        }
        // Contract harness constructs only managed controls. It never supplies an ArcObjects connection.
        internal StudioControl(MethodRegistry methods, string contractDirectory)
        {
            Dock = DockStyle.Fill; Font = new Font("Segoe UI", 9); localRoot = contractDirectory; Directory.CreateDirectory(localRoot);
            logPath = Path.Combine(localRoot, "managed_panel.log"); outputRoot.Text = Path.Combine(localRoot, "runs"); registry = methods; adapter = null;
            BuildUi(); loading = false;
        }
        private static ComboBox Combo() { return new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Dock = DockStyle.Fill }; }
        private static TextBox Input(string value = "") { return new TextBox { Text = value, Dock = DockStyle.Fill }; }
        private static CheckBox Check(string caption) { return new CheckBox { Text = caption, AutoSize = true }; }
        private static Button ActionButton(string caption, bool enabled = true) { return new Button { Text = caption, AutoSize = true, Enabled = enabled }; }
        private static NumericUpDown Number(decimal min, decimal max, decimal value, int decimals = 0) { return new NumericUpDown { Minimum = min, Maximum = max, Value = value, DecimalPlaces = decimals, Dock = DockStyle.Fill }; }
        private static Label Note(string value) { return new Label { Text = value, AutoSize = true, Dock = DockStyle.Top, MaximumSize = new Size(950, 0), Padding = new Padding(5) }; }
        private TableLayoutPanel Page(string title)
        {
            var page = new TabPage(title) { AutoScroll = true }; tabs.TabPages.Add(page);
            var table = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 2, Padding = new Padding(6) };
            table.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 36)); table.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 64)); page.Controls.Add(table); return table;
        }
        private static void Row(TableLayoutPanel table, string label, Control control)
        {
            int row = table.RowCount++; table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(new Label { Text = label, AutoSize = true, Dock = DockStyle.Fill, Padding = new Padding(1, 6, 1, 4) }, 0, row); table.Controls.Add(control, 1, row);
        }
        private static void Wide(TableLayoutPanel table, Control control) { int row = table.RowCount++; table.RowStyles.Add(new RowStyle(SizeType.AutoSize)); table.Controls.Add(control, 0, row); table.SetColumnSpan(control, 2); }
        private void AddButton(FlowLayoutPanel panel, string caption, Action action, bool guard = true)
        {
            var button = ActionButton(caption); button.Click += (s, e) => Safe(action); panel.Controls.Add(button); if (guard) guarded.Add(button);
        }
        private RegistryEditor Editor(string category, string initial)
        {
            var editor = new RegistryEditor(registry.Category(category), initial, name => CovariateChoices()); editor.ValueChanged += ScientificChange; guarded.Add(editor); return editor;
        }
        private void BuildUi()
        {
            var shell = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, Padding = new Padding(4) };
            shell.RowStyles.Add(new RowStyle(SizeType.Percent, 100)); shell.RowStyles.Add(new RowStyle(SizeType.AutoSize)); shell.RowStyles.Add(new RowStyle(SizeType.AutoSize)); Controls.Add(shell);
            tabs = new TabControl { Dock = DockStyle.Fill, Multiline = true }; shell.Controls.Add(tabs, 0, 0);
            var project = Page("1 Project"); var buttons = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, WrapContents = true };
            AddButton(buttons, "Refresh layers", RefreshMap); AddButton(buttons, "Load example", LoadExample); AddButton(buttons, "Save project", SaveProject); AddButton(buttons, "Open project", OpenProject); Wide(project, buttons);
            Row(project, "Output folder", PathPicker(outputRoot, "folder")); Wide(project, previewState); Wide(project, resultState);
            mcpEnabled.Checked = true; Wide(project, mcpEnabled); Wide(project, mcpState); mcpEnabled.CheckedChanged += (s, e) => UpdateMcpBridge();
            Wide(project, Note("Choose the data and validation settings. Inspect the folds on the map. A model run requires a current preview. Saved results are kept as previous runs."));
            var data = Page("2 Data and target"); AddChoices(inputType, new Choice("map", "ArcMap point layer"), new Choice("csv", "Table with coordinates")); Row(data, "Observation source", inputType);
            Row(data, "Point layer", points); Row(data, "CSV file", PathPicker(csvPath, "csv")); Row(data, "X / longitude field", csvX); Row(data, "Y / latitude field", csvY); Row(data, "Table CRS", csvCrs);
            var csvButtons = new FlowLayoutPanel { AutoSize = true }; AddButton(csvButtons, "Read table fields", LoadCsv); Wide(data, csvButtons);
            Row(data, "Target field", target); Row(data, "Observation ID", identity); Row(data, "Target unit", targetUnit); Row(data, "Unit field (optional)", unitField); Wide(data, selected);
            Row(data, "Separate independent test file", PathPicker(independentPath, "independent"));
            var heldButtons = new FlowLayoutPanel { AutoSize = true }; AddButton(heldButtons, "Read independent table fields", LoadIndependentFields); Wide(data, heldButtons);
            Row(data, "Independent table X field", independentX); Row(data, "Independent table Y field", independentY); Row(data, "Independent file CRS", independentCrs);
            Wide(data, Note("The independent test file must use the same target and ID fields, with different observation IDs. A table with coordinates needs separate X and Y fields and a coordinate reference system (CRS). A CRS defined in GeoJSON can be used. These observations are not used in model development."));
            Row(data, "Profile / parcel / farm groups", groups); Row(data, "Upper depth field", depthTop); Row(data, "Lower depth field", depthBottom); Row(data, "Measurement method field", laboratoryMethod); Row(data, "Sampling date field", dateField);
            AddChoices(support, new Choice("point", "Point"), new Choice("composite", "Composite sample"), new Choice("parcel_mean", "Parcel mean"));
            Row(data, "Area represented by the sample", support); Row(data, "Represented area note", supportNote); Row(data, "Parcel GeoJSON file", PathPicker(polygonPath, "geojson")); Row(data, "Parcel ID field", polygonId);
            Row(data, "Excluded observations and reasons", sampleExclusions);
            Wide(data, Note("The exclusion list and raster metadata are kept from the project file. These fields are read-only here. Information that was not recorded is not assumed."));
            Wide(data, Note("Check the type, sampling, scientific name, unit and ID of each raster band. For a categorical raster, only the nearest cell can be selected.")); Wide(data, rasters);
            rasters.Columns.Add(new DataGridViewCheckBoxColumn { Name = "use", HeaderText = "Use", Width = 35 });
            rasters.Columns.Add(new DataGridViewTextBoxColumn { Name = "name", HeaderText = "Scientific name", Width = 150 }); rasters.Columns.Add(new DataGridViewTextBoxColumn { Name = "band", HeaderText = "Band", Width = 45, ReadOnly = true });
            rasters.Columns.Add(new DataGridViewTextBoxColumn { Name = "unit", HeaderText = "Unit", Width = 70 }); rasters.Columns.Add(new DataGridViewTextBoxColumn { Name = "id", HeaderText = "ID", Width = 145 });
            rasters.Columns.Add(ChoiceColumn("kind", "Type", new Choice("continuous", "Continuous"), new Choice("categorical", "Categorical"))); rasters.Columns.Add(ChoiceColumn("sampling", "Sampling", new Choice("nearest", "Nearest cell"), new Choice("bilinear", "Bilinear")));
            rasters.Columns.Add(new DataGridViewTextBoxColumn { Name = "path", HeaderText = "File", Width = 300, ReadOnly = true });
            Row(data, "Selected raster band metadata", covariateMetadata);
            rasters.SelectionChanged += (s, e) => UpdateMetadataDetails(); UpdateMetadataDetails();
            rasters.CurrentCellDirtyStateChanged += (s, e) => { if (rasters.IsCurrentCellDirty) rasters.CommitEdit(DataGridViewDataErrorContexts.Commit); };
            rasters.CellValueChanged += (s, e) => { if (!loading) { RefreshCovariateChoices(); ScientificChange(s, e); } };
            points.SelectedIndexChanged += (s, e) => { if (!IsCsv) UpdateFields(); ScientificChange(s, e); };
            inputType.SelectedIndexChanged += (s, e) => { UpdateFields(); selected.Enabled = !busy && !IsCsv; ScientificChange(s, e); };
            groups.Options = () => FieldChoices(false); independent.Options = () => SampleChoices();
            groups.ValueChanged += (s, e) => { if (!loading) Safe(() => validation.Select(groups.Values.Length > 0 ? "group" : "random")); ScientificChange(s, e); };
            var validationPage = Page("3 Validation"); validation = Editor("validation", "random"); Wide(validationPage, validation);
            AddChoices(purpose, new Choice("within_area", "Prediction within the study area"), new Choice("transfer", "Transfer to another area"), new Choice("independent", "Independent test"));
            Row(validationPage, "Prediction purpose", purpose); Row(validationPage, "Independent test observations", independent); Wide(validationPage, customFolds); Wide(validationPage, Note("The user-defined fold table is used only with the user-defined folds method. Training and test IDs are stated explicitly. Repeated fold records do not increase the number of independent observations."));
            var preprocessingPage = Page("4 Preprocessing"); preprocessing = Editor("preprocessing", "standard_pipeline"); targetTransform = Editor("target_transform", "none"); Wide(preprocessingPage, preprocessing); Wide(preprocessingPage, Note("Target transformation")); Wide(preprocessingPage, targetTransform);
            var selectionPage = Page("5 Variable selection"); vif = Editor("vif", "diagnostic"); featureSelection = Editor("feature_selection", "none"); Wide(selectionPage, Note("Multicollinearity")); Wide(selectionPage, vif); Wide(selectionPage, Note("Variable selection / reduction")); Wide(selectionPage, featureSelection);
            var modelPage = Page("6 Model and search"); model = Editor("model", "ridge"); optimization = Editor("optimization", "manual"); candidates = new CandidateEditor(registry.Category("model")); Wide(modelPage, model); Wide(modelPage, candidates); Wide(modelPage, optimization);
            var searchButtons = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Top }; AddButton(searchButtons, "Refresh search parameters", RefreshSpaces); Wide(modelPage, searchButtons); Wide(modelPage, spaces);
            Wide(modelPage, permutation); Row(modelPage, "Permutation repeats", permutationRepeats); Wide(modelPage, Note("This shows how much the prediction error changes when the values of a variable are permuted. This result does not show a causal effect."));
            model.ValueChanged += (s, e) => { if (!loading) RefreshSpaces(); }; candidates.ValueChanged += (s, e) => { if (!loading) RefreshSpaces(); ScientificChange(s, e); };
            evaluationPage = new TabPage("7 Evaluation"); tabs.TabPages.Add(evaluationPage); var resultLayout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3 }; resultLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); resultLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); resultLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            resultLayout.Controls.Add(Note("Outer fold, out-of-fold prediction, independent test and search tables. Performance metrics are read from the verified table of the completed job."), 0, 0); resultLayout.Controls.Add(outputTables, 0, 1); resultLayout.Controls.Add(results, 0, 2); evaluationPage.Controls.Add(resultLayout); outputTables.SelectedIndexChanged += (s, e) => Safe(ShowTable);
            var uncertaintyPage = Page("8 Uncertainty"); uncertainty = Editor("uncertainty", "none"); Wide(uncertaintyPage, uncertainty); Wide(uncertaintyPage, Note("Quantile regression forest is used with random forest. Gradient boosting with quantile loss is used with histogram-based gradient boosting. The calibration fraction is assessed together with the validation design."));
            subgroupFields.Options = () => FieldChoices(false); Row(uncertaintyPage, "Fields for subgroup diagnostics", subgroupFields); Row(uncertaintyPage, "Minimum observations per subgroup", minSubgroupSamples); Row(uncertaintyPage, "Minimum independent groups per subgroup", minSubgroupGroups);
            Wide(uncertaintyPage, Note("Error and coverage diagnostics are produced for the categories of the selected fields. Results that do not reach the minimum numbers of observations and independent groups are flagged separately."));
            var mappingPage = Page("9 Mapping"); Wide(mappingPage, align); Row(mappingPage, "Reference raster ID", referenceRaster); Wide(mappingPage, mask); Row(mappingPage, "Processing window (cells)", windowSize); Wide(mappingPage, physicalMinUse); Row(mappingPage, "Lower bound", physicalMin); Wide(mappingPage, physicalMaxUse); Row(mappingPage, "Upper bound", physicalMax);
            Row(mappingPage, "Maximum missing data fraction", maxMissing); AddChoices(figureDpi, new Choice("150", "150 dpi"), new Choice("900", "900 dpi")); Row(mappingPage, "Figure resolution", figureDpi); Wide(mappingPage, Note("Predictions outside the physical bounds are flagged in a separate layer. Prediction values are not clipped to the bound."));
            Wide(mappingPage, applicability); Wide(mappingPage, Note("The applicability layer has the values 1: inside the area and 0: outside the area. Cells without data are kept separate. Low environmental dissimilarity does not by itself show that a prediction is adequate."));
            reportPage = new TabPage("10 Report and log"); tabs.TabPages.Add(reportPage); var reportLayout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 4 }; reportLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); reportLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); reportLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); reportLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            reportLayout.Controls.Add(outputReports, 0, 0); var reportButtons = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Top }; AddButton(reportButtons, "Open report", OpenReport, false); saveModel.Click += (s, e) => Safe(ExportModel); reportButtons.Controls.Add(saveModel); AddButton(reportButtons, "Save study package", ExportStudy); AddButton(reportButtons, "Save log", ExportLog, false); AddButton(reportButtons, "Add previous result to map", ReloadResult); reportLayout.Controls.Add(reportButtons, 0, 1); reportLayout.Controls.Add(modelExportState, 0, 2); reportLayout.Controls.Add(log, 0, 3); reportPage.Controls.Add(reportLayout);
            var actions = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill }; preflight.Click += (s, e) => Safe(Preflight); preview.Click += (s, e) => StartJob("preview"); run.Click += (s, e) => StartJob("run"); cancel.Click += (s, e) => Safe(Cancel); actions.Controls.AddRange(new Control[] { preflight, preview, run, cancel }); shell.Controls.Add(actions, 0, 1);
            var footer = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1 }; footer.Controls.Add(state); footer.Controls.Add(progress); shell.Controls.Add(footer, 0, 2);
            guarded.AddRange(new Control[] { points, target, identity, targetUnit, selected, outputRoot, groups, depthTop, depthBottom, laboratoryMethod, dateField, purpose, independent, customFolds, support, supportNote, polygonPath, polygonId, rasters, candidates, spaces, align, referenceRaster, mask, windowSize, physicalMinUse, physicalMin, physicalMaxUse, physicalMax, figureDpi, preflight, preview, inputType, csvPath, csvX, csvY, csvCrs });
            foreach (var control in new Control[] { target, identity, targetUnit, selected, depthTop, depthBottom, laboratoryMethod, dateField, purpose, support, supportNote, polygonPath, polygonId, align, referenceRaster, mask, windowSize, physicalMinUse, physicalMin, physicalMaxUse, physicalMax, figureDpi, csvPath, csvX, csvY, csvCrs }) WireChange(control);
            independent.ValueChanged += ScientificChange; customFolds.ValueChanged += ScientificChange; spaces.ValueChanged += ScientificChange; RefreshSpaces(); UpdateGate();
            guarded.AddRange(new Control[] { independentPath, unitField, maxMissing, independentX, independentY, independentCrs }); WireChange(independentPath); WireChange(unitField); WireChange(maxMissing); WireChange(independentX); WireChange(independentY); WireChange(independentCrs);
            guarded.AddRange(new Control[] { applicability, permutation, permutationRepeats, subgroupFields, minSubgroupSamples, minSubgroupGroups }); WireChange(applicability); WireChange(permutation); WireChange(permutationRepeats); WireChange(minSubgroupSamples); WireChange(minSubgroupGroups); subgroupFields.ValueChanged += ScientificChange;
        }
        private Control PathPicker(TextBox text, string type)
        {
            var panel = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 2 }; panel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100)); panel.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize)); panel.Controls.Add(text, 0, 0); var button = ActionButton("Select"); panel.Controls.Add(button, 1, 0); guarded.Add(button);
            button.Click += (s, e) => Safe(() => { if (type == "folder") { using (var dialog = new FolderBrowserDialog { SelectedPath = text.Text }) if (dialog.ShowDialog(this) == DialogResult.OK) text.Text = dialog.SelectedPath; } else using (var dialog = new OpenFileDialog { Filter = type == "independent" ? "Observation file (*.csv;*.geojson;*.json)|*.csv;*.geojson;*.json" : type == "csv" ? "Comma-separated values (*.csv)|*.csv" : "GeoJSON (*.geojson;*.json)|*.geojson;*.json" }) if (dialog.ShowDialog(this) == DialogResult.OK) { text.Text = dialog.FileName; if (type == "csv") LoadCsv(); else if (type == "independent") { independentCrs.Clear(); LoadIndependentFields(); } } }); return panel;
        }
        private static DataGridViewComboBoxColumn ChoiceColumn(string name, string header, params Choice[] choices) { return new DataGridViewComboBoxColumn { Name = name, HeaderText = header, Width = 115, DataSource = choices.Select(c => new { Value = c.Id, Label = c.Label }).ToArray(), ValueMember = "Value", DisplayMember = "Label" }; }
        private static void AddChoices(ComboBox combo, params Choice[] values) { combo.Items.AddRange(values); if (values.Length > 0) combo.SelectedIndex = 0; }
        private static string ChoiceId(ComboBox combo) { return (combo.SelectedItem as Choice)?.Id ?? ""; }
        private static void Choose(ComboBox combo, string id) { var value = combo.Items.Cast<Choice>().FirstOrDefault(c => c.Id == id); if (value == null) throw new InvalidDataException("Saved choice is not in the source: " + id); combo.SelectedItem = value; }
        private void WireChange(Control control) { if (control is ComboBox) ((ComboBox)control).SelectedIndexChanged += ScientificChange; else if (control is CheckBox) ((CheckBox)control).CheckedChanged += ScientificChange; else if (control is NumericUpDown) ((NumericUpDown)control).ValueChanged += ScientificChange; else control.TextChanged += ScientificChange; }
        private void ScientificChange(object sender, EventArgs args) { if (!loading) InvalidatePreview("Scientific settings changed."); }
        private void InvalidatePreview(string reason)
        {
            bool had = !String.IsNullOrEmpty(previewFingerprint); previewFingerprint = null; previewSignature = null; previewSources = null; currentResult = false;
            previewState.Text = "A new fold preview is required. " + reason; resultState.Text = String.IsNullOrEmpty(lastManifest) ? "No completed model yet." : "The previous result is kept; it was not produced with the current settings."; if (had) Log("Preview invalidated: " + reason); UpdateGate();
        }
        private void UpdateGate()
        {
            run.Enabled = !busy && !projectLoadFailed && !String.IsNullOrEmpty(previewFingerprint);
            bool hasResult = !String.IsNullOrWhiteSpace(lastManifest) && File.Exists(lastManifest);
            saveModel.Enabled = !busy && hasResult;
            modelExportState.Text = !hasResult ? "Saving a model requires a completed run." : currentResult
                ? "The model to save is the model of the last completed run."
                : "The model to save belongs to a previous run. The current settings were not applied to this model.";
        }
        private void SetBusy(bool value) { busy = value; foreach (var control in guarded) control.Enabled = !value; selected.Enabled = !value && !IsCsv; cancel.Enabled = value; UpdateGate(); }
        private void Safe(Action action) { try { action(); } catch (Exception ex) { if (remoteCommandDepth > 0 || projectLoadDepth > 0) throw; Fail(ex); } }
        private void Fail(Exception ex) { state.Text = "Error: " + ex.Message; Log(ex.ToString()); MessageBox.Show(this, ex.Message, "Spatial Prediction Studio", MessageBoxButtons.OK, MessageBoxIcon.Error); }
        private void OnUi(Action action) { if (disposed || IsDisposed) return; if (InvokeRequired) { try { BeginInvoke(new Action(() => Safe(action))); } catch (InvalidOperationException) { } } else Safe(action); }
        private void Log(string message) { OnUi(() => { string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff") + " " + message + Environment.NewLine; log.AppendText(line); try { File.AppendAllText(logPath, line, new UTF8Encoding(false)); } catch (IOException) { } }); }
        protected override void Dispose(bool disposing) { if (disposing && !disposed) { disposed = true; mcpBridge?.Dispose(); mcpBridge = null; sourceTimer.Stop(); sourceTimer.Dispose(); client.StopForShutdown(); foreach (var viewer in ownedViewers.ToArray()) if (!viewer.IsDisposed) viewer.Close(); } base.Dispose(disposing); }
    }
}
