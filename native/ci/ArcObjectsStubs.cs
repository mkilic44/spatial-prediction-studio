// Compile-check stand-ins for the ArcObjects members that Spatial Prediction Studio uses.
// These are NOT Esri assemblies and are never shipped or loaded by ArcMap.
// They let continuous integration compile the add-in and run the native contract tests
// on machines without ArcGIS Desktop. Signatures follow the ArcObjects 10.8 .NET API.
using System;

namespace ESRI.ArcGIS.esriSystem
{
    public interface UID { object Value { get; set; } }
    public class UIDClass : UID { public object Value { get; set; } }
}

namespace ESRI.ArcGIS.Framework
{
    public interface IDocument { }
    public interface IApplication { IDocument Document { get; } int hWnd { get; } }
    public interface IDockableWindow { void Show(bool Show); }
    public interface IDockableWindowManager { IDockableWindow GetDockableWindow(ESRI.ArcGIS.esriSystem.UID dockWindowID); }
}

namespace ESRI.ArcGIS.Desktop.AddIns
{
    public abstract class Button
    {
        protected object Hook { get { return null; } }
        public bool Enabled { get; set; }
        protected virtual void OnClick() { }
        protected virtual void OnUpdate() { }
    }
    public abstract class DockableWindow : IDisposable
    {
        protected object Hook { get { return null; } }
        protected virtual IntPtr OnCreateChild() { return IntPtr.Zero; }
        protected virtual void Dispose(bool disposing) { }
        public void Dispose() { Dispose(true); }
    }
}

namespace ESRI.ArcGIS.Geometry
{
    public enum esriGeometryType { esriGeometryNull = 0, esriGeometryPoint = 1 }
    public interface ISpatialReference { }
    public interface IUnknownCoordinateSystem : ISpatialReference { }
    public interface IGeographicCoordinateSystem : ISpatialReference { }
    public interface IESRISpatialReferenceGEN { void ExportToESRISpatialReference(out string buffer, out int bytes); }
    public interface ISpatialReferenceFactory3 { ISpatialReference CreateSpatialReference(int factoryCode); }
    public class SpatialReferenceEnvironmentClass : ISpatialReferenceFactory3
    {
        public ISpatialReference CreateSpatialReference(int factoryCode) { return null; }
        public IGeographicCoordinateSystem CreateGeographicCoordinateSystem(int gcsType) { return null; }
        public ISpatialReference CreateESRISpatialReferenceFromPRJ(string prj) { return null; }
    }
    public interface IEnvelope { }
    public interface IGeometry { ISpatialReference SpatialReference { get; set; } bool IsEmpty { get; } }
    public interface IPoint : IGeometry { double X { get; set; } double Y { get; set; } void PutCoords(double X, double Y); }
    public class PointClass : IPoint
    {
        public ISpatialReference SpatialReference { get; set; }
        public bool IsEmpty { get { return false; } }
        public double X { get; set; }
        public double Y { get; set; }
        public void PutCoords(double X, double Y) { }
    }
}

namespace ESRI.ArcGIS.Geodatabase
{
    using ESRI.ArcGIS.Geometry;
    public enum esriFieldType
    {
        esriFieldTypeSmallInteger = 0, esriFieldTypeInteger = 1, esriFieldTypeSingle = 2, esriFieldTypeDouble = 3, esriFieldTypeString = 4,
        esriFieldTypeDate = 5, esriFieldTypeOID = 6, esriFieldTypeGeometry = 7, esriFieldTypeBlob = 8, esriFieldTypeRaster = 9,
        esriFieldTypeGUID = 10, esriFieldTypeGlobalID = 11, esriFieldTypeXML = 12
    }
    public enum esriFeatureType { esriFTSimple = 1 }
    public enum esriSelectionType { esriSelectionTypeIDSet = 1, esriSelectionTypeSnapshot = 2, esriSelectionTypeHybrid = 3 }
    public enum esriSelectionOption { esriSelectionOptionNormal = 1, esriSelectionOptionOnlyOne = 2, esriSelectionOptionEmpty = 3 }
    public interface IQueryFilter { }
    public interface IWorkspace { string PathName { get; } }
    public interface IWorkspaceEdit { bool IsBeingEdited(); }
    public interface IWorkspaceFactory { IWorkspace OpenFromFile(string fileName, int hWnd); }
    public interface IDataset { IWorkspace Workspace { get; } string Name { get; } }
    public interface IGeoDataset { ISpatialReference SpatialReference { get; } IEnvelope Extent { get; } }
    public interface IField { string Name { get; } esriFieldType Type { get; } }
    public interface IFieldEdit : IField { string Name_2 { set; } esriFieldType Type_2 { set; } int Length_2 { set; } IGeometryDef GeometryDef_2 { set; } }
    public interface IFields { int FieldCount { get; } IField get_Field(int Index); }
    public interface IFieldsEdit : IFields { void AddField(IField Field); }
    public interface IGeometryDef { }
    public interface IGeometryDefEdit : IGeometryDef { esriGeometryType GeometryType_2 { set; } ISpatialReference SpatialReference_2 { set; } }
    public class FieldClass : IFieldEdit
    {
        public string Name { get { return null; } } public esriFieldType Type { get { return 0; } }
        public string Name_2 { set { } } public esriFieldType Type_2 { set { } } public int Length_2 { set { } } public IGeometryDef GeometryDef_2 { set { } }
    }
    public class FieldsClass : IFieldsEdit { public int FieldCount { get { return 0; } } public IField get_Field(int Index) { return null; } public void AddField(IField Field) { } }
    public class GeometryDefClass : IGeometryDefEdit { public esriGeometryType GeometryType_2 { set { } } public ISpatialReference SpatialReference_2 { set { } } }
    public interface IRow { object get_Value(int Index); void set_Value(int Index, object Value); void Store(); int OID { get; } }
    public interface IFeature : IRow { IGeometry Shape { get; set; } }
    public interface ICursor { IRow NextRow(); }
    public interface IFeatureCursor { IFeature NextFeature(); }
    public interface IEnumIDs { int Next(); void Reset(); }
    public interface ISelectionSet { void Search(IQueryFilter QueryFilter, bool Recycling, out ICursor Cursor); IEnumIDs IDs { get; } int Count { get; } void Add(int OID); }
    public interface IRelationshipClass { }
    public interface IFeatureClass
    {
        esriGeometryType ShapeType { get; }
        IFields Fields { get; }
        int FindField(string Name);
        IFeature CreateFeature();
        IFeatureCursor Search(IQueryFilter filter, bool Recycling);
        ISelectionSet Select(IQueryFilter QueryFilter, esriSelectionType selType, esriSelectionOption selOption, IWorkspace selectionContainer);
    }
    public interface IFeatureWorkspace
    {
        IFeatureClass OpenFeatureClass(string Name);
        IFeatureClass CreateFeatureClass(string Name, IFields Fields, ESRI.ArcGIS.esriSystem.UID CLSID, ESRI.ArcGIS.esriSystem.UID EXTCLSID, esriFeatureType FeatureType, string ShapeFieldName, string ConfigKeyword);
    }
}

namespace ESRI.ArcGIS.DataSourcesFile
{
    public class ShapefileWorkspaceFactoryClass : ESRI.ArcGIS.Geodatabase.IWorkspaceFactory
    { public ESRI.ArcGIS.Geodatabase.IWorkspace OpenFromFile(string fileName, int hWnd) { return null; } }
}

namespace ESRI.ArcGIS.DataSourcesGDB
{
    public class FileGDBWorkspaceFactoryClass : ESRI.ArcGIS.Geodatabase.IWorkspaceFactory
    { public ESRI.ArcGIS.Geodatabase.IWorkspace OpenFromFile(string fileName, int hWnd) { return null; } }
}

namespace ESRI.ArcGIS.Display
{
    public interface IColor { }
    public interface IRgbColor : IColor { int Red { get; set; } int Green { get; set; } int Blue { get; set; } }
    public class RgbColorClass : IRgbColor { public int Red { get; set; } public int Green { get; set; } public int Blue { get; set; } }
    public enum esriColorRampAlgorithm { esriHSVAlgorithm = 0, esriCIELabAlgorithm = 1, esriLabLChAlgorithm = 2 }
    public interface IColorRamp { int Size { get; set; } void CreateRamp(out bool ok); }
    public class AlgorithmicColorRampClass : IColorRamp
    {
        public IColor FromColor { get; set; } public IColor ToColor { get; set; } public esriColorRampAlgorithm Algorithm { get; set; }
        public int Size { get; set; } public void CreateRamp(out bool ok) { ok = true; }
    }
    public interface ISymbol { }
    public enum esriSimpleMarkerStyle { esriSMSCircle = 0, esriSMSSquare = 1, esriSMSCross = 2, esriSMSX = 3, esriSMSDiamond = 4 }
    public class SimpleMarkerSymbolClass : ISymbol { public esriSimpleMarkerStyle Style { get; set; } public double Size { get; set; } public IColor Color { get; set; } }
}

namespace ESRI.ArcGIS.Carto
{
    using ESRI.ArcGIS.Geodatabase;
    using ESRI.ArcGIS.Geometry;
    using ESRI.ArcGIS.Display;
    public interface ILayer { string Name { get; set; } bool Valid { get; } }
    public interface ICompositeLayer { int Count { get; } ILayer get_Layer(int Index); }
    public interface IMap { int LayerCount { get; } ILayer get_Layer(int Index); void AddLayer(ILayer Layer); }
    public interface IActiveView { IEnvelope Extent { get; set; } void Refresh(); }
    public interface IFeatureLayer : ILayer { IFeatureClass FeatureClass { get; set; } IFeatureCursor Search(IQueryFilter QueryFilter, bool Recycling); }
    public interface IFeatureSelection { ISelectionSet SelectionSet { get; set; } void SelectionChanged(); }
    public interface IFeatureLayerDefinition { string DefinitionExpression { get; set; } IRelationshipClass RelationshipClass { get; set; } ISelectionSet DefinitionSelectionSet { get; } }
    public interface IFeatureRenderer { }
    public interface IGeoFeatureLayer : IFeatureLayer { IFeatureRenderer Renderer { get; set; } }
    public class FeatureLayerClass : IGeoFeatureLayer, IFeatureSelection, IFeatureLayerDefinition
    {
        public string Name { get; set; } public bool Valid { get { return true; } } public IFeatureClass FeatureClass { get; set; }
        public IFeatureCursor Search(IQueryFilter QueryFilter, bool Recycling) { return null; }
        public IFeatureRenderer Renderer { get; set; } public ISelectionSet SelectionSet { get; set; } public void SelectionChanged() { }
        public string DefinitionExpression { get; set; } public IRelationshipClass RelationshipClass { get; set; } public ISelectionSet DefinitionSelectionSet { get { return null; } }
    }
    public class GroupLayerClass : ILayer, ICompositeLayer
    {
        public string Name { get; set; } public bool Valid { get { return true; } } public bool Expanded { get; set; }
        public int Count { get { return 0; } } public ILayer get_Layer(int Index) { return null; } public void Add(ILayer Layer) { }
    }
    public class UniqueValueRendererClass : IFeatureRenderer
    {
        public int FieldCount { get; set; } public bool UseDefaultSymbol { get; set; }
        public void set_Field(int Index, string Field) { } public void AddValue(string Value, string Heading, ISymbol Symbol) { } public void set_Label(string Value, string Label) { }
    }
    public interface IRaster { }
    public interface IRasterRenderer { IRaster Raster { get; set; } void Update(); }
    public enum esriRasterStretchTypesEnum { esriRasterStretch_NONE = 0, esriRasterStretch_DefaultFromSource = 1, esriRasterStretch_Custom = 2, esriRasterStretch_StandardDeviations = 3, esriRasterStretch_HistogramEqualize = 4, esriRasterStretch_MinimumMaximum = 5 }
    public interface IRasterStretch { esriRasterStretchTypesEnum StretchType { get; set; } }
    public interface IRasterStretchMinMax { double CustomStretchMin { get; set; } double CustomStretchMax { get; set; } bool UseCustomStretchMinMax { get; set; } }
    public interface IRasterStretchAdvancedLabels { int NumLabels { get; set; } void set_LabelValue(int Index, double Value); void set_LabelText(int Index, string Text); bool UseAdvancedLabeling { get; set; } }
    public class RasterStretchColorRampRendererClass : IRasterRenderer, IRasterStretch, IRasterStretchMinMax, IRasterStretchAdvancedLabels
    {
        public IRaster Raster { get; set; } public void Update() { }
        public int BandIndex { get; set; } public IColorRamp ColorRamp { get; set; }
        public string LabelLow { get; set; } public string LabelMedium { get; set; } public string LabelHigh { get; set; }
        public esriRasterStretchTypesEnum StretchType { get; set; }
        public double CustomStretchMin { get; set; } public double CustomStretchMax { get; set; } public bool UseCustomStretchMinMax { get; set; }
        public int NumLabels { get; set; } public void set_LabelValue(int Index, double Value) { } public void set_LabelText(int Index, string Text) { } public bool UseAdvancedLabeling { get; set; }
    }
    public interface IRasterLayer : ILayer { void CreateFromFilePath(string FilePath); string FilePath { get; } int BandCount { get; } IRaster Raster { get; } IRasterRenderer Renderer { get; set; } }
    public class RasterLayerClass : IRasterLayer
    {
        public string Name { get; set; } public bool Valid { get { return true; } } public void CreateFromFilePath(string FilePath) { }
        public string FilePath { get { return null; } } public int BandCount { get { return 1; } } public IRaster Raster { get { return null; } } public IRasterRenderer Renderer { get; set; }
    }
}

namespace ESRI.ArcGIS.ArcMapUI
{
    public interface IMxDocument : ESRI.ArcGIS.Framework.IDocument
    { ESRI.ArcGIS.Carto.IMap FocusMap { get; } ESRI.ArcGIS.Carto.IActiveView ActiveView { get; } void UpdateContents(); }
}
