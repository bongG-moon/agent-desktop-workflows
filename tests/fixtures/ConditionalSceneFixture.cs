// Two real windows paint the same game icon. An advertisement covers only part of one window.
using System;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

sealed class TestDevice : Form {
    readonly Panel canvas = new Panel();
    readonly Rectangle game = new Rectangle(55, 67, 66, 70);
    readonly Rectangle closeAd = new Rectangle(273, 48, 48, 44);
    bool advert;
    public int ClosedAds, Launches, Misses, Premature;

    public TestDevice(string title, bool showAdvert, Point location) {
        Text = title;
        advert = showAdvert;
        StartPosition = FormStartPosition.Manual;
        Location = location;
        ClientSize = new Size(370, 220);
        AutoScaleMode = AutoScaleMode.None;
        canvas.Dock = DockStyle.Fill;
        canvas.BackColor = Color.FromArgb(36, 46, 62);
        canvas.Paint += delegate(object s, PaintEventArgs e) {
            Graphics g = e.Graphics;
            if (Launches > 0) {
                g.DrawString("Game title screen", Font, Brushes.White, 48, 73);
                return;
            }
            g.FillRectangle(Brushes.Indigo, game);
            g.FillEllipse(Brushes.Orange, 60, 72, 51, 47);
            g.FillPolygon(Brushes.Navy, new Point[] {
                new Point(65, 86), new Point(101, 77), new Point(97, 112), new Point(80, 118) });
            g.FillEllipse(Brushes.White, 78, 87, 15, 15);
            g.DrawString("Game", Font, Brushes.White, 65, 117);
            using (Brush mark = new SolidBrush(Color.FromArgb(17, 199, 151)))
                g.FillRectangle(mark, 84, 93, 3, 3);
            if (!advert) return;
            g.FillRectangle(Brushes.LightSteelBlue, 142, 45, 187, 124);
            g.DrawString("Advertisement", Font, Brushes.Black, 149, 108);
            g.FillRectangle(Brushes.Maroon, closeAd);
            using (Pen ink = new Pen(Color.White, 5)) {
                g.DrawLine(ink, 281, 54, 312, 85);
                g.DrawLine(ink, 311, 55, 283, 84);
            }
            using (Brush mark = new SolidBrush(Color.FromArgb(223, 3, 198)))
                g.FillRectangle(mark, 295, 68, 3, 3);
        };
        canvas.MouseClick += delegate(object s, MouseEventArgs mouse) {
            if (mouse.Button != MouseButtons.Left || Launches > 0) Misses++;
            else if (advert && closeAd.Contains(mouse.Location)) { advert = false; ClosedAds++; }
            else if (game.Contains(mouse.Location)) { if (advert) Premature++; else Launches++; }
            else Misses++;
            canvas.Refresh();
        };
        Controls.Add(canvas);
    }
}

sealed class ConditionalSceneFixture : Form {
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly TestDevice[] devices = new TestDevice[2];
    readonly int[] opens = new int[2];

    ConditionalSceneFixture() {
        Text = "Conditional scene manager";
        ClientSize = new Size(310, 90);
        StartPosition = FormStartPosition.CenterScreen;
        for (int i = 0; i < devices.Length; i++) {
            int index = i;
            var button = new Button { Name = "Launch" + i, Text = "Open device " + i,
                Bounds = new Rectangle(10 + i * 148, 22, 140, 37) };
            button.Click += delegate {
                opens[index]++;
                if (devices[index] != null) return;
                Rectangle area = Screen.FromControl(this).WorkingArea;
                var location = new Point(area.Left + 30 + 380 * index, area.Top + 70);
                var device = new TestDevice("Painted device " + index, index == 0, location);
                devices[index] = device;
                device.Show(this);
            };
            Controls.Add(button);
        }
        Shown += delegate {
            Emit("ready");
            new Thread(ReadCommands) { IsBackground = true }.Start();
        };
    }

    void Emit(string kind) {
        var counters = Array.ConvertAll(devices, d => d == null ? null :
            new { closed = d.ClosedAds, launched = d.Launches, missed = d.Misses, premature = d.Premature });
        Console.WriteLine(json.Serialize(new { kind, pid = System.Diagnostics.Process.GetCurrentProcess().Id,
            hwnd = Handle.ToInt64(), title = Text, opens, devices = counters }));
        Console.Out.Flush();
    }

    void ReadCommands() {
        string line;
        while ((line = Console.ReadLine()) != null) {
            string command = line;
            Invoke(new Action(delegate {
                if (command == "quit") { Close(); return; }
                Emit(command);
            }));
        }
    }

    [STAThread] static void Main() {
        SetProcessDpiAwarenessContext(new IntPtr(-4));
        Application.EnableVisualStyles();
        Application.Run(new ConditionalSceneFixture());
    }
}
