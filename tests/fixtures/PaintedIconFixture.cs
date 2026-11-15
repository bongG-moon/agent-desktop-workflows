// The icon has no Windows control, automation ID, or accessibility name.
using System;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

sealed class PaintedIconFixture : Form {
    readonly Panel canvas = new Panel();
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    int left = 64, pressed, missed;
    Rectangle Icon { get { return new Rectangle(left, 65, 72, 68); } }

    PaintedIconFixture() {
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
        Controls.Add(canvas);
        Shown += delegate {
            Emit("ready");
            new Thread(ReadCommands) { IsBackground = true }.Start();
        };
    }

    void Emit(string kind) {
        Point center = canvas.PointToScreen(new Point(left + 33, 95));
        Console.WriteLine(json.Serialize(new { kind, pid = System.Diagnostics.Process.GetCurrentProcess().Id,
            hwnd = Handle.ToInt64(), title = Text, x = center.X, y = center.Y, pressed, missed }));
        Console.Out.Flush();
    }

    void ReadCommands() {
        string line;
        while ((line = Console.ReadLine()) != null) {
            string command = line;
            Invoke(new Action(delegate {
                if (command == "quit") { Close(); return; }
                if (command == "move-icon") { left = 246; canvas.Refresh(); }
                Emit(command);
            }));
        }
    }

    [STAThread] static void Main() {
        SetProcessDpiAwarenessContext(new IntPtr(-4));
        Application.EnableVisualStyles();
        Application.Run(new PaintedIconFixture());
    }
}
