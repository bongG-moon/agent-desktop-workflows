// Development-only fixture: real transient owned popup, blank parent UIA value.
// Input/model counters are reported independently from the accessibility tree.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Peers;
using System.Windows.Automation.Provider;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;

sealed class PopupCombo : UserControl {
    internal readonly string Key;
    internal readonly bool Readable, RejectChoice;
    internal readonly Button Arrow;
    internal readonly TextBox Editor;
    internal string Current = "Y";
    internal int WriteCount, ClickCount, OpenCount, CancelCount;
    internal Window Popup;
    internal readonly List<PopupOption> Options = new List<PopupOption>();
    readonly TextBlock display;

    internal PopupCombo(string key, bool readable, bool reject) {
        Key=key; Readable=readable; RejectChoice=reject;
        AutomationProperties.SetAutomationId(this,key);
        Focusable=true; Width=205; Height=36;
        var row=new DockPanel();
        Arrow=new Button { Content="v", Width=34, Height=32 };
        AutomationProperties.SetAutomationId(Arrow,key+"Open");
        AutomationProperties.SetName(Arrow,"Open choices");
        DockPanel.SetDock(Arrow,Dock.Right); row.Children.Add(Arrow);
        Arrow.Click+=delegate { Open(); };
        if(readable) {
            Editor=new TextBox { Text=Current, IsReadOnly=true, Height=32, Focusable=false };
            AutomationProperties.SetAutomationId(Editor,key+"Current");
            row.Children.Add(Editor);
        } else {
            display=new TextBlock { Text=Current, VerticalAlignment=VerticalAlignment.Center, Margin=new Thickness(8,0,0,0) };
            row.Children.Add(display);
        }
        Content=row;
    }

    internal void Open() {
        if(Popup!=null)return;
        var owner=Window.GetWindow(this);
        Popup=new Window { Owner=owner, WindowStyle=WindowStyle.None, ResizeMode=ResizeMode.NoResize,
            ShowInTaskbar=false, ShowActivated=true, Width=205, Height=104,
            Background=Brushes.White, Title=Key+" choices" };
        AutomationProperties.SetAutomationId(Popup,Key+"Popup");
        var origin=PointToScreen(new Point(0,ActualHeight));
        var source=PresentationSource.FromVisual(this);
        var logical=source.CompositionTarget.TransformFromDevice.Transform(origin);
        Popup.Left=logical.X; Popup.Top=logical.Y;
        var list=new StackPanel();
        Options.Clear();
        foreach(string choice in new[]{"Y","N","P"}) {
            var option=new PopupOption(this,choice) { Height=32, HorizontalContentAlignment=HorizontalAlignment.Left };
            Options.Add(option); list.Children.Add(option);
        }
        Popup.Content=list;
        Popup.PreviewKeyDown+=delegate(object sender,KeyEventArgs e) {
            if(e.Key==System.Windows.Input.Key.Escape) { CancelCount++; ClosePopup(); e.Handled=true; }
        };
        OpenCount++; Popup.Show();
        // Keyboard navigation is native button focus; no fixture command selects values.
        Options[0].Focus();
    }

    internal void ClosePopup() {
        var popup=Popup; Popup=null;
        if(popup!=null)popup.Close();
    }
    internal void Choose(string value) {
        ClickCount++;
        if(!RejectChoice)Change(value);
        ClosePopup(); // Destroy the option window immediately after the actual click.
    }
    internal void Change(string value) {
        if(value!="Y"&&value!="N"&&value!="P")throw new ArgumentException("fixture_value_not_allowed");
        Current=value;
        if(Editor!=null)Editor.Text=value;
        if(display!=null)display.Text=value;
    }
    internal void Write(string value) { WriteCount++; Change(value); }
    internal void Reset() { ClosePopup(); Change("Y"); WriteCount=ClickCount=OpenCount=CancelCount=0; }
    protected override AutomationPeer OnCreateAutomationPeer() { return new PopupComboPeer(this); }
}

sealed class PopupComboPeer : FrameworkElementAutomationPeer, IValueProvider, IExpandCollapseProvider {
    PopupCombo Combo { get { return (PopupCombo)Owner; } }
    internal PopupComboPeer(PopupCombo combo) : base(combo) {}
    protected override AutomationControlType GetAutomationControlTypeCore() { return AutomationControlType.ComboBox; }
    protected override string GetClassNameCore() { return "SyntheticTransientPopupCombo"; }
    protected override string GetNameCore() { return ""; }
    protected override bool IsControlElementCore() { return true; }
    protected override bool IsContentElementCore() { return true; }
    protected override List<AutomationPeer> GetChildrenCore() {
        var peers=new List<AutomationPeer>();
        if(Combo.Editor!=null)peers.Add(UIElementAutomationPeer.CreatePeerForElement(Combo.Editor));
        peers.Add(UIElementAutomationPeer.CreatePeerForElement(Combo.Arrow));
        // Deliberately do not attach popup items as combo children. Ownership must
        // be proven through SelectionItem.SelectionContainer, not UIA ancestry.
        return peers;
    }
    public override object GetPattern(PatternInterface pattern) {
        if(pattern==PatternInterface.Value||pattern==PatternInterface.ExpandCollapse)return this;
        return base.GetPattern(pattern);
    }
    string IValueProvider.Value { get { return ""; } }
    bool IValueProvider.IsReadOnly { get { return false; } }
    void IValueProvider.SetValue(string value) { Combo.Write(value); }
    ExpandCollapseState IExpandCollapseProvider.ExpandCollapseState {
        get { return Combo.Popup==null?ExpandCollapseState.Collapsed:ExpandCollapseState.Expanded; }
    }
    void IExpandCollapseProvider.Expand() { Combo.Open(); }
    void IExpandCollapseProvider.Collapse() { Combo.ClosePopup(); }
}

sealed class PopupOption : Button {
    internal readonly PopupCombo Combo;
    internal readonly string Choice;
    internal PopupOption(PopupCombo combo,string choice) {
        Combo=combo; Choice=choice; Content=choice;
        AutomationProperties.SetAutomationId(this,combo.Key+"Option"+choice);
        AutomationProperties.SetName(this,choice);
        Click+=delegate { Combo.Choose(Choice); };
    }
    protected override AutomationPeer OnCreateAutomationPeer() { return new PopupOptionPeer(this); }
}

sealed class PopupOptionPeer : ButtonAutomationPeer, ISelectionItemProvider {
    PopupOption Option { get { return (PopupOption)Owner; } }
    internal PopupOptionPeer(PopupOption option) : base(option) {}
    protected override AutomationControlType GetAutomationControlTypeCore() { return AutomationControlType.ListItem; }
    public override object GetPattern(PatternInterface pattern) {
        if(pattern==PatternInterface.SelectionItem)return this;
        return base.GetPattern(pattern);
    }
    // Opaque controls expose relation but no useful current selection evidence.
    bool ISelectionItemProvider.IsSelected { get { return Option.Combo.Readable&&Option.Combo.Current==Option.Choice; } }
    IRawElementProviderSimple ISelectionItemProvider.SelectionContainer {
        get { return ProviderFromPeer(UIElementAutomationPeer.CreatePeerForElement(Option.Combo)); }
    }
    void ISelectionItemProvider.Select() { Option.Combo.Choose(Option.Choice); }
    void ISelectionItemProvider.AddToSelection() { Option.Combo.Choose(Option.Choice); }
    void ISelectionItemProvider.RemoveFromSelection() { throw new InvalidOperationException("selection_required"); }
}

sealed class ComboPopupFixture : Window {
    readonly JavaScriptSerializer json=new JavaScriptSerializer();
    readonly Dictionary<string,PopupCombo> controls=new Dictionary<string,PopupCombo>();
    internal ComboPopupFixture() {
        Title="Agent Desktop Workflows Synthetic Popup Combos";
        Width=680; Height=410; Left=60; Top=60; ShowInTaskbar=false;
        WindowStartupLocation=WindowStartupLocation.Manual;
        var panel=new StackPanel { Margin=new Thickness(22) };
        panel.Children.Add(new TextBlock { Text="Synthetic custom combos: blank parent name/value", FontSize=17, Margin=new Thickness(0,0,0,18) });
        Add(panel,"ComboAlpha","Processing mode",true,false);
        Add(panel,"ComboBeta","Release state",false,false);
        Add(panel,"ComboGamma","Secondary state",false,false);
        Add(panel,"ComboReject","Rejected change",true,true);
        Content=panel;
        Loaded+=delegate {
            Emit("ready");
            new Thread(Read) { IsBackground=true }.Start();
        };
    }
    void Add(StackPanel panel,string key,string text,bool readable,bool reject) {
        var row=new StackPanel { Orientation=Orientation.Horizontal, Margin=new Thickness(0,0,0,14) };
        var label=new Label { Content=text, Width=180, VerticalAlignment=VerticalAlignment.Center };
        AutomationProperties.SetAutomationId(label,key+"Label");
        var combo=new PopupCombo(key,readable,reject);
        AutomationProperties.SetLabeledBy(combo,label);
        label.Target=combo;
        row.Children.Add(label); row.Children.Add(combo); panel.Children.Add(row); controls.Add(key,combo);
    }
    void Emit(string kind) {
        var values=new Dictionary<string,object>();
        foreach(var item in controls)values.Add(item.Key,new { value=item.Value.Current,writes=item.Value.WriteCount,
            clicks=item.Value.ClickCount,opens=item.Value.OpenCount,cancels=item.Value.CancelCount,popup=item.Value.Popup!=null });
        Console.WriteLine(json.Serialize(new { kind=kind,pid=Process.GetCurrentProcess().Id,
            hwnd=new WindowInteropHelper(this).Handle.ToInt64(),controls=values }));
        Console.Out.Flush();
    }
    void Read() {
        string line;
        while((line=Console.ReadLine())!=null) {
            string command=line;
            try { Dispatcher.Invoke(new Action(delegate {
                if(command=="quit") { foreach(var combo in controls.Values)combo.ClosePopup(); Close(); return; }
                if(command=="reset")foreach(var combo in controls.Values)combo.Reset();
                else if(command=="activate")Activate();
                // No select/open/set test command: recorded interaction must use actual input.
                Emit(command=="state"?"state":"updated");
            })); } catch { return; }
        }
    }
    [STAThread] static void Main() { new Application().Run(new ComboPopupFixture()); }
}
