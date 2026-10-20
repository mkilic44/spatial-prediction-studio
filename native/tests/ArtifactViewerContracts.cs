using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading;
using System.Windows.Forms;

namespace SpatialPredictionStudio
{
    internal static class ArtifactViewerContracts
    {
        private static int count;
        private static void Check(bool passed, string label)
        {
            if (!passed) throw new Exception(label); ++count; Console.WriteLine("PASS " + label);
        }
        private static void PumpUntil(Func<bool> complete, string label)
        {
            var timer = Stopwatch.StartNew();
            while (!complete() && timer.ElapsedMilliseconds < 10000) { Application.DoEvents(); Thread.Sleep(5); }
            Check(complete(), label);
        }
        internal static int Run(string root)
        {
            string directory = Path.Combine(root, "viewer"); Directory.CreateDirectory(directory);
            string index = Path.Combine(directory, "study_report_index.html"), page = Path.Combine(directory, "study_report_page_001.html");
            string unsafePage = Path.Combine(directory, "unsafe.html"), remotePage = Path.Combine(directory, "remote.html"), unknown = Path.Combine(directory, "unknown.html");
            string indexText = "<!doctype html><html><body><h1>Index</h1><a id='next' href='study_report_page_001.html'>Next</a></body></html>";
            string pageText = "<!doctype html><html><body><h1>Page one</h1><a id='back' href='study_report_index.html'>Index</a><a id='anchor' href='#section'>Section</a><p id='section'>Verified page bytes</p></body></html>";
            File.WriteAllText(index, indexText); File.WriteAllText(page, pageText);
            File.WriteAllText(unsafePage, "<html><body><script>document.title='unsafe'</script></body></html>");
            File.WriteAllText(remotePage, "<html><body><img src='https://example.invalid/a.png'></body></html>");
            File.WriteAllText(unknown, "<html><body>Not in manifest</body></html>");
            var hashes = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (string file in new[] { index, page, unsafePage, remotePage }) hashes[file] = ConfigurationIdentity.FileDigest(file);
            using (var viewer = ArtifactViewer.Create(index, hashes))
            {
                var browser = viewer.Controls.OfType<WebBrowser>().Single();
                // Create only our own hidden component handles. No desktop input or accessibility traversal.
                IntPtr formHandle = viewer.Handle, browserHandle = browser.Handle;
                PumpUntil(() => browser.ReadyState == WebBrowserReadyState.Complete && browser.Document != null, "hidden report index finishes real WebBrowser loading");
                int openForms = Application.OpenForms.Count;
                browser.Document.GetElementById("next").InvokeMember("click");
                PumpUntil(() => viewer.ActivePath == page && browser.ReadyState == WebBrowserReadyState.Complete, "verified HTML link navigates in the existing browser");
                Check(viewer.Text.EndsWith("study_report_page_001.html") && Application.OpenForms.Count == openForms && viewer.OwnedForms.Length == 0, "HTML page updates title without creating another report form");
                string copy = Path.Combine(directory, "saved-current.html"); viewer.CopyActiveTo(copy);
                Check(File.ReadAllText(copy) == pageText, "save copy uses the displayed HTML page rather than the original index");
                browser.Document.GetElementById("anchor").InvokeMember("click");
                PumpUntil(() => browser.Url.Fragment == "#section", "same-page HTML anchor remains usable");
                Check(viewer.ActivePath == page, "anchor navigation preserves active file identity");
                browser.Document.GetElementById("back").InvokeMember("click");
                PumpUntil(() => viewer.ActivePath == index && browser.ReadyState == WebBrowserReadyState.Complete, "index return link uses the same verified browser");
                viewer.CopyActiveTo(copy); Check(File.ReadAllText(copy) == indexText, "copy tracks the returned index document");
                int navigations = 0; bool cancelled = false;
                browser.Navigating += (s, e) => { ++navigations; cancelled = e.Cancel; };
                Action<Uri, string> reject = (uri, label) => {
                    int before = navigations; browser.Navigate(uri);
                    PumpUntil(() => navigations > before, label + " reaches navigation guard");
                    Check(cancelled && viewer.ActivePath == index && viewer.Text.EndsWith("study_report_index.html"), label + " is blocked without changing displayed file");
                };
                reject(new Uri("https://example.invalid/report.html"), "network URL");
                reject(new Uri("file://example.invalid/share/report.html"), "UNC network file");
                reject(new Uri(unknown), "unlisted local file");
                reject(new Uri(unsafePage), "hash-verified active HTML");
                reject(new Uri(remotePage), "hash-verified HTML with remote resource");
                File.AppendAllText(page, "changed after manifest");
                reject(new Uri(page), "HTML changed after manifest verification");
                File.AppendAllText(index, "changed displayed source");
                bool refused = false; try { viewer.CopyActiveTo(copy); } catch (InvalidDataException) { refused = true; }
                Check(refused && File.ReadAllText(copy) == indexText, "copy rechecks active file digest before overwriting destination");
                Check(!viewer.Visible && Application.OpenForms.Count == openForms, "component regression never opens or activates a desktop report window");
            }
            return count;
        }
    }
}
