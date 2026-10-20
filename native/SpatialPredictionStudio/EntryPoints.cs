using System;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using ESRI.ArcGIS.Framework;
using ESRI.ArcGIS.esriSystem;

[assembly: AssemblyTitle("Spatial Prediction Studio")]
[assembly: AssemblyDescription("ArcMap yerel mekansal tahmin paneli")]
[assembly: AssemblyVersion("0.5.0.0")]
[assembly: AssemblyFileVersion("0.5.0.0")]
[assembly: ComVisible(false)]

namespace SpatialPredictionStudio
{
    public sealed class OpenStudioButton : ESRI.ArcGIS.Desktop.AddIns.Button
    {
        protected override void OnClick()
        {
            try
            {
                var application = Hook as IApplication;
                if (application == null) throw new InvalidOperationException("ArcMap uygulama bağlantısı alınamadı.");
                var id = new UIDClass { Value = "SpatialPredictionStudio_StudioDockWindow" };
                var window = ((IDockableWindowManager)application).GetDockableWindow(id);
                if (window == null) throw new InvalidOperationException("Kenetlenebilir pencere kaydı bulunamadı.");
                window.Show(true);
            }
            catch (Exception ex) { MessageBox.Show(ex.Message, "Spatial Prediction Studio", MessageBoxButtons.OK, MessageBoxIcon.Error); }
        }
        protected override void OnUpdate() { Enabled = Hook is IApplication; }
    }

    public sealed class StudioDockWindow : ESRI.ArcGIS.Desktop.AddIns.DockableWindow
    {
        private StudioControl control;
        protected override IntPtr OnCreateChild()
        {
            control = new StudioControl(Hook as IApplication);
            return control.Handle;
        }
        protected override void Dispose(bool disposing)
        {
            if (disposing && control != null) { control.Dispose(); control = null; }
            base.Dispose(disposing);
        }
    }
}
