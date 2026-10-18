// Cavalry Bridge Setup - Windows installer for the Cavalry <-> Cinema 4D bridge.
//
// Finds Cinema 4D and Cavalry and installs the bridge into each program's
// own user folders (no admin rights needed):
//   Cinema 4D  %APPDATA%\Maxon\<version>\plugins\cavalry_bridge\
//              %APPDATA%\Maxon\<version>\library\scripts\Cavalry Bridge Setup.py
//   Cavalry    %APPDATA%\Cavalry\Scripts\C4D Bridge.js
// The files travel inside this exe as an embedded zip (payload.zip).
//
// Command line (for automation and testing):
//   /silent      install (or uninstall) into everything found, no window
//   /uninstall   remove instead of install
//   /appdata X   use folder X instead of %APPDATA%
//   /log X       also write the log to file X
//
// Built by build_installer.ps1 with the C# compiler that ships with .NET
// Framework 4.x, so it is written in C# 5.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;

[assembly: AssemblyTitle("Cavalry Bridge Setup")]
[assembly: AssemblyProduct("Cavalry Bridge")]
[assembly: AssemblyDescription("Installs the Cavalry Bridge into Cinema 4D and Cavalry")]
[assembly: AssemblyVersion("1.0.1.0")]
[assembly: AssemblyFileVersion("1.0.1.0")]

namespace CavalryBridgeSetup
{
    class Target
    {
        public bool IsC4D;
        public string Label;    // "Cinema 4D 2026", "Cavalry"
        public string Folder;   // C4D: its user (prefs) folder; Cavalry: %APPDATA%\Cavalry
        public bool Installed;

        public string PluginDir { get { return Path.Combine(Folder, "plugins", Setup.PluginName); } }
        public string SetupScript { get { return Path.Combine(Folder, "library", "scripts", Setup.SetupScriptName); } }
        public string CavalryScript { get { return Path.Combine(Folder, "Scripts", Setup.CavalryScriptName); } }

        public override string ToString()
        {
            return Label + (Installed ? "   (installed - will be updated)" : "");
        }
    }

    static class Setup
    {
        public const string Version = "1.0.1";
        public const string PluginName = "cavalry_bridge";
        public const string SetupScriptName = "Cavalry Bridge Setup.py";
        public const string CavalryScriptName = "C4D Bridge.js";

        // ------------------------------------------------------------ detect

        static readonly Regex C4DFolder = new Regex(@"^Maxon Cinema 4D (R(\d+)|(\d{4}))", RegexOptions.IgnoreCase);

        static int Release(Match m)   // R23 = 2020 ... 2026 = R29
        {
            return m.Groups[2].Success ? int.Parse(m.Groups[2].Value) : int.Parse(m.Groups[3].Value) - 1997;
        }

        static string Label(Match m)
        {
            return "Cinema 4D " + (m.Groups[2].Success ? "R" + m.Groups[2].Value : m.Groups[3].Value);
        }

        static IEnumerable<string> ProgramFolders()
        {
            foreach (var root in new[] {
                Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                Environment.GetEnvironmentVariable("ProgramW6432"),
                Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs") }.Distinct())
            {
                if (!string.IsNullOrEmpty(root) && Directory.Exists(root)) yield return root;
            }
        }

        // Targets to install into, plus notes about what was skipped and why.
        public static List<Target> Find(string appData, List<string> notes)
        {
            var targets = new List<Target>();

            // Cinema 4D: one user folder per installed version that has been started.
            var labels = new HashSet<string>();
            string maxon = Path.Combine(appData, "Maxon");
            if (Directory.Exists(maxon))
            {
                foreach (var dir in Directory.GetDirectories(maxon).OrderByDescending(d => d))
                {
                    var m = C4DFolder.Match(Path.GetFileName(dir));
                    if (!m.Success) continue;
                    labels.Add(Label(m));
                    if (Release(m) < 23)
                    {
                        notes.Add(Label(m) + " is too old for Python 3 plugins (R23 or newer is needed) - skipped.");
                        continue;
                    }
                    var t = new Target { IsC4D = true, Label = Label(m), Folder = dir };
                    if (targets.Any(x => x.Label == t.Label))
                        t.Label += " (" + Path.GetFileName(dir).Substring(m.Length).Trim('_', ' ') + ")";
                    t.Installed = Directory.Exists(t.PluginDir);
                    targets.Add(t);
                }
            }
            // Installed versions that were never started have no user folder yet.
            foreach (var root in ProgramFolders())
            {
                foreach (var dir in Directory.GetDirectories(root, "Maxon Cinema 4D*"))
                {
                    if (!File.Exists(Path.Combine(dir, "Cinema 4D.exe"))) continue;
                    var m = C4DFolder.Match(Path.GetFileName(dir));
                    if (m.Success && !labels.Contains(Label(m)))
                    {
                        labels.Add(Label(m));
                        notes.Add(Label(m) + " is installed but has never been started - start it once, then run this setup again.");
                    }
                }
            }
            if (!targets.Any() && !notes.Any(n => n.StartsWith("Cinema 4D")))
                notes.Add("Cinema 4D was not found.");

            // Cavalry: its user folder, or an installed Cavalry that will create it.
            string cavalry = Path.Combine(appData, "Cavalry");
            string exe = ProgramFolders().Select(r => Path.Combine(r, "Cavalry", "Cavalry.exe")).FirstOrDefault(File.Exists);
            if (Directory.Exists(cavalry) || exe != null)
            {
                string label = "Cavalry";
                if (exe != null)
                {
                    string v = FileVersionInfo.GetVersionInfo(exe).ProductVersion;
                    if (!string.IsNullOrEmpty(v)) label += " " + v;
                }
                var t = new Target { IsC4D = false, Label = label, Folder = cavalry };
                t.Installed = File.Exists(t.CavalryScript);
                targets.Add(t);
            }
            else
            {
                notes.Add("Cavalry was not found.");
            }
            return targets;
        }

        public static string RunningWarning(IEnumerable<Target> targets, bool uninstall)
        {
            var lines = new List<string>();
            if (targets.Any(t => t.IsC4D) && Process.GetProcessesByName("Cinema 4D").Length > 0)
                lines.Add(uninstall ? "Cinema 4D is running: restart it to unload the plugin."
                                    : "Cinema 4D is running: restart it afterwards to load the plugin (save your work first).");
            if (targets.Any(t => !t.IsC4D) && Process.GetProcessesByName("Cavalry").Length > 0)
                lines.Add(uninstall ? "Cavalry is running: close the C4D Bridge window if it is open."
                                    : "Cavalry is running: close and reopen the C4D Bridge window if it is open.");
            return string.Join(Environment.NewLine, lines);
        }

        // ----------------------------------------------------- install/remove

        static ZipArchive OpenPayload()
        {
            var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip");
            if (stream == null) throw new InvalidOperationException("This setup file is damaged (no payload).");
            return new ZipArchive(stream, ZipArchiveMode.Read);
        }

        // Extracts the payload entries under `prefix` into `destination`.
        static int Extract(ZipArchive zip, string prefix, string destination)
        {
            int count = 0;
            foreach (var entry in zip.Entries)
            {
                string name = entry.FullName.Replace('\\', '/');
                if (!name.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) || name.EndsWith("/")) continue;
                string target = Path.Combine(destination, name.Substring(prefix.Length).Replace('/', Path.DirectorySeparatorChar));
                Directory.CreateDirectory(Path.GetDirectoryName(target));
                entry.ExtractToFile(target, true);
                count++;
            }
            if (count == 0) throw new InvalidOperationException("This setup file is damaged (missing " + prefix + ").");
            return count;
        }

        public static void Install(Target t, Action<string> log)
        {
            using (var zip = OpenPayload())
            {
                if (t.IsC4D)
                {
                    // Replace the plugin folder as a whole so no stale files are left over.
                    if (Directory.Exists(t.PluginDir)) Directory.Delete(t.PluginDir, true);
                    Extract(zip, "cinema4d/plugins/", Path.Combine(t.Folder, "plugins"));
                    Extract(zip, "cinema4d/scripts/", Path.GetDirectoryName(t.SetupScript));
                    log(t.Label + ": " + (t.Installed ? "updated" : "installed"));
                    log("    " + t.PluginDir);
                    log("    " + t.SetupScript);
                }
                else
                {
                    Extract(zip, "cavalry/", Path.GetDirectoryName(t.CavalryScript));
                    log(t.Label + ": " + (t.Installed ? "updated" : "installed"));
                    log("    " + t.CavalryScript);
                }
            }
            t.Installed = true;
        }

        public static void Uninstall(Target t, Action<string> log)
        {
            bool removed = false;
            if (t.IsC4D)
            {
                if (Directory.Exists(t.PluginDir)) { Directory.Delete(t.PluginDir, true); removed = true; }
                if (File.Exists(t.SetupScript)) { File.Delete(t.SetupScript); removed = true; }
            }
            else if (File.Exists(t.CavalryScript))
            {
                File.Delete(t.CavalryScript);
                removed = true;
            }
            log(t.Label + ": " + (removed ? "removed" : "was not installed"));
            t.Installed = false;
        }

        public const string NextSteps =
            "Next steps\r\n" +
            "  Cinema 4D: sending from Cavalry creates Cavalry Spline objects automatically.\r\n" +
            "    To stream splines to Cavalry, select them and run\r\n" +
            "    Extensions > User Scripts > Cavalry Bridge Setup.\r\n" +
            "  Cavalry: open Window > Scripts > C4D Bridge. The first time, Cavalry asks\r\n" +
            "    whether to trust the script (it makes local network calls) - choose to trust it.";

        // ------------------------------------------------------------- main

        [STAThread]
        static int Main(string[] args)
        {
            bool silent = false, uninstall = false;
            string appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
            string logFile = null;
            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i].ToLowerInvariant().TrimStart('/', '-');
                if (a == "silent" || a == "s") silent = true;
                else if (a == "uninstall") uninstall = true;
                else if (a == "appdata" && i + 1 < args.Length) appData = args[++i];
                else if (a == "log" && i + 1 < args.Length) logFile = args[++i];
            }

            var notes = new List<string>();
            var targets = Find(appData, notes);

            if (silent)
            {
                var sb = new StringBuilder();
                Action<string> log = s => sb.AppendLine(s);
                log("Cavalry Bridge " + Version + (uninstall ? " - uninstall" : " - install"));
                foreach (var n in notes) log("Note: " + n);
                int failures = 0;
                foreach (var t in targets)
                {
                    try { if (uninstall) Uninstall(t, log); else Install(t, log); }
                    catch (Exception e) { log(t.Label + ": FAILED - " + e.Message); failures++; }
                }
                string warn = RunningWarning(targets, uninstall);
                if (warn.Length > 0) log(warn);
                if (logFile != null) File.WriteAllText(logFile, sb.ToString());
                return failures > 0 ? 2 : (targets.Count == 0 ? 1 : 0);
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new SetupForm(targets, notes, uninstall));
            return 0;
        }
    }

    class SetupForm : Form
    {
        readonly List<Target> targets;
        readonly CheckedListBox list = new CheckedListBox();
        readonly TextBox logBox = new TextBox();
        readonly Button installButton = new Button();
        readonly Button uninstallButton = new Button();

        // Layout is in 96-dpi pixels; fonts are in points and already follow
        // the screen's DPI, so scale positions and sizes to match them.
        readonly float dpi;
        int S(int v) { return (int)Math.Round(v * dpi); }
        Point P(int x, int y) { return new Point(S(x), S(y)); }
        Size Z(int w, int h) { return new Size(S(w), S(h)); }

        public SetupForm(List<Target> targets, List<string> notes, bool startWithUninstall)
        {
            this.targets = targets;
            using (var g = CreateGraphics()) dpi = g.DpiX / 96f;
            AutoScaleMode = AutoScaleMode.None;
            Text = "Cavalry Bridge " + Setup.Version + " Setup";
            Font = new Font("Segoe UI", 9f);
            ClientSize = Z(600, 540);   // 79 header + 444 body + margin
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }

            // Header
            var header = new Panel { Dock = DockStyle.Top, Height = S(78), BackColor = Color.White };
            var title = new Label { Text = "Cavalry Bridge", Font = new Font("Segoe UI", 15f, FontStyle.Bold), AutoSize = true, Location = P(18, 10) };
            var subtitle = new Label { Text = "Live link between Cavalry and Cinema 4D  -  version " + Setup.Version, AutoSize = true, Location = P(20, 48), ForeColor = Color.DimGray };
            var picture = new PictureBox { Size = Z(48, 48), Location = P(534, 15), SizeMode = PictureBoxSizeMode.Zoom };
            try { picture.Image = new Icon(Icon, S(48), S(48)).ToBitmap(); } catch { }
            header.Controls.AddRange(new Control[] { title, subtitle, picture });
            var line = new Label { Dock = DockStyle.Top, Height = 1, BackColor = Color.Gainsboro };

            var body = new Panel { Dock = DockStyle.Fill };
            var intro = new Label { Text = "Install into:", AutoSize = true, Location = P(18, 12) };
            list.Location = P(18, 34);
            list.Size = Z(564, 92);
            list.CheckOnClick = true;
            list.IntegralHeight = false;
            foreach (var t in targets) list.Items.Add(t, true);
            if (targets.Count == 0) list.Items.Add("Nothing to install into - see below.", false);

            var allNotes = new List<string>(notes);
            string warn = Setup.RunningWarning(targets, false);
            if (warn.Length > 0) allNotes.AddRange(warn.Split(new[] { Environment.NewLine }, StringSplitOptions.None));
            var notesLabel = new Label
            {
                Text = string.Join(Environment.NewLine, allNotes.Select(n => "• " + n)),
                Location = P(18, 134), Size = Z(564, 64), ForeColor = Color.FromArgb(150, 90, 0)
            };

            logBox.Location = P(18, 204);
            logBox.Size = Z(564, 200);
            logBox.Multiline = true;
            logBox.ReadOnly = true;
            logBox.ScrollBars = ScrollBars.Vertical;
            logBox.BackColor = Color.White;
            logBox.Font = new Font("Consolas", 8.5f);
            logBox.Text = "Files go into each program's own user folders; no admin rights are needed." + Environment.NewLine;

            installButton.Text = "Install";
            installButton.Size = Z(110, 30);
            installButton.Location = P(232, 414);
            installButton.Click += delegate { Run(false); };
            uninstallButton.Text = "Uninstall";
            uninstallButton.Size = Z(110, 30);
            uninstallButton.Location = P(352, 414);
            uninstallButton.Click += delegate { Run(true); };
            var closeButton = new Button { Text = "Close", Size = Z(110, 30), Location = P(472, 414) };
            closeButton.Click += delegate { Close(); };
            installButton.Enabled = uninstallButton.Enabled = targets.Count > 0;
            AcceptButton = startWithUninstall ? uninstallButton : installButton;
            CancelButton = closeButton;

            body.Controls.AddRange(new Control[] { intro, list, notesLabel, logBox, installButton, uninstallButton, closeButton });
            Controls.Add(body);
            Controls.Add(line);
            Controls.Add(header);
        }

        void Log(string text)
        {
            logBox.AppendText(text + Environment.NewLine);
        }

        void Run(bool uninstall)
        {
            var chosen = list.CheckedItems.OfType<Target>().ToList();
            if (chosen.Count == 0)
            {
                MessageBox.Show(this, "Tick at least one program.", Text, MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            if (uninstall && MessageBox.Show(this, "Remove the Cavalry Bridge from the ticked programs?", Text,
                    MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
                return;

            Log("");
            int failures = 0;
            foreach (var t in chosen)
            {
                try
                {
                    if (uninstall) Setup.Uninstall(t, Log); else Setup.Install(t, Log);
                }
                catch (Exception e)
                {
                    Log(t.Label + ": FAILED - " + e.Message);
                    failures++;
                }
            }
            // Refresh "(installed)" markers.
            for (int i = 0; i < list.Items.Count; i++)
            {
                bool check = list.GetItemChecked(i);
                list.Items[i] = list.Items[i];
                list.SetItemChecked(i, check);
            }
            Log("");
            string warn = Setup.RunningWarning(chosen, uninstall);
            if (warn.Length > 0) Log(warn);
            if (!uninstall && failures == 0) Log(Setup.NextSteps);

            if (failures > 0)
                MessageBox.Show(this, "Some parts could not be " + (uninstall ? "removed" : "installed") + " - see the log.",
                    Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            else
                MessageBox.Show(this, uninstall ? "The Cavalry Bridge was removed." : "The Cavalry Bridge was installed.",
                    Text, MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
    }
}
