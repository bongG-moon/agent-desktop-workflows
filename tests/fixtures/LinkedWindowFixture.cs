// Development-only WinForms manager and independent child-process windows.
// All windows deliberately expose an identical edit control. The test reads
// per-process values over redirected pipes, independently of the UIA client.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

sealed class LinkedWindowFixture : Form {
    readonly JavaScriptSerializer serializer = new JavaScriptSerializer();
    readonly TextBox entry = new TextBox();
    readonly Dictionary<string, Process> children = new Dictionary<string, Process>();
    readonly Dictionary<string, int> launchCounts = new Dictionary<string, int>();
    readonly bool manager;
    int writes;
    protected override bool ShowWithoutActivation { get { return true; } }

    LinkedWindowFixture(string device) {
        manager = device == null;
        Text = manager ? "Synthetic desk manager" : "Synthetic device " + device;
        Name = manager ? "Manager" : "Device";
        StartPosition = FormStartPosition.Manual;
        Location = new Point(manager ? 35 : device == "Alpha" ? 400 : 765, 60);
        ClientSize = new Size(320, 160);
        ShowInTaskbar = false;
        AutoScaleMode = AutoScaleMode.None;
        entry.Name = "SharedValue";
        entry.AccessibleName = "Shared value";
        entry.Text = "initial";
        entry.SetBounds(15, 15, 270, 28);
        entry.TextChanged += delegate { writes++; };
        Controls.Add(entry);
        if (manager) {
            int x = 15;
            foreach (string name in new [] {"Alpha", "Beta"}) {
                string key = name;
                var launch = new Button();
                launch.Name = "Open" + key;
                launch.AccessibleName = launch.Text = "Open " + key;
                launch.SetBounds(x, 65, 120, 35);
                x += 132;
                launchCounts[key] = 0;
                launch.Click += delegate { launchCounts[key]++; StartDevice(key); };
                Controls.Add(launch);
            }
        }
        Shown += delegate {
            Emit("ready");
            new Thread(ReadCommands) { IsBackground = true }.Start();
        };
        FormClosed += delegate { CloseChildren(); };
    }

    void StartDevice(string key) {
        if (children.ContainsKey(key)) return;
        string exe = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "DeskDeviceFixture.exe");
        var info = new ProcessStartInfo(exe, key);
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardInput = info.RedirectStandardOutput = true;
        children[key] = Process.Start(info);
        if (children[key].StandardOutput.ReadLine() == null)
            throw new IOException("Device stopped during startup");
    }

    void CloseChildren() {
        foreach (Process child in children.Values) {
            try {
                if (!child.HasExited) {
                    child.StandardInput.WriteLine("quit");
                    child.StandardInput.Flush();
                    if (!child.WaitForExit(3000)) { child.Kill(); child.WaitForExit(); }
                }
            } finally { child.Dispose(); }
        }
        children.Clear();
    }

    void Emit(string kind) {
        var states = new Dictionary<string, object>();
        var targets = new Dictionary<string, object>();
        foreach (Control control in Controls) {
            targets[control.Name] = control.Handle.ToInt64();
        }
        if (kind == "state" || kind == "open-decoy") {
            foreach (var child in children) {
                child.Value.StandardInput.WriteLine("state");
                child.Value.StandardInput.Flush();
                states[child.Key] = serializer.DeserializeObject(child.Value.StandardOutput.ReadLine());
            }
        }
        Console.WriteLine(serializer.Serialize(new {
            kind = kind, pid = Process.GetCurrentProcess().Id, hwnd = Handle.ToInt64(), title = Text,
            value = entry.Text, writes = writes, launched = launchCounts, devices = states, targets = targets
        }));
        Console.Out.Flush();
    }

    void ReadCommands() {
        string line;
        while ((line = Console.ReadLine()) != null) {
            string command = line;
            try { Invoke(new Action(delegate {
                if (command == "quit") { Close(); return; }
                if (command == "reset") {
                    CloseChildren(); entry.Text = "initial"; writes = 0;
                    if (manager) { launchCounts["Alpha"] = launchCounts["Beta"] = 0; }
                }
                if (command == "open-decoy" && manager) StartDevice("Decoy");
                Emit(command);
            })); } catch (InvalidOperationException) { return; }
        }
    }

    [STAThread] static void Main(string[] args) {
        Console.OutputEncoding = new UTF8Encoding(false);
        Application.EnableVisualStyles();
        Application.Run(new LinkedWindowFixture(args.Length == 0 ? null : args[0]));
    }
}
