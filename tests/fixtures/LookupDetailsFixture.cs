// A fresh query replaces stale rows. A matching row opens an owned, read-only detail.
using System;
using System.Linq;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Peers;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Data;
using System.Windows.Input;
using System.Windows.Interop;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;

// Draw cell contents without UI Automation children, like an owner-drawn grid.
public sealed class PaintedCell : FrameworkElement {
    public static readonly DependencyProperty CaptionProperty = DependencyProperty.Register(
        "Caption", typeof(string), typeof(PaintedCell),
        new FrameworkPropertyMetadata("", FrameworkPropertyMetadataOptions.AffectsRender));
    public string Caption {get {return (string)GetValue(CaptionProperty);} set {SetValue(CaptionProperty, value);}}
    protected override Size MeasureOverride(Size available) { return new Size(110, 23); }
    protected override void OnRender(DrawingContext context) {
        context.DrawRectangle(Brushes.White, null, new Rect(RenderSize));
        var label = new FormattedText(Caption, System.Globalization.CultureInfo.InvariantCulture,
            FlowDirection.LeftToRight, new Typeface("Segoe UI"), 14, Brushes.Black, VisualTreeHelper.GetDpi(this).PixelsPerDip);
        context.DrawText(label, new Point(4, 2));
    }
}

// No header, row or cell peers. Both hit tests inside this grid reach the same group.
public sealed class PaintedGroup : FrameworkElement {
    public string Key = "OLD";
    public bool Oversized, DuplicateColumnValue;
    public int KeyColumn, MatchRow = 2, WrongColumns;
    [StructLayout(LayoutKind.Sequential)] struct NativeRect { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd, out NativeRect rect);
    protected override AutomationPeer OnCreateAutomationPeer() { return new GroupPeer(this); }
    sealed class GroupPeer : FrameworkElementAutomationPeer {
        public GroupPeer(FrameworkElement owner) : base(owner) {}
        protected override AutomationControlType GetAutomationControlTypeCore() { return AutomationControlType.Group; }
        protected override bool IsControlElementCore() { return true; }
        protected override bool IsContentElementCore() { return true; }
        protected override Rect GetBoundingRectangleCore() {
            Rect actual = base.GetBoundingRectangleCore();
            var group = (PaintedGroup)Owner;
            if (!group.Oversized || actual.IsEmpty) return actual;
            NativeRect window;
            if (!GetWindowRect(new WindowInteropHelper(Window.GetWindow(group)).Handle, out window))
                throw new InvalidOperationException("fixture_window_bounds");
            return new Rect(actual.X, actual.Y, window.Right + 107 - actual.X, actual.Height);
        }
    }
    protected override void OnRender(DrawingContext dc) {
        dc.DrawRectangle(Brushes.White, new Pen(Brushes.SteelBlue,1), new Rect(RenderSize));
        for (int row = 0; row < 3; row++) for (int column = 0; column < 2; column++) {
            string value = row == 0 ? (column == KeyColumn ? "Equip ID" : "Category") :
                column == KeyColumn ? (row == MatchRow ? Key : "OTHER") :
                (row == MatchRow ? (DuplicateColumnValue ? Key : "requested") : "decoy");
            var label = new FormattedText(value, System.Globalization.CultureInfo.InvariantCulture,
                FlowDirection.LeftToRight, new Typeface("Segoe UI"), 15, Brushes.Black,
                VisualTreeHelper.GetDpi(this).PixelsPerDip);
            dc.DrawText(label, new Point(10 + 180*column, 5+28*row));
        }
    }
}

sealed class LookupDetailsFixture : Window {
    public class Record { public string Key {get; set;} public string Note {get; set;} }
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly StackPanel main = new StackPanel();
    readonly TextBox input = new TextBox { Width=120, Text="OLD" };
    readonly ListView results = new ListView { Height=140, Margin=new Thickness(8) };
    readonly PaintedGroup opaque = new PaintedGroup { Height=140, Margin=new Thickness(8) };
    readonly ProviderResultGrid providerGrid = new ProviderResultGrid();
    readonly Button view = new Button { Content="VIEW", Width=70 };
    Window detail, cover;
    TextBox comment;
    Button exit;
    int queries, opened, closed, archiveClicks;
    int wrongRows;
    string last, resultKey = "OLD";
    [StructLayout(LayoutKind.Sequential)] struct NativeRect { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd, out NativeRect rect);

    void FormRow(string group, TextBox field, Button button) {
        var panel = new StackPanel { Orientation=Orientation.Horizontal, Margin=new Thickness(4) };
        var label = new Label { Content="Equip ID", Target=field, Width=85 };
        AutomationProperties.SetLabeledBy(field, label);
        AutomationProperties.SetAutomationId(field, "queryField");
        AutomationProperties.SetAutomationId(button, "searchButton");
        panel.Children.Add(label); panel.Children.Add(field); panel.Children.Add(button);
        var region = new GroupBox { Header=group, Content=panel };
        AutomationProperties.SetAutomationId(region, group);
        main.Children.Add(region);
    }

    GridViewColumn Column(string name, string prop, int width) {
        bool painted = Environment.GetCommandLineArgs().Contains("--painted");
        var cell = new FrameworkElementFactory(painted ? typeof(PaintedCell) : typeof(TextBlock));
        cell.SetBinding(painted ? PaintedCell.CaptionProperty : TextBlock.TextProperty, new Binding(prop));
        if (!painted) cell.SetBinding(AutomationProperties.NameProperty,new Binding(prop));
        return new GridViewColumn { Header=name, Width=width, CellTemplate=new DataTemplate { VisualTree=cell } };
    }
    void Fill(string key) {
        resultKey=key;
        providerGrid.Fill(key, queries);
        opaque.Key=key;
        if (opaque.Oversized) { opaque.KeyColumn=queries%2; opaque.MatchRow=queries%2==0?1:2; }
        opaque.InvalidateVisual();
        results.ItemsSource = new [] { new Record { Key="OTHER", Note="decoy" }, new Record { Key=key, Note="requested" } };
    }
    LookupDetailsFixture() {
        Title="Lookup demonstration"; Width=540; Height=370; WindowStartupLocation=WindowStartupLocation.CenterScreen;
        Content=main;
        FormRow("Filters",input,view);
        var spare=new Button {Content="VIEW", Width=70};
        spare.Click+=delegate {archiveClicks++;};
        FormRow("Archive",new TextBox { Width=120, Text="DECOY" },spare);
        var grid=new GridView();
        grid.Columns.Add(Column("Equip ID","Key",190)); grid.Columns.Add(Column("Category","Note",145));
        results.View=grid;
        AutomationProperties.SetAutomationId(results,"ResultGrid");
        bool groupOnly = Environment.GetCommandLineArgs().Contains("--group-only");
        opaque.Oversized = Environment.GetCommandLineArgs().Contains("--oversized-grid");
        opaque.DuplicateColumnValue = Environment.GetCommandLineArgs().Contains("--duplicate-column-value");
        AutomationProperties.SetAutomationId(opaque, "PaintedResults");
        bool providerOnly = Environment.GetCommandLineArgs().Contains("--provider-grid");
        providerGrid.OpenResult = OpenDetail;
        main.Children.Add(providerOnly ? (FrameworkElement)providerGrid : groupOnly ? (FrameworkElement)opaque : results);
        Fill(resultKey);
        view.Click+=delegate { queries++; Fill(input.Text); };
        results.MouseDoubleClick+=delegate(object s, MouseButtonEventArgs e) {
            var row=ItemsControl.ContainerFromElement(results,e.OriginalSource as DependencyObject) as ListViewItem;
            var rec=row==null?null:row.Content as Record;
            if(rec!=null) OpenDetail(rec.Key);
        };
        opaque.MouseLeftButtonDown+=delegate(object sender, MouseButtonEventArgs e) {
            Point p=e.GetPosition(opaque);
            if(e.ClickCount==2 && p.Y>=33 && p.Y<89) {
                if ((p.X < 180 ? 0 : 1) != opaque.KeyColumn) opaque.WrongColumns++;
                OpenDetail((p.Y<61?1:2)==opaque.MatchRow?resultKey:"OTHER");
            }
        };
        ContentRendered+=delegate {
            if (Environment.GetCommandLineArgs().Contains("--cover-during-review")) {
                cover=new Window {Title="Result review fixture cover",Width=Width,Height=Height,
                    Left=Left,Top=Top,ShowInTaskbar=false,Content=new TextBlock {Text="Isolated background cover fixture"}};
                cover.Show(); cover.Hide(); Activate();
            }
            Emit("ready"); new Thread(ReadCommands){IsBackground=true}.Start();
        };
    }
    void OpenDetail(string key) {
            if (detail!=null) return;
            if(key!=resultKey) wrongRows++;
            opened++; last=key;
            detail=new Window {Title="Item details",Width=320,Height=170,Owner=this,WindowStartupLocation=WindowStartupLocation.CenterOwner};
            var box=new StackPanel {Margin=new Thickness(12)};
            comment=new TextBox {Text="Note for "+last,IsReadOnly=true};
            AutomationProperties.SetName(comment,"Comment"); AutomationProperties.SetAutomationId(comment,"CommentText");
            exit=new Button {Content="Exit"}; AutomationProperties.SetAutomationId(exit,"btnClose");
            exit.Click+=delegate {closed++; detail.Close();};
            box.Children.Add(comment); box.Children.Add(exit); detail.Content=box;
            detail.Closed+=delegate {detail=null;}; detail.Show();
    }
    FrameworkElement Cell(DependencyObject root) {
        for(int i=0;i<VisualTreeHelper.GetChildrenCount(root);i++){
            var child=VisualTreeHelper.GetChild(root,i);
            var text=child as TextBlock;
            if(text!=null && text.Text==resultKey) return text;
            var painted=child as PaintedCell;
            if(painted!=null && painted.Caption==resultKey) return painted;
            var found=Cell(child); if(found!=null) return found;
        } return null;
    }
    void Emit(string command) {
        var hwnd=new WindowInteropHelper(this).Handle.ToInt64();
        if(command.StartsWith("review-layout") && opaque.IsVisible) {
            NativeRect window;
            if (!GetWindowRect(new IntPtr(hwnd),out window)) throw new InvalidOperationException("fixture_window_bounds");
            Func<double,double,int[]> imagePoint=(x,y)=>{
                var p=opaque.PointToScreen(new Point(x,y));
                return new []{(int)Math.Round(p.X)-window.Left,(int)Math.Round(p.Y)-window.Top};
            };
            int keyColumn=opaque.KeyColumn, matchRow=opaque.MatchRow;
            if(command=="review-layout-next" && opaque.Oversized) { keyColumn=(queries+1)%2; matchRow=(queries+1)%2==0?1:2; }
            var origin=imagePoint(0,0);
            var end=imagePoint(opaque.ActualWidth,opaque.ActualHeight);
            var rawEnd=opaque.Oversized?new []{window.Right-window.Left+107,end[1]}:end;
            Console.WriteLine(json.Serialize(new {
                headerLeft=imagePoint(6+180*keyColumn,15),headerRight=imagePoint(165+180*keyColumn,15),
                cell=imagePoint(35+180*keyColumn,16+28*matchRow),decoy=imagePoint(35+180*keyColumn,16+28*(3-matchRow)),
                visibleOrigin=origin,visibleEnd=new []{Math.Min(rawEnd[0],window.Right-window.Left),Math.Min(rawEnd[1],window.Bottom-window.Top)},
                rawOrigin=origin,rawEnd,
                groupOrigin=imagePoint(0,0),groupEnd=imagePoint(opaque.ActualWidth,opaque.ActualHeight),
                imageSize=new []{window.Right-window.Left,window.Bottom-window.Top}}));
        } else if(command.StartsWith("point:")){
            var key=command.Substring(6);
            var target= key=="field"?input:key=="view"?view:key=="cell"?Cell(results):key=="note"?(FrameworkElement)comment:exit;
            if(target!=null && target.IsVisible){
                var p=target.PointToScreen(new Point(target.ActualWidth/2,target.ActualHeight/2));
                Console.WriteLine(json.Serialize(new {x=(int)p.X,y=(int)p.Y,hwnd=new WindowInteropHelper(Window.GetWindow(target)).Handle.ToInt64()}));
            } else Console.WriteLine("{}");
        } else Console.WriteLine(json.Serialize(new {kind=command,hwnd,pid=Process.GetCurrentProcess().Id,title=Title,
            coverHwnd=cover==null?0:new WindowInteropHelper(cover).Handle.ToInt64(),
            resultKey,last,queries,opened,closed,archiveClicks,wrongRows,detailOpen=detail!=null,
            keyColumn=opaque.IsVisible?opaque.KeyColumn:providerGrid.KeyColumn,
            matchRow=opaque.IsVisible?opaque.MatchRow:providerGrid.MatchRow,
            wrongColumns=providerGrid.WrongColumns+opaque.WrongColumns,
            providerGrid.HeaderReads, providerGrid.ItemReads, providerGrid.ValueReads}));
        Console.Out.Flush();
    }
    void ReadCommands() {
        string cmd; while((cmd=Console.ReadLine())!=null){
            string current=cmd;
            Dispatcher.Invoke(new Action(delegate {
                if(current=="quit"){if(cover!=null)cover.Close();Close();return;}
                if(current=="focus")Activate();
                if(current=="reset"){input.Text="OLD";Fill("OLD");}
                if(current=="missing" || current=="duplicate" || current=="single" || current=="first" || current=="empty")
                    providerGrid.Scenario=current;
                Emit(current);
            }));
            if(cmd=="quit")break;
        }
    }
    [STAThread] public static void Main(){new Application().Run(new LookupDetailsFixture());}
}
