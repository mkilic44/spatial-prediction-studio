using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.DataSourcesFile;
using ESRI.ArcGIS.DataSourcesGDB;
using ESRI.ArcGIS.Framework;
using ESRI.ArcGIS.Geodatabase;
using ESRI.ArcGIS.Geometry;
using ESRI.ArcGIS.Display;

namespace SpatialPredictionStudio
{
    internal sealed class PointLayerInfo
    {
        public string Name;
        public string SourcePath;
        public IFeatureLayer Layer;
        public override string ToString() { return Name; }
    }
    internal sealed class RasterBandInfo
    {
        public string Id;
        public string Name;
        public string Path;
        public int Band;
        public string Unit = "bilinmiyor";
    }
    internal sealed class ExportedSamples
    {
        public int Count;
        public string Crs;
    }

    internal sealed class ArcMapAdapter
    {
        private readonly IApplication application;
        private readonly int ownerThread;
        internal ArcMapAdapter(IApplication application)
        {
            this.application = application ?? throw new ArgumentNullException(nameof(application), "ArcMap uygulama bağlantısı yok.");
            ownerThread = Thread.CurrentThread.ManagedThreadId;
        }
        private void AssertThread()
        {
            if (Thread.CurrentThread.ManagedThreadId != ownerThread) throw new InvalidOperationException("ArcObjects erişimi ana arayüz iş parçacığında olmalıdır.");
        }
        private IMap Map { get { AssertThread(); var doc = application.Document as IMxDocument; if (doc == null) throw new InvalidOperationException("Etkin ArcMap belgesi yok."); return doc.FocusMap; } }
        internal void Inventory(out List<PointLayerInfo> points, out List<RasterBandInfo> rasters, Action<string> log)
        {
            points = new List<PointLayerInfo>(); rasters = new List<RasterBandInfo>();
            var map = Map;
            for (int i = 0; i < map.LayerCount; ++i) Visit(map.get_Layer(i), "", points, rasters, log);
        }
        private void Visit(ILayer layer, string prefix, List<PointLayerInfo> points, List<RasterBandInfo> rasters, Action<string> log)
        {
            var name = prefix + layer.Name;
            var composite = layer as ICompositeLayer;
            if (composite != null)
            {
                for (int i = 0; i < composite.Count; ++i) Visit(composite.get_Layer(i), name + " / ", points, rasters, log);
                return;
            }
            if (!layer.Valid) { log("Geçersiz katman atlandı: " + name); return; }
            var feature = layer as IFeatureLayer;
            if (feature != null && feature.FeatureClass != null && feature.FeatureClass.ShapeType == esriGeometryType.esriGeometryPoint)
            {
                var dataset = feature.FeatureClass as IDataset;
                string sourcePath = dataset == null ? "" : System.IO.Path.Combine(dataset.Workspace.PathName, dataset.Name);
                if (!String.IsNullOrWhiteSpace(sourcePath) && !File.Exists(sourcePath) && File.Exists(sourcePath + ".shp")) sourcePath += ".shp";
                points.Add(new PointLayerInfo { Name = name, Layer = feature, SourcePath = sourcePath });
            }
            var raster = layer as IRasterLayer;
            if (raster == null) return;
            var path = raster.FilePath;
            if (String.IsNullOrWhiteSpace(path) || !File.Exists(path)) { log("Dosya tabanlı olmayan raster kullanılamaz: " + name); return; }
            for (int band = 1; band <= raster.BandCount; ++band)
                rasters.Add(new RasterBandInfo { Id = StableId(path, band), Name = name + " - Bant " + band, Path = System.IO.Path.GetFullPath(path), Band = band });
        }
        internal List<string> Fields(PointLayerInfo info, bool numericOnly)
        {
            AssertThread();
            var result = new List<string>();
            var fields = info.Layer.FeatureClass.Fields;
            for (int i = 0; i < fields.FieldCount; ++i)
            {
                var field = fields.get_Field(i);
                bool numeric = field.Type == esriFieldType.esriFieldTypeDouble || field.Type == esriFieldType.esriFieldTypeSingle || field.Type == esriFieldType.esriFieldTypeInteger || field.Type == esriFieldType.esriFieldTypeSmallInteger;
                if (numeric || (!numericOnly && (field.Type == esriFieldType.esriFieldTypeString || field.Type == esriFieldType.esriFieldTypeOID || field.Type == esriFieldType.esriFieldTypeGUID || field.Type == esriFieldType.esriFieldTypeGlobalID || field.Type == esriFieldType.esriFieldTypeDate))) result.Add(field.Name);
            }
            return result;
        }
        internal ExportedSamples ExportPoints(PointLayerInfo info, string idField, string targetField, bool selectedOnly, string path)
        {
            AssertThread();
            var layer = info.Layer;
            var featureClass = layer.FeatureClass;
            var workspace = ((IDataset)featureClass).Workspace;
            var editable = workspace as IWorkspaceEdit;
            if (editable != null && editable.IsBeingEdited()) throw new InvalidOperationException("Nokta kaynağında düzenleme oturumu açık. Önce düzenlemeleri kaydedip oturumu kapatın.");
            if (String.Equals(idField, targetField, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Kimlik ve hedef alanı aynı olamaz.");
            var definition = layer as IFeatureLayerDefinition;
            if (definition != null && definition.RelationshipClass != null) throw new InvalidOperationException("İliştirilmiş tablolar Aşama A'da desteklenmiyor. Bağımsız bir çalışma kopyası seçin.");
            int idIndex = featureClass.FindField(idField), targetIndex = featureClass.FindField(targetField);
            if (idIndex < 0 || targetIndex < 0) throw new InvalidDataException("Kimlik veya hedef alanı bulunamadı.");
            var spatialReference = ((IGeoDataset)featureClass).SpatialReference;
            if (spatialReference == null || spatialReference is IUnknownCoordinateSystem) throw new InvalidDataException("Nokta katmanının koordinat sistemi tanımlı değil.");
            string wkt; int length;
            ((IESRISpatialReferenceGEN)spatialReference).ExportToESRISpatialReference(out wkt, out length);
            if (String.IsNullOrWhiteSpace(wkt)) throw new InvalidDataException("Nokta koordinat sistemi dışa aktarılamadı.");
            // ArcGIS may append precision/domain metadata after the WKT separator.
            int separator = wkt.IndexOf(';'); if (separator >= 0) wkt = wkt.Substring(0, separator);
            IFeatureCursor cursor = null;
            var features = new List<object>();
            var ids = new HashSet<string>(StringComparer.Ordinal);
            try
            {
                if (selectedOnly)
                {
                    var selection = ((IFeatureSelection)layer).SelectionSet;
                    if (selection.Count == 0) throw new InvalidDataException("Yalnız seçili noktalar istendi ancak seçili kayıt yok.");
                    ICursor selectionCursor;
                    selection.Search(null, false, out selectionCursor);
                    cursor = (IFeatureCursor)selectionCursor;
                }
                else cursor = layer.Search(null, false);
                IFeature feature;
                while ((feature = cursor.NextFeature()) != null)
                {
                    try
                    {
                        object rawId = feature.get_Value(idIndex), rawTarget = feature.get_Value(targetIndex);
                        if (rawId == null || rawId == DBNull.Value || String.IsNullOrWhiteSpace(Convert.ToString(rawId, CultureInfo.InvariantCulture))) throw new InvalidDataException("Boş gözlem kimliği bulundu.");
                        string id = Convert.ToString(rawId, CultureInfo.InvariantCulture);
                        if (!ids.Add(id)) throw new InvalidDataException("Yinelenen gözlem kimliği: " + id);
                        if (rawTarget == null || rawTarget == DBNull.Value) throw new InvalidDataException("Hedef değeri eksik: " + id);
                        double target = Convert.ToDouble(rawTarget, CultureInfo.InvariantCulture);
                        if (Double.IsNaN(target) || Double.IsInfinity(target)) throw new InvalidDataException("Sonlu olmayan hedef: " + id);
                        var point = feature.Shape as IPoint;
                        if (point == null || point.IsEmpty) throw new InvalidDataException("Boş veya geçersiz nokta: " + id);
                        if (Double.IsNaN(point.X) || Double.IsNaN(point.Y) || Double.IsInfinity(point.X) || Double.IsInfinity(point.Y)) throw new InvalidDataException("Geçersiz koordinat: " + id);
                        var props = new Dictionary<string, object>();
                        for (int fieldIndex = 0; fieldIndex < featureClass.Fields.FieldCount; ++fieldIndex)
                        {
                            var field = featureClass.Fields.get_Field(fieldIndex);
                            if (field.Type == esriFieldType.esriFieldTypeGeometry) continue;
                            props[field.Name] = SerializableProperty(feature.get_Value(fieldIndex), field.Name);
                        }
                        props[idField] = id.Trim(); props[targetField] = target;
                        features.Add(new { type = "Feature", geometry = new { type = "Point", coordinates = new[] { point.X, point.Y } }, properties = props });
                    }
                    finally { Marshal.ReleaseComObject(feature); }
                }
            }
            finally { if (cursor != null) Marshal.ReleaseComObject(cursor); }
            if (features.Count < 2) throw new InvalidDataException("En az iki geçerli gözlem gerekir.");
            Json.Write(path, new { type = "FeatureCollection", crs = new { type = "name", properties = new { name = wkt } }, features = features });
            return new ExportedSamples { Count = features.Count, Crs = wkt };
        }
        private static object SerializableProperty(object value, string field)
        {
            if (value == null || value == DBNull.Value) return null;
            if (value is DateTime) return ((DateTime)value).ToString("O", CultureInfo.InvariantCulture);
            if (value is Guid) return value.ToString();
            if (value is byte[]) return Convert.ToBase64String((byte[])value);
            if (value is string || value is bool || value is byte || value is short || value is int || value is long || value is float || value is double || value is decimal) return value;
            throw new InvalidDataException("Alan değeri güvenle aktarılamıyor: " + field + ". Desteklenen öznitelik türlerini içeren bir çalışma kopyası kullanın.");
        }
        internal string[] SampleIds(PointLayerInfo info, string idField, bool selectedOnly)
        {
            AssertThread();
            int index = info.Layer.FeatureClass.FindField(idField); if (index < 0) throw new InvalidDataException("Gözlem kimliği alanı yok.");
            ICursor cursor;
            if (selectedOnly) ((IFeatureSelection)info.Layer).SelectionSet.Search(null, false, out cursor);
            else cursor = (ICursor)info.Layer.Search(null, false);
            var ids = new HashSet<string>(StringComparer.Ordinal);
            try { IRow row; while ((row = cursor.NextRow()) != null) { try { string id = StableSampleId(row.get_Value(index)); if (!ids.Add(id)) throw new InvalidDataException("Yinelenen gözlem kimliği: " + id); } finally { Marshal.ReleaseComObject(row); } } }
            finally { Marshal.ReleaseComObject(cursor); }
            return ids.OrderBy(v => v, StringComparer.Ordinal).ToArray();
        }
        internal string ScopeStamp(PointLayerInfo info, bool selectedOnly)
        {
            AssertThread();
            var definition = info.Layer as IFeatureLayerDefinition;
            var objectIds = new List<int>();
            if (selectedOnly)
            {
                var selection = ((IFeatureSelection)info.Layer).SelectionSet;
                if (selection != null) { var ids = selection.IDs; ids.Reset(); int id; while ((id = ids.Next()) >= 0) objectIds.Add(id); }
            }
            return ConfigurationIdentity.Digest(new { source = info.SourcePath, query = definition?.DefinitionExpression ?? "", selected = selectedOnly, ids = objectIds.OrderBy(i => i).ToArray() });
        }
        internal void AddRaster(string path, string name)
        {
            var layer = new RasterLayerClass(); layer.CreateFromFilePath(path); layer.Name = name;
            if (!layer.Valid) throw new InvalidDataException("ArcMap rasteri açamadı: " + path);
            Map.AddLayer(layer); Refresh();
        }
        internal void AddPointSource(string path, string name)
        {
            var layer = CreatePointLayer(path, name);
            Map.AddLayer(layer); Refresh();
        }
        private IFeatureLayer CreatePointLayer(string path, string name)
        {
            AssertThread();
            IWorkspaceFactory factory;
            string workspacePath, featureName;
            int gdb = path.IndexOf(".gdb", StringComparison.OrdinalIgnoreCase);
            if (gdb >= 0) { workspacePath = path.Substring(0, gdb + 4); featureName = path.Substring(gdb + 4).TrimStart('\\', '/'); factory = new FileGDBWorkspaceFactoryClass(); }
            else { workspacePath = System.IO.Path.GetDirectoryName(path); featureName = System.IO.Path.GetFileName(path); factory = new ShapefileWorkspaceFactoryClass(); }
            var workspace = factory.OpenFromFile(workspacePath, application.hWnd);
            var fc = ((IFeatureWorkspace)workspace).OpenFeatureClass(featureName);
            var layer = new FeatureLayerClass { FeatureClass = fc, Name = name };
            return layer;
        }
        internal PointScope CapturePointScope(PointLayerInfo point, string idField, bool selectedOnly)
        {
            AssertThread();
            var definition = point.Layer as IFeatureLayerDefinition;
            if (definition != null && definition.DefinitionSelectionSet != null && definition.DefinitionSelectionSet.Count > 0)
                throw new InvalidOperationException("Seçimden oluşturulmuş katmanlar Aşama A proje kaydında desteklenmiyor. Kalıcı bir nokta çalışma kopyası kullanın.");
            var scope = new PointScope { DefinitionExpression = definition == null ? "" : definition.DefinitionExpression ?? "", SelectedOnly = selectedOnly };
            if (!selectedOnly) return scope;
            int index = point.Layer.FeatureClass.FindField(idField);
            if (index < 0) throw new InvalidDataException("Gözlem kimliği alanı kaynakta bulunamadı.");
            var selection = ((IFeatureSelection)point.Layer).SelectionSet;
            if (selection == null || selection.Count == 0) throw new InvalidDataException("Seçili kayıt modundaki proje için en az bir nokta seçilmelidir.");
            ICursor selectedCursor;
            selection.Search(null, false, out selectedCursor);
            var selectedIds = new HashSet<string>(StringComparer.Ordinal);
            try
            {
                IRow row;
                while ((row = selectedCursor.NextRow()) != null)
                {
                    try { if (!selectedIds.Add(StableSampleId(row.get_Value(index)))) throw new InvalidDataException("Seçimde yinelenen gözlem kimliği var."); }
                    finally { Marshal.ReleaseComObject(row); }
                }
            }
            finally { Marshal.ReleaseComObject(selectedCursor); }
            // Verify that selected identities resolve uniquely inside the layer's current query.
            ResolveSelection(point.Layer, idField, selectedIds);
            scope.SelectedIds = selectedIds.OrderBy(v => v, StringComparer.Ordinal).ToArray();
            return scope;
        }
        internal PointLayerInfo RestorePointScope(string source, string name, string idField, PointScope scope)
        {
            AssertThread();
            var layer = CreatePointLayer(source, name + " - proje " + DateTime.Now.ToString("HHmmss"));
            ((IFeatureLayerDefinition)layer).DefinitionExpression = scope.DefinitionExpression;
            // Query validation happens on the dedicated layer before adding it to the map.
            if (scope.SelectedOnly)
            {
                var objectIds = ResolveSelection(layer, idField, new HashSet<string>(scope.SelectedIds, StringComparer.Ordinal));
                var selection = (IFeatureSelection)layer;
                selection.SelectionSet = layer.FeatureClass.Select(null, esriSelectionType.esriSelectionTypeIDSet, esriSelectionOption.esriSelectionOptionEmpty, null);
                foreach (int objectId in objectIds) selection.SelectionSet.Add(objectId);
                selection.SelectionChanged();
            }
            else
            {
                IFeatureCursor cursor = layer.Search(null, false);
                try { var first = cursor.NextFeature(); if (first != null) Marshal.ReleaseComObject(first); }
                finally { Marshal.ReleaseComObject(cursor); }
            }
            var restored = new PointLayerInfo { Layer = layer, Name = layer.Name, SourcePath = source };
            if (!CapturePointScope(restored, idField, scope.SelectedOnly).EquivalentTo(scope))
                throw new InvalidDataException("Projedeki gözlem kapsamı geri okuma kontrolünü geçmedi.");
            Map.AddLayer(layer); Refresh();
            return restored;
        }
        private static List<int> ResolveSelection(IFeatureLayer layer, string idField, HashSet<string> desired)
        {
            int index = layer.FeatureClass.FindField(idField);
            if (index < 0) throw new InvalidDataException("Projedeki gözlem kimliği alanı kaynakta yok.");
            var found = new HashSet<string>(StringComparer.Ordinal);
            var objectIds = new List<int>();
            IFeatureCursor cursor = layer.Search(null, false);
            try
            {
                IFeature feature;
                while ((feature = cursor.NextFeature()) != null)
                {
                    try
                    {
                        string id = StableSampleId(feature.get_Value(index));
                        if (!desired.Contains(id)) continue;
                        if (!found.Add(id)) throw new InvalidDataException("Kaynakta seçili kimlik birden fazla noktaya karşılık geliyor: " + id);
                        objectIds.Add(feature.OID);
                    }
                    finally { Marshal.ReleaseComObject(feature); }
                }
            }
            finally { Marshal.ReleaseComObject(cursor); }
            if (found.Count != desired.Count) throw new InvalidDataException("Kaydedilmiş seçili gözlemlerden bazıları kaynakta veya tanım sorgusunun kapsamında yok.");
            return objectIds;
        }
        private static string StableSampleId(object value)
        {
            if (value == null || value == DBNull.Value) throw new InvalidDataException("Boş gözlem kimliği bulundu.");
            string id = Convert.ToString(value, CultureInfo.InvariantCulture).Trim();
            if (id.Length == 0) throw new InvalidDataException("Boş gözlem kimliği bulundu.");
            return id;
        }
        internal string LoadExamples(string examplesPath, string destination)
        {
            AssertThread();
            var source = System.IO.Path.Combine(examplesPath, "samples.geojson");
            var rasterPath = System.IO.Path.Combine(examplesPath, "covariates.tif");
            if (!File.Exists(source) || !File.Exists(rasterPath)) throw new FileNotFoundException("Örnek nokta veya raster dosyası bulunamadı.");
            var document = Json.Read(source);
            string crs = "EPSG:4326";
            object crsObject;
            if (document.TryGetValue("crs", out crsObject) && crsObject is Dictionary<string, object>)
            {
                var crsMap = (Dictionary<string, object>)crsObject;
                var properties = crsMap["properties"] as Dictionary<string, object>;
                if (properties != null) crs = Json.Text(properties, "name", crs);
            }
            ISpatialReference spatialReference;
            var srFactory = new SpatialReferenceEnvironmentClass();
            int code;
            if (Int32.TryParse(crs.Substring(crs.LastIndexOf(':') + 1), out code)) spatialReference = ((ISpatialReferenceFactory3)srFactory).CreateSpatialReference(code);
            else spatialReference = srFactory.CreateESRISpatialReferenceFromPRJ(crs);
            Directory.CreateDirectory(destination);
            var workspace = new ShapefileWorkspaceFactoryClass().OpenFromFile(destination, application.hWnd);
            var fields = new FieldsClass(); var fieldsEdit = (IFieldsEdit)fields;
            var geometry = new GeometryDefClass(); var geometryEdit = (IGeometryDefEdit)geometry;
            geometryEdit.GeometryType_2 = esriGeometryType.esriGeometryPoint; geometryEdit.SpatialReference_2 = spatialReference;
            var shape = new FieldClass(); var shapeEdit = (IFieldEdit)shape;
            shapeEdit.Name_2 = "Shape"; shapeEdit.Type_2 = esriFieldType.esriFieldTypeGeometry; shapeEdit.GeometryDef_2 = geometry; fieldsEdit.AddField(shape);
            var id = new FieldClass(); var idEdit = (IFieldEdit)id; idEdit.Name_2 = "sample_id"; idEdit.Type_2 = esriFieldType.esriFieldTypeString; idEdit.Length_2 = 100; fieldsEdit.AddField(id);
            var target = new FieldClass(); var targetEdit = (IFieldEdit)target; targetEdit.Name_2 = "soil_value"; targetEdit.Type_2 = esriFieldType.esriFieldTypeDouble; fieldsEdit.AddField(target);
            var fc = ((IFeatureWorkspace)workspace).CreateFeatureClass("ornek_noktalar.shp", fields, null, null, esriFeatureType.esriFTSimple, "Shape", "");
            foreach (var featureObject in (IEnumerable)document["features"])
            {
                var item = (Dictionary<string, object>)featureObject;
                var props = (Dictionary<string, object>)item["properties"];
                var geom = (Dictionary<string, object>)item["geometry"];
                if (Json.Text(geom, "type") != "Point") throw new InvalidDataException("Örnek dosyada nokta dışı geometri var.");
                var coordinates = ((IEnumerable)geom["coordinates"]).Cast<object>().ToArray();
                var feature = fc.CreateFeature();
                try {
                    var point = new PointClass(); point.PutCoords(Convert.ToDouble(coordinates[0]), Convert.ToDouble(coordinates[1])); point.SpatialReference = spatialReference;
                    feature.Shape = point; feature.set_Value(fc.FindField("sample_id"), props["sample_id"]); feature.set_Value(fc.FindField("soil_value"), props["soil_value"]); feature.Store();
                } finally { Marshal.ReleaseComObject(feature); }
            }
            var group = new GroupLayerClass { Name = "Spatial Prediction Studio örnekleri" };
            var pointLayer = new FeatureLayerClass { FeatureClass = fc, Name = "Örnek noktalar" }; group.Add(pointLayer);
            var rasterLayer = new RasterLayerClass(); rasterLayer.CreateFromFilePath(rasterPath); rasterLayer.Name = "Örnek çevresel değişkenler"; group.Add(rasterLayer);
            Map.AddLayer(group);
            ((IMxDocument)application.Document).ActiveView.Extent = ((IGeoDataset)fc).Extent;
            Refresh();
            return System.IO.Path.Combine(destination, "ornek_noktalar.shp");
        }
        internal int AddResult(Dictionary<string, object> result)
        {
            AssertThread();
            if (Json.Text(result, "status") != "completed") throw new InvalidDataException("Tamamlanmamış sonuç haritaya eklenemez.");
            if (!result.ContainsKey("schema_version") || (Convert.ToInt32(result["schema_version"]) != 1 && Convert.ToInt32(result["schema_version"]) != 2) || String.IsNullOrWhiteSpace(Json.Text(result, "run_id")))
                throw new InvalidDataException("Sonuç bildirimi sürümü veya çalışma kimliği geçersiz.");
            var manifestDirectory = System.IO.Path.GetDirectoryName(Json.Text(result, "manifest_path"));
            string prefix = System.IO.Path.GetFullPath(manifestDirectory).TrimEnd('\\', '/') + System.IO.Path.DirectorySeparatorChar;
            var group = new GroupLayerClass { Name = "SPS " + Json.Text(result, "run_id") + (Convert.ToInt32(result["schema_version"]) == 1 ? " - Aşama A" : " - tahmin ve tanılar") };
            int count = 0;
            foreach (var outputObject in (IEnumerable)result["outputs"])
            {
                var output = (Dictionary<string, object>)outputObject;
                string kind = Json.Text(output, "kind");
                if (!RasterLabels.ContainsKey(kind)) continue;
                var path = Json.Text(output, "path");
                path = System.IO.Path.GetFullPath(System.IO.Path.IsPathRooted(path) ? path : System.IO.Path.Combine(manifestDirectory, path));
                if (!path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Sonuç dosyası iş dizininin dışında: " + path);
                if (!File.Exists(path)) throw new FileNotFoundException("Sonuç rasteri bulunamadı.", path);
                ArtifactIntegrity.VerifySha256(path, Json.Text(output, "sha256"));
                var layer = new RasterLayerClass(); layer.CreateFromFilePath(path); layer.Name = RasterLabels[kind];
                if (!layer.Valid) throw new InvalidDataException("Sonuç rasteri ArcMap tarafından açılamadı.");
                StyleRaster(layer, kind);
                group.Add(layer); ++count;
            }
            if (count == 0) throw new InvalidDataException("Tamamlanmış bildirimde tahmin rasteri yok.");
            Map.AddLayer(group); Refresh();
            if (!String.Equals(Map.get_Layer(0).Name, group.Name, StringComparison.Ordinal)) throw new InvalidOperationException("Sonuç grubu harita üzerinden geri okunamadı.");
            return count;
        }
        private static readonly Dictionary<string, string> RasterLabels = new Dictionary<string, string> {
            { "prediction", "Nihai tahmin" }, { "lower", "Tahmin aralığı alt sınırı" }, { "upper", "Tahmin aralığı üst sınırı" }, { "width", "Tahmin aralığı genişliği" },
            { "nearest_distance", "En yakın eğitim konumuna uzaklık" }, { "range_extrapolation", "Eğitim aralığının dışına taşma" }, { "unknown_category", "Eğitimde görülmeyen kategori" }, { "physical_violation", "Fiziksel sınır dışı tahmin" }, { "quantile_crossing", "Kantil sınırlarının kesişmesi" },
            { "applicability", "Uygulanabilirlik alanı (1: içinde, 0: dışında)" }, { "dissimilarity", "Çevresel benzemezlik (artan değer: daha farklı)" },
            { "interval_available", "Yerel tahmin aralığı (1: var, 0: yok)" },
            { "interval_effective_n", "Yerel kalibrasyonda etkin gözlem sayısı" },
            { "interval_effective_group_n", "Yerel kalibrasyonda etkin grup sayısı" }
        };
        private static void StyleRaster(IRasterLayer layer, string kind)
        {
            var stretch = new RasterStretchColorRampRendererClass(); var renderer = (IRasterRenderer)stretch; renderer.Raster = layer.Raster;
            var ramp = new AlgorithmicColorRampClass { FromColor = kind == "interval_available" ? Rgb(180, 180, 180) : kind == "applicability" ? Rgb(200, 75, 30) : Rgb(245, 249, 250), ToColor = kind == "interval_available" ? Rgb(0, 114, 178) : kind == "applicability" ? Rgb(0, 125, 85) : kind == "dissimilarity" ? Rgb(104, 52, 150) : kind == "prediction" ? Rgb(22, 105, 115) : kind == "lower" || kind == "upper" ? Rgb(63, 81, 181) : Rgb(200, 75, 30), Size = 256 };
            bool created; ramp.CreateRamp(out created); if (!created) throw new InvalidDataException("Raster renk geçişi oluşturulamadı.");
            stretch.BandIndex = 0; stretch.ColorRamp = ramp;
            if (kind == "applicability" || kind == "interval_available") {
                ((IRasterStretch)stretch).StretchType = esriRasterStretchTypesEnum.esriRasterStretch_MinimumMaximum;
                var range = (IRasterStretchMinMax)stretch; range.CustomStretchMin = 0; range.CustomStretchMax = 1; range.UseCustomStretchMinMax = true;
            }
            renderer.Update();
            if (kind == "applicability") {
                stretch.LabelLow = "0 - Alan dışında"; stretch.LabelMedium = ""; stretch.LabelHigh = "1 - Alan içinde";
                var labels = (IRasterStretchAdvancedLabels)stretch; labels.NumLabels = 2; labels.set_LabelValue(0, 0); labels.set_LabelText(0, "0 - Alan dışında"); labels.set_LabelValue(1, 1); labels.set_LabelText(1, "1 - Alan içinde"); labels.UseAdvancedLabeling = true;
            }
            else if (kind == "interval_available") {
                stretch.LabelLow = "0 - Aralık yok"; stretch.LabelMedium = ""; stretch.LabelHigh = "1 - Aralık var";
                var labels = (IRasterStretchAdvancedLabels)stretch; labels.NumLabels = 2; labels.set_LabelValue(0, 0); labels.set_LabelText(0, "0 - Aralık yok"); labels.set_LabelValue(1, 1); labels.set_LabelText(1, "1 - Aralık var"); labels.UseAdvancedLabeling = true;
            }
            else if (kind == "dissimilarity") { stretch.LabelLow += " (daha benzer)"; stretch.LabelHigh += " (daha farklı)"; }
            layer.Renderer = renderer;
        }
        private static IColor Rgb(int red, int green, int blue) { return new RgbColorClass { Red = red, Green = green, Blue = blue }; }
        internal int AddPreview(Dictionary<string, object> result, string destination)
        {
            AssertThread();
            if (Json.Text(result, "status") != "completed" || Convert.ToInt32(result["schema_version"]) != 2 || String.IsNullOrWhiteSpace(Json.Text(result, "preview_fingerprint"))) throw new InvalidDataException("Geçerli kat ön gösterimi yok.");
            string manifestDirectory = System.IO.Path.GetDirectoryName(Json.Text(result, "manifest_path"));
            var output = ((IEnumerable)result["outputs"]).Cast<Dictionary<string, object>>().SingleOrDefault(o => Json.Text(o, "kind") == "folds");
            if (output == null) throw new InvalidDataException("Ön gösterimde kat geometrileri bulunamadı.");
            string path = ArtifactIntegrity.ResolveOutputPath(manifestDirectory, Json.Text(output, "path"));
            ArtifactIntegrity.VerifySha256(path, Json.Text(output, "sha256"));
            var document = Json.Read(path); Directory.CreateDirectory(destination);
            var workspace = new ShapefileWorkspaceFactoryClass().OpenFromFile(destination, application.hWnd);
            var fields = new FieldsClass(); var edit = (IFieldsEdit)fields;
            var geometry = new GeometryDefClass(); ((IGeometryDefEdit)geometry).GeometryType_2 = esriGeometryType.esriGeometryPoint;
            ((IGeometryDefEdit)geometry).SpatialReference_2 = new SpatialReferenceEnvironmentClass().CreateGeographicCoordinateSystem(4326);
            var shape = new FieldClass(); ((IFieldEdit)shape).Name_2 = "Shape"; ((IFieldEdit)shape).Type_2 = esriFieldType.esriFieldTypeGeometry; ((IFieldEdit)shape).GeometryDef_2 = geometry; edit.AddField(shape);
            foreach (string fieldName in new[] { "sample_id", "role", "classkey" }) { var field = new FieldClass(); var fe = (IFieldEdit)field; fe.Name_2 = fieldName; fe.Type_2 = esriFieldType.esriFieldTypeString; fe.Length_2 = fieldName == "sample_id" ? 180 : 80; edit.AddField(field); }
            foreach (string fieldName in new[] { "rep_idx", "fold_idx" }) { var field = new FieldClass(); ((IFieldEdit)field).Name_2 = fieldName; ((IFieldEdit)field).Type_2 = esriFieldType.esriFieldTypeInteger; edit.AddField(field); }
            var fc = ((IFeatureWorkspace)workspace).CreateFeatureClass("dogrulama_katlari.shp", fields, null, null, esriFeatureType.esriFTSimple, "Shape", "");
            var repetitions = new SortedSet<int>(); var symbols = new Dictionary<string, string>(); int count = 0;
            foreach (var raw in (IEnumerable)document["features"])
            {
                var item = (Dictionary<string, object>)raw; var props = (Dictionary<string, object>)item["properties"]; var geom = (Dictionary<string, object>)item["geometry"];
                if (Json.Text(geom, "type") != "Point") throw new InvalidDataException("Kat ön gösterimi nokta geometrisi olmalıdır.");
                var xy = ((IEnumerable)geom["coordinates"]).Cast<object>().ToArray(); int repeat = Convert.ToInt32(props["repeat"]), fold = Convert.ToInt32(props["fold"]); string role = Json.Text(props, "role");
                string key = repeat + ":" + fold + ":" + role; repetitions.Add(repeat); symbols[key] = role == "independent" ? "Bağımsız test" : role == "initial_training" ? "İlk eğitim dönemi" : "Kat " + (fold + 1) + " / " + (role == "test" ? "sınama" : role == "train" ? "eğitim" : role == "excluded" ? "bu katta dışarıda" : role);
                var feature = fc.CreateFeature();
                try { var point = new PointClass(); point.PutCoords(Convert.ToDouble(xy[0]), Convert.ToDouble(xy[1])); point.SpatialReference = ((IGeoDataset)fc).SpatialReference; feature.Shape = point; feature.set_Value(fc.FindField("sample_id"), Json.Text(props, "sample_id")); feature.set_Value(fc.FindField("role"), role); feature.set_Value(fc.FindField("classkey"), key); feature.set_Value(fc.FindField("rep_idx"), repeat); feature.set_Value(fc.FindField("fold_idx"), fold); feature.Store(); ++count; }
                finally { Marshal.ReleaseComObject(feature); }
            }
            if (count == 0) throw new InvalidDataException("Ön gösterim katları boş.");
            var group = new GroupLayerClass { Name = "SPS kat ön gösterimi " + Json.Text(result, "run_id") };
            int[][] palette = { new[] { 0, 114, 178 }, new[] { 213, 94, 0 }, new[] { 0, 158, 115 }, new[] { 204, 121, 167 }, new[] { 230, 159, 0 }, new[] { 86, 180, 233 } };
            foreach (int repeat in repetitions)
            {
                var layer = new FeatureLayerClass { FeatureClass = fc, Name = repeat < 0 ? "Bağımsız test" : "Tekrar " + (repeat + 1) }; ((IFeatureLayerDefinition)layer).DefinitionExpression = "rep_idx = " + repeat;
                var renderer = new UniqueValueRendererClass { FieldCount = 1, UseDefaultSymbol = false }; renderer.set_Field(0, "classkey");
                foreach (var pair in symbols.Where(p => p.Key.StartsWith(repeat + ":", StringComparison.Ordinal)))
                {
                    var parts = pair.Key.Split(':'); int fold = Int32.Parse(parts[1], CultureInfo.InvariantCulture); var color = palette[Math.Abs(fold) % palette.Length];
                    var symbol = new SimpleMarkerSymbolClass { Style = parts[2] == "train" ? esriSimpleMarkerStyle.esriSMSCircle : esriSimpleMarkerStyle.esriSMSDiamond, Size = parts[2] == "train" ? 4 : 7, Color = Rgb(color[0], color[1], color[2]) };
                    renderer.AddValue(pair.Key, "", (ISymbol)symbol); renderer.set_Label(pair.Key, pair.Value);
                }
                ((IGeoFeatureLayer)layer).Renderer = renderer; group.Add(layer);
            }
            Map.AddLayer(group); Refresh(); if (Map.get_Layer(0).Name != group.Name) throw new InvalidOperationException("Kat ön gösterimi haritadan geri okunamadı."); return count;
        }
        private void Refresh() { var doc = (IMxDocument)application.Document; doc.UpdateContents(); doc.ActiveView.Refresh(); }
        private static string StableId(string path, int band)
        {
            using (var hash = SHA256.Create()) return "r_" + BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(System.IO.Path.GetFullPath(path).ToUpperInvariant() + "|" + band))).Replace("-", "").Substring(0, 16).ToLowerInvariant();
        }
    }
}
