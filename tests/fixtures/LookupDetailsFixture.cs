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
    protected override AutomationPeer OnCreateAutomationPeer() { return new GroupPeer(this); }
    sealed class GroupPeer : FrameworkElementAutomationPeer {
        public GroupPeer(FrameworkElement owner) : base(owner) {}
        protected override AutomationControlType GetAutomationControlTypeCore() { return AutomationControlType.Group; }
        protected override bool IsControlElementCore() { return true; }
        protected override bool IsContentElementCore() { return true; }
    }
    protected override void OnRender(DrawingContext dc) {
        dc.DrawRectangle(Brushes.White, new Pen(Brushes.SteelBlue,1), new Rect(RenderSize));
        int index = 0;
        foreach (string value in new [] {"Equip ID","Category","OTHER","decoy",Key,"requested"}) {
            var label = new FormattedText(value, System.Globalization.CultureInfo.InvariantCulture,
                FlowDirection.LeftToRight, new Typeface("Segoe UI"), 15, Brushes.Black,
                VisualTreeHelper.GetDpi(this).PixelsPerDip);
            dc.DrawText(label, new Point(10 + 180*(index % 2), 5+28*(index/2))); index++;
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
    Window detail;
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
        opaque.Key=key; opaque.InvalidateVisual();
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
            double y=e.GetPosition(opaque).Y;
            if(e.ClickCount==2 && y>=33 && y<89) OpenDetail(y<61?"OTHER":resultKey);
        };
        ContentRendered+=delegate { Emit("ready"); new Thread(ReadCommands){IsBackground=true}.Start(); };
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
        if(command=="review-layout" && opaque.IsVisible) {
            NativeRect window;
            if (!GetWindowRect(new IntPtr(hwnd),out window)) throw new InvalidOperationException("fixture_window_bounds");
            Func<double,double,int[]> imagePoint=(x,y)=>{
                var p=opaque.PointToScreen(new Point(x,y));
                return new []{(int)Math.Round(p.X)-window.Left,(int)Math.Round(p.Y)-window.Top};
            };
            Console.WriteLine(json.Serialize(new {
                headerLeft=imagePoint(6,15),headerRight=imagePoint(165,15),cell=imagePoint(35,72),decoy=imagePoint(35,44),
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
            resultKey,last,queries,opened,closed,archiveClicks,wrongRows,detailOpen=detail!=null,
            keyColumn=providerGrid.KeyColumn, matchRow=providerGrid.MatchRow, wrongColumns=providerGrid.WrongColumns,
            providerGrid.HeaderReads, providerGrid.ItemReads, providerGrid.ValueReads}));
        Console.Out.Flush();
    }
    void ReadCommands() {
        string cmd; while((cmd=Console.ReadLine())!=null){
            string current=cmd;
            Dispatcher.Invoke(new Action(delegate {
                if(current=="quit"){Close();return;}
                if(current=="focus")Activate();
                if(current=="reset"){input.Text="OLD";Fill("OLD");}
                if(current=="missing" || current=="duplicate")providerGrid.Scenario=current;
                Emit(current);
            }));
            if(cmd=="quit")break;
        }
    }
    [STAThread] public static void Main(){new Application().Run(new LookupDetailsFixture());}
}
