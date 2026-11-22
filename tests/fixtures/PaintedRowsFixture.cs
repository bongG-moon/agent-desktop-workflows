// Real Win32 list rows; the painted glyphs deliberately have no button peer.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

sealed class PaintedRowsFixture : Form {
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    sealed class Device {
        internal readonly string Name;
        internal Device(string name) { Name = name; }
        // The launcher paints its own caption. Standard accessibility text for
        // each list item is empty, just as it is for an unnamed painted peer.
        public override string ToString() { return ""; }
    }
    readonly ListBox list = new ListBox();
    readonly Dictionary<string,int> calls = new Dictionary<string,int>();
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly string[] names = { "Atlas", "Birch", "Cobalt" };
    bool unchangedButtons;
    int otherButtonCalls;

    readonly Dictionary<string,int> childCalls = new Dictionary<string,int>();

    PaintedRowsFixture(bool linked) {
        Text = "Desktop Select painted launchers";
        ClientSize = new Size(640, 240);
        StartPosition = FormStartPosition.Manual;
        Location = new Point(100, 100);
        ShowInTaskbar = false;
        list.Name = "Devices";
        list.AccessibleName = "Devices";
        list.DrawMode = DrawMode.OwnerDrawFixed;
        list.ItemHeight = 58;
        list.Dock = DockStyle.Fill;
        list.IntegralHeight = false;
        foreach (string name in names) { calls[name] = 0; list.Items.Add(new Device(name)); }
        list.DrawItem += PaintRow;
        list.MouseUp += delegate(object sender, MouseEventArgs e) {
            int i = list.IndexFromPoint(e.Location);
            if (unchangedButtons && i >= 0 && e.Button == MouseButtons.Left
                && OtherButtonBox(list.GetItemRectangle(i)).Contains(e.Location)) otherButtonCalls++;
            if (i >= 0 && e.Button == MouseButtons.Left && ButtonBox(list.GetItemRectangle(i)).Contains(e.Location)) {
                string name = ((Device)list.Items[i]).Name;
                calls[name]++;
                if (linked && calls[name] == 1) {
                    var child = new Form { Text = "Details " + name, StartPosition = FormStartPosition.Manual,
                        Location = new Point(800, 100), Size = new Size(200, 140), ShowInTaskbar = false };
                    childCalls[name] = 0;
                    var confirm = new Button { Text = "Confirm", Name = "Confirm", Dock = DockStyle.Fill };
                    confirm.Click += delegate { childCalls[name]++; };
                    child.Controls.Add(confirm);
                    child.Show(this);
                }
                list.Invalidate(list.GetItemRectangle(i));
            }
        };
        Controls.Add(list);
        Shown += delegate {
            Emit("ready");
            new Thread(ReadCommands) { IsBackground = true }.Start();
        };
    }

    static Rectangle ButtonBox(Rectangle r) { return new Rectangle(r.Right-80, r.Y+10, 46, 38); }
    static Rectangle OtherButtonBox(Rectangle r) { return new Rectangle(r.Right-138, r.Y+10, 46, 38); }

    void PaintRow(object sender, DrawItemEventArgs e) {
        if (e.Index < 0) return;
        string name = ((Device)list.Items[e.Index]).Name;
        var r = e.Bounds;
        var b = ButtonBox(r);
        using (var font = new Font("Segoe UI", 13, FontStyle.Bold)) {
            e.Graphics.FillRectangle(Brushes.WhiteSmoke, r);
            e.Graphics.DrawLine(Pens.LightGray, r.X, r.Bottom-1, r.Right, r.Bottom-1);
            e.Graphics.DrawString(name, font, Brushes.Black, r.X + 30, r.Y + 15);
            e.Graphics.DrawString(!unchangedButtons && calls[name] > 0 ? "Running" : "Ready", font,
                                  Brushes.Black, r.X + 310, r.Y + 15);
            e.Graphics.DrawRectangle(Pens.SlateGray, b);
            if (unchangedButtons) {
                // Two non-launch actions, with no visible result inside the row.
                var other = OtherButtonBox(r);
                e.Graphics.DrawRectangle(Pens.SlateGray, other);
                e.Graphics.FillEllipse(Brushes.DarkBlue, other.X+17, other.Y+12, 12, 12);
                e.Graphics.FillRectangle(Brushes.DarkBlue, b.X+13, b.Y+17, 20, 5);
                e.Graphics.FillRectangle(Brushes.DarkBlue, b.X+21, b.Y+9, 5, 20);
            } else if (calls[name] > 0) {
                e.Graphics.FillRectangle(Brushes.DarkGreen, b.X+15, b.Y+10, 17, 18);
            } else {
                e.Graphics.FillPolygon(Brushes.DarkGreen, new [] {
                    new Point(b.X+15,b.Y+9), new Point(b.X+30,b.Y+19), new Point(b.X+15,b.Y+29) });
            }
        }
    }

    void Emit(string kind) {
        var points = new List<int[]>();
        var order = new List<string>();
        for (int i=0; i<list.Items.Count; i++) {
            order.Add(((Device)list.Items[i]).Name);
            var b = ButtonBox(list.GetItemRectangle(i));
            var p = list.PointToScreen(new Point(b.X+b.Width/2, b.Y+b.Height/2));
            points.Add(new int[] {p.X,p.Y});
        }
        Console.WriteLine(json.Serialize(new {kind=kind, title=Text, pid=Process.GetCurrentProcess().Id,
            hwnd=Handle.ToInt64(), calls=calls, childCalls=childCalls, otherButtonCalls=otherButtonCalls,
            points=points, order=order}));
        Console.Out.Flush();
    }

    void ReadCommands() {
        string line;
        while ((line=Console.ReadLine())!=null) {
            string op=line;
            try { Invoke(new Action(delegate {
                if (op=="quit") { Close(); return; }
                if (op=="activate") { Activate(); }
                if (op=="unchanged-buttons") { unchangedButtons=true; list.Invalidate(); }
                if (op=="reset-reorder") {
                    foreach(string name in names) calls[name]=0;
                    list.Items.Clear();
                    foreach (int i in new int[]{2,1,0}) list.Items.Add(new Device(names[i]));
                    list.Invalidate();
                }
                Emit(op);
            })); } catch (InvalidOperationException) { return; }
        }
    }
    [STAThread] static void Main(string[] args) {
        SetProcessDPIAware();
        Console.OutputEncoding=new UTF8Encoding(false);
        Application.EnableVisualStyles();
        Application.Run(new PaintedRowsFixture(Array.IndexOf(args, "--linked") >= 0));
    }
}
