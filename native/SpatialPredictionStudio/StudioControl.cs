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
        private readonly CheckBox applicability = Check("Modelin uygulanabilirlik alanını hesapla"), permutation = Check("Permütasyonla değişken önemini hesapla");
        private readonly NumericUpDown permutationRepeats = Number(2, 100, 5), minSubgroupSamples = Number(3, 1000, 10), minSubgroupGroups = Number(2, 1000, 3);
        private readonly MultiChoiceControl subgroupFields = new MultiChoiceControl();
        private readonly CheckBox selected = Check("Yalnız haritada seçili noktalar"), align = Check("Rasterleri referans gridine hizala"), mask = Check("Eğitim aralığı dışındaki tahminleri maskele"), physicalMinUse = Check("Fiziksel alt sınır"), physicalMaxUse = Check("Fiziksel üst sınır");
        private readonly NumericUpDown physicalMin = Number(-1000000000, 1000000000, 0, 4), physicalMax = Number(-1000000000, 1000000000, 100, 4), windowSize = Number(16, 4096, 256);
        private readonly DataGridView rasters = new DataGridView { Dock = DockStyle.Fill, AllowUserToAddRows = false, AllowUserToDeleteRows = false, RowHeadersVisible = false, SelectionMode = DataGridViewSelectionMode.FullRowSelect, Height = 240, BackgroundColor = Color.White };
        private readonly TextBox covariateMetadata = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill, WordWrap = true, Height = 145 };
        private readonly TextBox sampleExclusions = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill, WordWrap = true, Height = 80, Text = "Kaydedilmiş dışlama yok." };
        private readonly TextBox log = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Both, Dock = DockStyle.Fill, WordWrap = false, Font = new Font("Consolas", 9) };
        private readonly Label state = Note("Yeni kat ön gösterimi gerekli."), previewState = Note("Henüz kat ön gösterimi oluşturulmadı."), resultState = Note("Henüz tamamlanmış model yok.");
        private readonly ProgressBar progress = new ProgressBar { Dock = DockStyle.Fill, Height = 18 };
        private readonly Button run = ActionButton("Modeli çalıştır", false), cancel = ActionButton("İptal et", false), preview = ActionButton("Katları göster"), preflight = ActionButton("Ön kontrol");
        private readonly Button saveModel = ActionButton("Modeli kaydet", false);
        private readonly Label modelExportState = Note("Modeli kaydetmek için tamamlanmış bir çalışma gerekir.");
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
            catch (Exception ex) { Controls.Add(Note("Panel yapılandırması yüklenemedi: " + ex.Message)); Log(ex.ToString()); return; }
            loading = false;
            Load += (s, e) => { Log("Mekânsal tahmin paneli açıldı. ArcMap süreç kimliği: " + System.Diagnostics.Process.GetCurrentProcess().Id + "; ana iş parçacığı: " + Thread.CurrentThread.ManagedThreadId); RefreshMap(); sourceTimer.Start(); UpdateMcpBridge(); };
            sourceTimer.Tick += (s, e) => {
                if (busy || String.IsNullOrEmpty(previewFingerprint)) return;
                try { if (CurrentSources() != previewSources) InvalidatePreview("Kaynak veri, sorgu veya seçili gözlemler değişti."); }
                catch (Exception ex) { InvalidatePreview("Kaynak yeniden denetlenemedi: " + ex.Message); }
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
            var project = Page("1 Proje"); var buttons = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, WrapContents = true };
            AddButton(buttons, "Katmanları yenile", RefreshMap); AddButton(buttons, "Örneği yükle", LoadExample); AddButton(buttons, "Proje kaydet", SaveProject); AddButton(buttons, "Proje aç", OpenProject); Wide(project, buttons);
            Row(project, "Çıktı klasörü", PathPicker(outputRoot, "folder")); Wide(project, previewState); Wide(project, resultState);
            mcpEnabled.Checked = true; Wide(project, mcpEnabled); Wide(project, mcpState); mcpEnabled.CheckedChanged += (s, e) => UpdateMcpBridge();
            Wide(project, Note("Veri ve doğrulama ayarlarını seçin. Katları haritada inceleyin. Model çalışması güncel ön gösterim gerektirir. Kayıtlı sonuçlar geçmiş çalışma olarak korunur."));
            var data = Page("2 Veri ve hedef"); AddChoices(inputType, new Choice("map", "ArcMap nokta katmanı"), new Choice("csv", "Koordinatlı tablo")); Row(data, "Gözlem kaynağı", inputType);
            Row(data, "Nokta katmanı", points); Row(data, "Virgülle ayrılmış dosya", PathPicker(csvPath, "csv")); Row(data, "X / boylam alanı", csvX); Row(data, "Y / enlem alanı", csvY); Row(data, "Tablonun koordinat sistemi", csvCrs);
            var csvButtons = new FlowLayoutPanel { AutoSize = true }; AddButton(csvButtons, "Tablo alanlarını oku", LoadCsv); Wide(data, csvButtons);
            Row(data, "Hedef alanı", target); Row(data, "Gözlem kimliği", identity); Row(data, "Hedef birimi", targetUnit); Row(data, "Birim alanı (isteğe bağlı)", unitField); Wide(data, selected);
            Row(data, "Ayrı bağımsız test dosyası", PathPicker(independentPath, "independent"));
            var heldButtons = new FlowLayoutPanel { AutoSize = true }; AddButton(heldButtons, "Bağımsız tablo alanlarını oku", LoadIndependentFields); Wide(data, heldButtons);
            Row(data, "Bağımsız tablo X alanı", independentX); Row(data, "Bağımsız tablo Y alanı", independentY); Row(data, "Bağımsız dosyanın koordinat sistemi", independentCrs);
            Wide(data, Note("Bağımsız test dosyası aynı hedef ve kimlik alanlarını, farklı gözlem kimliklerini kullanmalıdır. Koordinatlı tabloda ayrı X, Y ve koordinat sistemi gerekir. GeoJSON içinde tanımlı koordinat sistemi kullanılabilir. Bu gözlemler model geliştirmeye alınmaz."));
            Row(data, "Profil / parsel / işletme grupları", groups); Row(data, "Derinlik üst sınırı alanı", depthTop); Row(data, "Derinlik alt sınırı alanı", depthBottom); Row(data, "Ölçüm yöntemi alanı", laboratoryMethod); Row(data, "Örnekleme tarihi alanı", dateField);
            AddChoices(support, new Choice("point", "Nokta"), new Choice("composite", "Kompozit örnek"), new Choice("parcel_mean", "Parsel ortalaması"));
            Row(data, "Örneğin temsil ettiği alan", support); Row(data, "Temsil alanı açıklaması", supportNote); Row(data, "Parsel GeoJSON dosyası", PathPicker(polygonPath, "geojson")); Row(data, "Parsel kimlik alanı", polygonId);
            Row(data, "Dışlanan gözlemler ve gerekçeleri", sampleExclusions);
            Wide(data, Note("Dışlama listesi ve raster üst verisi proje dosyasından korunur. Bu alanlar burada salt okunurdur. Kaydedilmemiş bilgi varsayılmaz."));
            Wide(data, Note("Raster bantlarında tür, örnekleme, bilimsel ad, birim ve kimliği denetleyin. Kategorik raster için yalnız en yakın hücre seçilebilir.")); Wide(data, rasters);
            rasters.Columns.Add(new DataGridViewCheckBoxColumn { Name = "use", HeaderText = "Seç", Width = 35 });
            rasters.Columns.Add(new DataGridViewTextBoxColumn { Name = "name", HeaderText = "Bilimsel ad", Width = 150 }); rasters.Columns.Add(new DataGridViewTextBoxColumn { Name = "band", HeaderText = "Bant", Width = 45, ReadOnly = true });
            rasters.Columns.Add(new DataGridViewTextBoxColumn { Name = "unit", HeaderText = "Birim", Width = 70 }); rasters.Columns.Add(new DataGridViewTextBoxColumn { Name = "id", HeaderText = "Kimlik", Width = 145 });
            rasters.Columns.Add(ChoiceColumn("kind", "Tür", new Choice("continuous", "Sürekli"), new Choice("categorical", "Kategorik"))); rasters.Columns.Add(ChoiceColumn("sampling", "Örnekleme", new Choice("nearest", "En yakın hücre"), new Choice("bilinear", "Bilineer")));
            rasters.Columns.Add(new DataGridViewTextBoxColumn { Name = "path", HeaderText = "Dosya", Width = 300, ReadOnly = true });
            Row(data, "Seçili raster bandının üst verisi", covariateMetadata);
            rasters.SelectionChanged += (s, e) => UpdateMetadataDetails(); UpdateMetadataDetails();
            rasters.CurrentCellDirtyStateChanged += (s, e) => { if (rasters.IsCurrentCellDirty) rasters.CommitEdit(DataGridViewDataErrorContexts.Commit); };
            rasters.CellValueChanged += (s, e) => { if (!loading) { RefreshCovariateChoices(); ScientificChange(s, e); } };
            points.SelectedIndexChanged += (s, e) => { if (!IsCsv) UpdateFields(); ScientificChange(s, e); };
            inputType.SelectedIndexChanged += (s, e) => { UpdateFields(); selected.Enabled = !busy && !IsCsv; ScientificChange(s, e); };
            groups.Options = () => FieldChoices(false); independent.Options = () => SampleChoices();
            groups.ValueChanged += (s, e) => { if (!loading) Safe(() => validation.Select(groups.Values.Length > 0 ? "group" : "random")); ScientificChange(s, e); };
            var validationPage = Page("3 Doğrulama"); validation = Editor("validation", "random"); Wide(validationPage, validation);
            AddChoices(purpose, new Choice("within_area", "Çalışma alanı içinde tahmin"), new Choice("transfer", "Başka alana aktarım"), new Choice("independent", "Bağımsız sınama"));
            Row(validationPage, "Tahmin amacı", purpose); Row(validationPage, "Bağımsız test gözlemleri", independent); Wide(validationPage, customFolds); Wide(validationPage, Note("Özel kat tablosu yalnız özel doğrulama yönteminde kullanılır. Eğitim ve sınama kimlikleri açıkça belirtilir. Tekrarlı kat kayıtları bağımsız gözlem sayısını artırmaz."));
            var preprocessingPage = Page("4 Ön işleme"); preprocessing = Editor("preprocessing", "standard_pipeline"); targetTransform = Editor("target_transform", "none"); Wide(preprocessingPage, preprocessing); Wide(preprocessingPage, Note("Hedef dönüşümü")); Wide(preprocessingPage, targetTransform);
            var selectionPage = Page("5 Değişken seçimi"); vif = Editor("vif", "diagnostic"); featureSelection = Editor("feature_selection", "none"); Wide(selectionPage, Note("Çoklu doğrusal bağlantı")); Wide(selectionPage, vif); Wide(selectionPage, Note("Değişken seçimi / indirgeme")); Wide(selectionPage, featureSelection);
            var modelPage = Page("6 Model ve arama"); model = Editor("model", "ridge"); optimization = Editor("optimization", "manual"); candidates = new CandidateEditor(registry.Category("model")); Wide(modelPage, model); Wide(modelPage, candidates); Wide(modelPage, optimization);
            var searchButtons = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Top }; AddButton(searchButtons, "Arama parametrelerini yenile", RefreshSpaces); Wide(modelPage, searchButtons); Wide(modelPage, spaces);
            Wide(modelPage, permutation); Row(modelPage, "Permütasyon tekrar sayısı", permutationRepeats); Wide(modelPage, Note("Değişken değerleri yer değiştirdiğinde tahmin hatasının ne kadar değiştiği incelenir. Bu sonuç nedensel etkiyi göstermez."));
            model.ValueChanged += (s, e) => { if (!loading) RefreshSpaces(); }; candidates.ValueChanged += (s, e) => { if (!loading) RefreshSpaces(); ScientificChange(s, e); };
            evaluationPage = new TabPage("7 Değerlendirme"); tabs.TabPages.Add(evaluationPage); var resultLayout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3 }; resultLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); resultLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); resultLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            resultLayout.Controls.Add(Note("Dış kat, kat dışında üretilen tahmin, bağımsız test ve arama tabloları. Başarı ölçütleri tamamlanmış işin doğrulanmış tablosundan okunur."), 0, 0); resultLayout.Controls.Add(outputTables, 0, 1); resultLayout.Controls.Add(results, 0, 2); evaluationPage.Controls.Add(resultLayout); outputTables.SelectedIndexChanged += (s, e) => Safe(ShowTable);
            var uncertaintyPage = Page("8 Belirsizlik"); uncertainty = Editor("uncertainty", "none"); Wide(uncertaintyPage, uncertainty); Wide(uncertaintyPage, Note("Kantil ormanı rastgele ormanla, kantil artırma histograma dayalı artırmayla kullanılır. Kalibrasyon payı doğrulama tasarımıyla birlikte değerlendirilir."));
            subgroupFields.Options = () => FieldChoices(false); Row(uncertaintyPage, "Alt grup tanısı için alanlar", subgroupFields); Row(uncertaintyPage, "Alt grupta en az gözlem", minSubgroupSamples); Row(uncertaintyPage, "Alt grupta en az bağımsız grup", minSubgroupGroups);
            Wide(uncertaintyPage, Note("Seçilen alanların kategorileri için hata ve kapsama tanıları üretilir. Asgari gözlem ve bağımsız grup sayısı sağlanmayan sonuçlar ayrıca belirtilir."));
            var mappingPage = Page("9 Haritalama"); Wide(mappingPage, align); Row(mappingPage, "Referans raster kimliği", referenceRaster); Wide(mappingPage, mask); Row(mappingPage, "İşleme penceresi (hücre)", windowSize); Wide(mappingPage, physicalMinUse); Row(mappingPage, "Alt sınır", physicalMin); Wide(mappingPage, physicalMaxUse); Row(mappingPage, "Üst sınır", physicalMax);
            Row(mappingPage, "En fazla eksik veri oranı", maxMissing); AddChoices(figureDpi, new Choice("150", "150 nokta/inç"), new Choice("900", "900 nokta/inç")); Row(mappingPage, "Şekil çözünürlüğü", figureDpi); Wide(mappingPage, Note("Fiziksel sınır dışındaki tahminler ayrı katmanda işaretlenir. Tahmin değerleri sınır değerine kırpılmaz."));
            Wide(mappingPage, applicability); Wide(mappingPage, Note("Uygulanabilirlik katmanı 1: alan içinde, 0: alan dışında değerlerini taşır. Veri olmayan hücreler ayrıdır. Çevresel benzemezliğin düşük olması tahminin yeterli olduğunu tek başına göstermez."));
            reportPage = new TabPage("10 Rapor ve günlük"); tabs.TabPages.Add(reportPage); var reportLayout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 4 }; reportLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); reportLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); reportLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); reportLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            reportLayout.Controls.Add(outputReports, 0, 0); var reportButtons = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Top }; AddButton(reportButtons, "Raporu aç", OpenReport, false); saveModel.Click += (s, e) => Safe(ExportModel); reportButtons.Controls.Add(saveModel); AddButton(reportButtons, "Çalışma paketini kaydet", ExportStudy); AddButton(reportButtons, "Günlüğü kaydet", ExportLog, false); AddButton(reportButtons, "Geçmiş sonucu haritaya ekle", ReloadResult); reportLayout.Controls.Add(reportButtons, 0, 1); reportLayout.Controls.Add(modelExportState, 0, 2); reportLayout.Controls.Add(log, 0, 3); reportPage.Controls.Add(reportLayout);
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
            var panel = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 2 }; panel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100)); panel.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize)); panel.Controls.Add(text, 0, 0); var button = ActionButton("Seç"); panel.Controls.Add(button, 1, 0); guarded.Add(button);
            button.Click += (s, e) => Safe(() => { if (type == "folder") { using (var dialog = new FolderBrowserDialog { SelectedPath = text.Text }) if (dialog.ShowDialog(this) == DialogResult.OK) text.Text = dialog.SelectedPath; } else using (var dialog = new OpenFileDialog { Filter = type == "independent" ? "Gözlem dosyası (*.csv;*.geojson;*.json)|*.csv;*.geojson;*.json" : type == "csv" ? "Virgülle ayrılmış tablo (*.csv)|*.csv" : "GeoJSON (*.geojson;*.json)|*.geojson;*.json" }) if (dialog.ShowDialog(this) == DialogResult.OK) { text.Text = dialog.FileName; if (type == "csv") LoadCsv(); else if (type == "independent") { independentCrs.Clear(); LoadIndependentFields(); } } }); return panel;
        }
        private static DataGridViewComboBoxColumn ChoiceColumn(string name, string header, params Choice[] choices) { return new DataGridViewComboBoxColumn { Name = name, HeaderText = header, Width = 115, DataSource = choices.Select(c => new { Value = c.Id, Label = c.Label }).ToArray(), ValueMember = "Value", DisplayMember = "Label" }; }
        private static void AddChoices(ComboBox combo, params Choice[] values) { combo.Items.AddRange(values); if (values.Length > 0) combo.SelectedIndex = 0; }
        private static string ChoiceId(ComboBox combo) { return (combo.SelectedItem as Choice)?.Id ?? ""; }
        private static void Choose(ComboBox combo, string id) { var value = combo.Items.Cast<Choice>().FirstOrDefault(c => c.Id == id); if (value == null) throw new InvalidDataException("Kayıtlı seçim kaynakta yok: " + id); combo.SelectedItem = value; }
        private void WireChange(Control control) { if (control is ComboBox) ((ComboBox)control).SelectedIndexChanged += ScientificChange; else if (control is CheckBox) ((CheckBox)control).CheckedChanged += ScientificChange; else if (control is NumericUpDown) ((NumericUpDown)control).ValueChanged += ScientificChange; else control.TextChanged += ScientificChange; }
        private void ScientificChange(object sender, EventArgs args) { if (!loading) InvalidatePreview("Bilimsel ayarlar değişti."); }
        private void InvalidatePreview(string reason)
        {
            bool had = !String.IsNullOrEmpty(previewFingerprint); previewFingerprint = null; previewSignature = null; previewSources = null; currentResult = false;
            previewState.Text = "Yeni kat ön gösterimi gerekli. " + reason; resultState.Text = String.IsNullOrEmpty(lastManifest) ? "Henüz tamamlanmış model yok." : "Geçmiş sonuç korunuyor; güncel ayarlara ait değildir."; if (had) Log("Ön gösterim geçersizleşti: " + reason); UpdateGate();
        }
        private void UpdateGate()
        {
            run.Enabled = !busy && !projectLoadFailed && !String.IsNullOrEmpty(previewFingerprint);
            bool hasResult = !String.IsNullOrWhiteSpace(lastManifest) && File.Exists(lastManifest);
            saveModel.Enabled = !busy && hasResult;
            modelExportState.Text = !hasResult ? "Modeli kaydetmek için tamamlanmış bir çalışma gerekir." : currentResult
                ? "Kaydedilecek model, son tamamlanan çalışmanın modelidir."
                : "Kaydedilecek model geçmiş çalışmaya aittir. Güncel ayarlar bu modele uygulanmamıştır.";
        }
        private void SetBusy(bool value) { busy = value; foreach (var control in guarded) control.Enabled = !value; selected.Enabled = !value && !IsCsv; cancel.Enabled = value; UpdateGate(); }
        private void Safe(Action action) { try { action(); } catch (Exception ex) { if (remoteCommandDepth > 0 || projectLoadDepth > 0) throw; Fail(ex); } }
        private void Fail(Exception ex) { state.Text = "Hata: " + ex.Message; Log(ex.ToString()); MessageBox.Show(this, ex.Message, "Spatial Prediction Studio", MessageBoxButtons.OK, MessageBoxIcon.Error); }
        private void OnUi(Action action) { if (disposed || IsDisposed) return; if (InvokeRequired) { try { BeginInvoke(new Action(() => Safe(action))); } catch (InvalidOperationException) { } } else Safe(action); }
        private void Log(string message) { OnUi(() => { string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff") + " " + message + Environment.NewLine; log.AppendText(line); try { File.AppendAllText(logPath, line, new UTF8Encoding(false)); } catch (IOException) { } }); }
        protected override void Dispose(bool disposing) { if (disposing && !disposed) { disposed = true; mcpBridge?.Dispose(); mcpBridge = null; sourceTimer.Stop(); sourceTimer.Dispose(); client.StopForShutdown(); foreach (var viewer in ownedViewers.ToArray()) if (!viewer.IsDisposed) viewer.Close(); } base.Dispose(disposing); }
    }
}
