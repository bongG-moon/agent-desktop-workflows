// The icon has no Windows control, automation ID, or accessibility name.
using System;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

sealed class PaintedIconFixture : Form {
    sealed class DoubleClickPanel : Panel {
        internal DoubleClickPanel() { SetStyle(ControlStyles.StandardDoubleClick, true); }
    }
    readonly Panel canvas = new DoubleClickPanel();
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    int left = 64, pressed, missed, opened;
    Form detail;
    ListView list;
    ListViewItem targetRow;
    Rectangle Icon { get { return new Rectangle(left, 65, 72, 68); } }

    void OpenDetails() {
        if (detail != null) return;
        opened++;
        detail = new Form { Text = "Item details", Size = new Size(280, 160), ShowInTaskbar = false };
        var note = new TextBox { Name = "CommentText", AccessibleName = "Comment",
            Text = "Ready for inspection", ReadOnly = true, Dock = DockStyle.Top };
        var close = new Button { Name = "CloseDetails", Text = "Done", Dock = DockStyle.Bottom };
        close.Click += delegate { detail.Close(); };
        detail.FormClosed += delegate { detail = null; };
        detail.Controls.Add(note);
        detail.Controls.Add(close);
        detail.Show(this);
    }

    PaintedIconFixture(bool showDetails, bool useList) {
        Text = "Desktop Select painted icon fixture";
        StartPosition = FormStartPosition.CenterScreen;
        TopMost = true;
        AutoScaleMode = AutoScaleMode.None;
        ClientSize = new Size(420, 240);
        canvas.Dock = DockStyle.Fill;
        canvas.BackColor = Color.FromArgb(33, 42, 50);
        canvas.Paint += delegate(object sender, PaintEventArgs paint) {
            var g = paint.Graphics;
            var box = Icon;
            g.FillRectangle(Brushes.Indigo, box);
            // A single non-repeating multicolour glyph on a plain canvas.
            g.FillEllipse(Brushes.Orange, box.X + 9, box.Y + 7, 46, 48);
            g.FillPolygon(Brushes.Navy, new Point[] {
                new Point(box.X + 14, box.Y + 23), new Point(box.X + 45, box.Y + 12),
                new Point(box.X + 44, box.Y + 44), new Point(box.X + 24, box.Y + 50) });
            g.FillEllipse(Brushes.White, box.X + 24, box.Y + 24, 11, 11);
            g.DrawString("Pocket", Font, Brushes.White, box.X + 11, box.Y + 51);
        };
        canvas.MouseClick += delegate(object sender, MouseEventArgs mouse) {
            if (mouse.Button == MouseButtons.Left && Icon.Contains(mouse.Location)) pressed++;
            else missed++;
        };
        if (showDetails) canvas.MouseDoubleClick += delegate(object sender, MouseEventArgs mouse) {
            if (mouse.Button == MouseButtons.Left && Icon.Contains(mouse.Location)) OpenDetails();
        };
        if (useList) {
            list = new ListView { Name = "Results", Dock = DockStyle.Fill, View = View.Details, FullRowSelect = true };
            list.Columns.Add("Reference", 300);
            list.Items.AddRange(new ListViewItem[] { new ListViewItem("Bravo"), new ListViewItem("Charlie"),
                targetRow = new ListViewItem("Order A123"), new ListViewItem("Echo"), new ListViewItem("Foxtrot") });
            list.MouseDoubleClick += delegate(object sender, MouseEventArgs mouse) {
                if (list.GetItemAt(mouse.X, mouse.Y) == targetRow) OpenDetails();
                else missed++;
            };
            Controls.Add(list);
        } else Controls.Add(canvas);
        Shown += delegate {
            Emit("ready");
            new Thread(ReadCommands) { IsBackground = true }.Start();
        };
    }

    void Emit(string kind) {
        Point center = list == null ? canvas.PointToScreen(new Point(left + 33, 95)) :
            list.PointToScreen(new Point(35, targetRow.Bounds.Top + targetRow.Bounds.Height / 2));
        Console.WriteLine(json.Serialize(new { kind, pid = System.Diagnostics.Process.GetCurrentProcess().Id,
            hwnd = Handle.ToInt64(), title = Text, x = center.X, y = center.Y, pressed, missed, opened,
            detailHwnd = detail == null ? 0L : detail.Handle.ToInt64() }));
        Console.Out.Flush();
    }

    void ReadCommands() {
        string line;
        while ((line = Console.ReadLine()) != null) {
            string command = line;
            Invoke(new Action(delegate {
                if (command == "quit") { Close(); return; }
                if (command == "dismiss" && detail != null) { detail.Close(); }
                if (command == "move-icon") {
                    if (list == null) { left = 246; canvas.Refresh(); }
                    else { list.Items.Insert(0, "Alpha"); targetRow.Selected = false; targetRow.Focused = false; list.Refresh(); }
                }
                Emit(command);
            }));
        }
    }

    [STAThread] static void Main(string[] args) {
        SetProcessDpiAwarenessContext(new IntPtr(-4));
        Application.EnableVisualStyles();
        Application.Run(new PaintedIconFixture(Array.IndexOf(args, "--details") >= 0, Array.IndexOf(args, "--list") >= 0));
    }
}
