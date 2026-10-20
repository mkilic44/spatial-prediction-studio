using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Net;
using System.Text.RegularExpressions;
using System.Windows.Forms;

namespace SpatialPredictionStudio
{
    internal sealed class ArtifactWindow : Form
    {
        private readonly Dictionary<string, string> hashes;
        internal string ActivePath { get; private set; }
        internal ArtifactWindow(string path, Dictionary<string, string> verifiedHashes)
        {
            hashes = verifiedHashes; SetActive(path);
            Width = 900; Height = 680; StartPosition = FormStartPosition.Manual;
        }
        internal void SetActive(string path)
        {
            ActivePath = Path.GetFullPath(path); Text = "Spatial Prediction Studio - " + Path.GetFileName(ActivePath);
        }
        internal void CopyActiveTo(string destination)
        {
            ArtifactViewer.Verify(ActivePath, hashes); File.Copy(ActivePath, destination, true);
        }
    }
    internal static class ArtifactViewer
    {
        internal static Form Open(Control owner, string path, Dictionary<string, string> hashes)
        {
            var form = Create(path, hashes);
            // Set bounds after WinForms has applied the current monitor's DPI scaling.
            form.Shown += (s, e) => {
                var area = Screen.FromControl(owner).WorkingArea;
                int width = Math.Min(1000, area.Width - 60), height = Math.Min(740, area.Height - 60);
                form.Bounds = new Rectangle(area.Left + (area.Width - width) / 2, area.Top + (area.Height - height) / 2, width, height);
            };
            form.Show(owner); return form;
        }
        internal static ArtifactWindow Create(string path, Dictionary<string, string> hashes)
        {
            path = Path.GetFullPath(path); Verify(path, hashes); string extension = Path.GetExtension(path).ToLowerInvariant();
            var form = new ArtifactWindow(path, hashes);
            var actions = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true }; var save = new Button { Text = "Save a copy of the file", AutoSize = true }; actions.Controls.Add(save);
            var info = new Label { AutoSize = true, Padding = new Padding(8, 7, 0, 0), Text = "Verified local output. Network access is disabled." }; actions.Controls.Add(info);
            save.Click += (s, e) => { try { string activeExtension = Path.GetExtension(form.ActivePath); Verify(form.ActivePath, hashes); using (var dialog = new SaveFileDialog { FileName = Path.GetFileName(form.ActivePath), Filter = "Output file (*" + activeExtension + ")|*" + activeExtension, OverwritePrompt = true }) if (dialog.ShowDialog(form) == DialogResult.OK) form.CopyActiveTo(dialog.FileName); } catch (Exception ex) { MessageBox.Show(form, ex.Message, "Save file", MessageBoxButtons.OK, MessageBoxIcon.Error); } };
            if (extension == ".png" || extension == ".svg")
            {
                string previewPath = path;
                if (extension == ".svg") { previewPath = Path.ChangeExtension(path, ".png"); Verify(previewPath, hashes); info.Text = "PNG preview of the vector graphic. The saved file is the original SVG."; }
                var picture = new PictureBox { Dock = DockStyle.Fill, SizeMode = PictureBoxSizeMode.Zoom, BackColor = Color.White };
                using (var image = Image.FromFile(previewPath)) { double scale = Math.Min(1, 2048.0 / Math.Max(image.Width, image.Height)); picture.Image = new Bitmap(image, new Size(Math.Max(1, (int)(image.Width * scale)), Math.Max(1, (int)(image.Height * scale)))); }
                form.Controls.Add(picture); form.FormClosed += (s, e) => picture.Image?.Dispose();
            }
            else if (extension == ".html" || extension == ".htm")
            {
                ValidateHtml(path, hashes);
                var browser = new WebBrowser { Dock = DockStyle.Fill, AllowWebBrowserDrop = false, IsWebBrowserContextMenuEnabled = false, WebBrowserShortcutsEnabled = false, ScriptErrorsSuppressed = true };
                string pendingHtml = null;
                browser.NewWindow += (s, e) => { e.Cancel = true; };
                browser.Navigating += (s, e) => {
                    e.Cancel = true;
                    try {
                        if (e.Url == null || !e.Url.IsFile || e.Url.IsUnc) throw new InvalidDataException("Only verified local outputs of the job can be opened.");
                        string target = Path.GetFullPath(e.Url.LocalPath); Verify(target, hashes);
                        string targetExtension = Path.GetExtension(target).ToLowerInvariant();
                        if (targetExtension == ".html" || targetExtension == ".htm") {
                            ValidateHtml(target, hashes); pendingHtml = target;
                            save.Enabled = browser.Document != null && String.Equals(form.ActivePath, target, StringComparison.OrdinalIgnoreCase);
                            e.Cancel = false;
                        } else Open(form, target, hashes);
                    } catch (Exception ex) { info.Text = "This link was blocked. " + ex.Message; }
                };
                browser.DocumentCompleted += (s, e) => {
                    if (pendingHtml == null || e.Url == null || !e.Url.IsFile || browser.ReadyState != WebBrowserReadyState.Complete ||
                        !String.Equals(Path.GetFullPath(e.Url.LocalPath), pendingHtml, StringComparison.OrdinalIgnoreCase)) return;
                    form.SetActive(pendingHtml); pendingHtml = null; save.Enabled = true;
                    info.Text = "Verified local output. Network access is disabled.";
                };
                form.Controls.Add(browser); browser.Navigate(new Uri(path));
            }
            else if (extension == ".csv") form.Controls.Add(new DataGridView { Dock = DockStyle.Fill, ReadOnly = true, AllowUserToAddRows = false, DataSource = TabularFiles.Read(path), BackgroundColor = Color.White, AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.DisplayedCells });
            else if (extension == ".json" || extension == ".txt") form.Controls.Add(new TextBox { Dock = DockStyle.Fill, Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Both, WordWrap = false, Text = File.ReadAllText(path), Font = new Font("Consolas", 10) });
            else { form.Dispose(); throw new InvalidDataException("This output type cannot be displayed in the panel: " + extension); }
            form.Controls.Add(actions); return form;
        }
        internal static void Verify(string path, Dictionary<string, string> hashes)
        {
            string expected; if (!hashes.TryGetValue(Path.GetFullPath(path), out expected)) throw new InvalidDataException("The file is not in the output list of the completed job: " + path); ArtifactIntegrity.VerifySha256(path, expected);
        }
        internal static void ValidateHtml(string path, Dictionary<string, string> hashes)
        {
            Verify(path, hashes); string html = File.ReadAllText(path);
            if (Regex.IsMatch(html, @"<\s*(script|iframe|object|embed|base|link|form)\b|@import\b|url\s*\(|expression\s*\(", RegexOptions.IgnoreCase)) throw new InvalidDataException("The report contains active content or a request for an external resource.");
            foreach (Match match in Regex.Matches(html, @"\b(?:src|href)\s*=\s*[""']([^""']+)[""']", RegexOptions.IgnoreCase))
            {
                string target = WebUtility.HtmlDecode(match.Groups[1].Value); if (target.StartsWith("#", StringComparison.Ordinal)) continue;
                if (target.Contains(":") || target.StartsWith("/") || target.StartsWith("\\")) throw new InvalidDataException("The report references an external resource: " + target);
                string resolved = ArtifactIntegrity.ResolveOutputPath(Path.GetDirectoryName(path), Uri.UnescapeDataString(target.Split('#')[0])); Verify(resolved, hashes);
            }
        }
    }
}
