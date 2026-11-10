// Development-only app. The grid paints its model and exposes no UIA cell children.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Security;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Peers;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;

sealed class ResultSurface : FrameworkElement {
    internal bool Ready;
    internal string Result = "";
    static readonly Typeface Face = new Typeface("Segoe UI");
    internal ResultSurface() {
        Height = 138; Focusable = true;
        AutomationProperties.SetAutomationId(this, "ResultSurface");
        AutomationProperties.SetName(this, "Result table");
    }
    protected override AutomationPeer OnCreateAutomationPeer() { return new SurfacePeer(this); }
    protected override void OnRender(DrawingContext dc) {
        var border = new Pen(Brushes.DimGray, 1);
        dc.DrawRectangle(Brushes.White, border, new Rect(0, 0, ActualWidth, ActualHeight));
        var text = new FormattedText(Ready ? "RESULT READY" : "WAITING", CultureInfo.InvariantCulture,
            FlowDirection.LeftToRight, Face, 16, Brushes.Black, VisualTreeHelper.GetDpi(this).PixelsPerDip);
        dc.DrawText(text, new Point(10, 10));
        dc.DrawLine(border, new Point(0, 40), new Point(ActualWidth, 40));
        if (Ready) dc.DrawText(new FormattedText(Result, CultureInfo.InvariantCulture,
            FlowDirection.LeftToRight, Face, 15, Brushes.Black, VisualTreeHelper.GetDpi(this).PixelsPerDip), new Point(10, 55));
    }
    sealed class SurfacePeer : FrameworkElementAutomationPeer {
        internal SurfacePeer(ResultSurface element) : base(element) {}
        protected override string GetClassNameCore() { return "PaintedResults"; }
        protected override AutomationControlType GetAutomationControlTypeCore() { return AutomationControlType.DataGrid; }
        protected override List<AutomationPeer> GetChildrenCore() { return null; }
    }
}

sealed class DesktopContextFixture : Window {
    readonly EvidenceCombo status = new EvidenceCombo("conditionState", true, false, false, false);
    readonly ComboBox family = new ComboBox();
    readonly Button query = new Button {Content = "Search"};
    readonly ResultSurface surface = new ResultSurface();
    readonly TextBlock indicator = new TextBlock();
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly DispatcherTimer timer = new DispatcherTimer();
    readonly ContextMenu popup = new ContextMenu();
    readonly string output;
    int queries, rightClicks, menus, parents, leaves, wrongPoint, unrelated, generation;
    string resultStatus = "", resultFamily = "";
    bool hold, stale, skipFile;

    DesktopContextFixture(string path) {
        output = path; Width = 525; Height = 370; Title = "Synthetic device report";
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        ShowInTaskbar = false;
        family.Items.Add("Standard"); family.Items.Add("Hybrid"); family.Items.Add("DRAM");
        family.SelectedIndex = 0; AutomationProperties.SetAutomationId(family, "conditionFamily");
        AutomationProperties.SetAutomationId(query, "QueryCommand");
        AutomationProperties.SetAutomationId(indicator, "ResultIndicator");
        var panel = new StackPanel { Margin = new Thickness(16) };
        AddField(panel, "MES Setup", status); AddField(panel, "Family", family);
        query.Click += delegate {
            queries++; resultStatus = status.Current; resultFamily = (string)family.SelectedItem;
            if (!stale) UpdateResult(false);
            timer.Stop(); if (!hold && !stale) timer.Start();
        };
        timer.Interval = TimeSpan.FromMilliseconds(1050);
        timer.Tick += delegate { timer.Stop(); generation++; UpdateResult(true); };
        panel.Children.Add(query); panel.Children.Add(indicator); panel.Children.Add(surface);
        var decoy = new Menu();
        var menu = new MenuItem { Header = "EXPORT" };
        var item = new MenuItem { Header = "EXPORT TO EXCEL" };
        item.Click += delegate { unrelated++; }; menu.Items.Add(item); decoy.Items.Add(menu); panel.Children.Add(decoy);
        var parent = new MenuItem { Header = "EXPORT" };
        var leaf = new MenuItem { Header = "EXPORT TO EXCEL" };
        var other = new MenuItem { Header = "Copy cell" };
        other.Click += delegate { unrelated++; };
        parent.SubmenuOpened += delegate { parents++; };
        leaf.Click += delegate { leaves++; if (surface.Ready && !skipFile) WriteWorkbook(); };
        parent.Items.Add(leaf); popup.Items.Add(other); popup.Items.Add(parent);
        popup.Opened += delegate { menus++; }; surface.ContextMenu = popup;
        surface.PreviewMouseRightButtonUp += delegate(object sender, System.Windows.Input.MouseButtonEventArgs e) {
            rightClicks++;
            Point point = e.GetPosition(surface);
            if (!surface.Ready || point.Y < 40 || point.Y >= 112) { wrongPoint++; e.Handled = true; }
        };
        surface.ContextMenuOpening += delegate(object sender, ContextMenuEventArgs e) {
            if (!surface.Ready || e.CursorTop < 40 || e.CursorTop >= 112) e.Handled = true;
        };
        Content = panel; UpdateResult(false);
        ContentRendered += delegate {
            Emit("ready"); new Thread(ReadCommands) { IsBackground = true }.Start();
        };
    }
    static void AddField(Panel panel, string text, Control control) {
        var row = new StackPanel { Orientation = Orientation.Horizontal };
        var label = new Label { Content = text, Width = 110, Target = control };
        AutomationProperties.SetName(label, text); AutomationProperties.SetLabeledBy(control, label);
        control.Width = 300; row.Children.Add(label); row.Children.Add(control); panel.Children.Add(row);
    }
    void UpdateResult(bool ready) {
        surface.Ready = ready; surface.Result = ready ? resultStatus + "   " + resultFamily : "";
        indicator.Text = ready ? "RESULT READY" : "WAITING"; surface.InvalidateVisual(); UpdateLayout();
    }
    // Create the actual distributable workbook from the query's captured parameters.
    void WriteWorkbook() {
        const string ss = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
        const string rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
        const string pkgrel = "http://schemas.openxmlformats.org/package/2006/relationships";
        using (var stream = new FileStream(output, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        using (var zip = new ZipArchive(stream, ZipArchiveMode.Create)) {
            Entry(zip, "[Content_Types].xml", "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'><Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/><Default Extension='xml' ContentType='application/xml'/><Override PartName='/xl/workbook.xml' ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'/><Override PartName='/xl/worksheets/sheet1.xml' ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'/></Types>");
            Entry(zip, "_rels/.rels", "<Relationships xmlns='" + pkgrel + "'><Relationship Id='rId1' Type='" + rel + "/officeDocument' Target='xl/workbook.xml'/></Relationships>");
            Entry(zip, "xl/workbook.xml", "<workbook xmlns='" + ss + "' xmlns:r='" + rel + "'><sheets><sheet name='Results' sheetId='1' r:id='rId1'/></sheets></workbook>");
            Entry(zip, "xl/_rels/workbook.xml.rels", "<Relationships xmlns='" + pkgrel + "'><Relationship Id='rId1' Type='" + rel + "/worksheet' Target='worksheets/sheet1.xml'/></Relationships>");
            Entry(zip, "xl/worksheets/sheet1.xml", "<worksheet xmlns='" + ss + "'><sheetData><row r='1'>" +
                Cell("A1", "MES Setup") + Cell("B1", "Family") + Cell("C1", "Generation") + "</row><row r='2'>" +
                Cell("A2", resultStatus) + Cell("B2", resultFamily) + Cell("C2", generation.ToString()) +
                "</row></sheetData></worksheet>");
        }
    }
    static string Cell(string address, string value) {
        return "<c r='" + address + "' t='inlineStr'><is><t>" + SecurityElement.Escape(value) + "</t></is></c>";
    }
    static void Entry(ZipArchive zip, string name, string xml) {
        using (var writer = new StreamWriter(zip.CreateEntry(name).Open(), new UTF8Encoding(false))) writer.Write(xml);
    }
    static object Bounds(FrameworkElement element) {
        var p = element.PointToScreen(new Point(0,0));
        var q = element.PointToScreen(new Point(element.ActualWidth, element.ActualHeight));
        return new [] {(int)p.X, (int)p.Y, (int)(q.X-p.X), (int)(q.Y-p.Y)};
    }
    void Emit(string kind) {
        Console.WriteLine(json.Serialize(new {kind = kind, pid = Process.GetCurrentProcess().Id,
            title = Title, hwnd = new WindowInteropHelper(this).Handle.ToInt64(),
            status = status.Current, family = (string)family.SelectedItem, result_status = resultStatus,
            result_family = resultFamily, writes = status.WriteCount, queries = queries, right_clicks = rightClicks,
            wrong_point = wrongPoint, menus = menus, parents = parents, leaves = leaves, unrelated = unrelated,
            ready = surface.Ready, generation = generation, grid_bounds = Bounds(surface), query_bounds = Bounds(query) }));
        Console.Out.Flush();
    }
    void ReadCommands() {
        string line;
        while ((line = Console.ReadLine()) != null) {
            string cmd = line;
            Dispatcher.Invoke(new Action(delegate {
                if (cmd == "quit") { popup.IsOpen = false; Close(); return; }
                if (cmd == "reference-ready") UpdateResult(true);
                if (cmd == "reset") {
                    popup.IsOpen = false; timer.Stop(); hold = stale = skipFile = false;
                    queries = rightClicks = menus = parents = leaves = wrongPoint = unrelated = generation = 0;
                    family.SelectedIndex = 0; resultStatus = resultFamily = ""; UpdateResult(false);
                }
                if (cmd == "hold-query") hold = true;
                if (cmd == "stale-result") { stale = true; UpdateResult(true); }
                if (cmd == "skip-file") skipFile = true;
                if (cmd == "activate") Activate();
                Emit("state");
            }));
            if (cmd == "quit") break;
        }
    }
    [STAThread] static void Main(string[] args) { new Application().Run(new DesktopContextFixture(args[0])); }
}
