// Development-only UIA provider fixture. No production application is targeted.
// A blank parent ValuePattern is deliberate, independent of the visible/model value.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Peers;
using System.Windows.Automation.Provider;
using System.Windows.Controls;
using System.Windows.Interop;

sealed class EvidenceCombo : UserControl {
    internal readonly string Key;
    internal readonly bool SelectedEvidence, EditorEvidence, IgnoreWrites, Conflict;
    internal readonly List<EvidenceOption> Options = new List<EvidenceOption>();
    internal readonly TextBox Editor;
    internal string Current = "Y";
    internal int WriteCount;
    readonly TextBlock caption;
    internal EvidenceCombo(string key, bool selection, bool editor, bool ignore, bool conflict) {
        Key=key; SelectedEvidence=selection; EditorEvidence=editor; IgnoreWrites=ignore; Conflict=conflict;
        AutomationProperties.SetAutomationId(this,key); AutomationProperties.SetName(this,key);
        Margin=new Thickness(12,5,12,5); Padding=new Thickness(10); Focusable=true;
        var row=new StackPanel { Orientation=Orientation.Horizontal };
        caption=new TextBlock { Width=185, VerticalAlignment=VerticalAlignment.Center };
        row.Children.Add(caption);
        if(editor) {
            Editor=new TextBox { Width=105, Height=28, Margin=new Thickness(0,0,12,0), Text="Y" };
            AutomationProperties.SetAutomationId(Editor,key+"Editor");
            AutomationProperties.SetName(Editor,"Current choice editor");
            row.Children.Add(Editor);
        }
        foreach(string value in new[]{"Y","N"}) {
            var option=new EvidenceOption(this,value) { Width=65, Height=28, Margin=new Thickness(0,0,8,0) };
            Options.Add(option); row.Children.Add(option);
        }
        Content=row; Refresh();
    }
    internal void Change(string value) {
        if(value!="Y"&&value!="N")throw new ArgumentException("fixture_value_not_allowed");
        Current=value; Refresh();
    }
    internal void Write(string value) { WriteCount++; if(!IgnoreWrites)Change(value); }
    internal bool IsSelected(string value) { return value==(Conflict?"N":Current); }
    void Refresh() {
        caption.Text=Key+" = "+Current;
        if(Editor!=null)Editor.Text=Conflict?"Y":Current;
    }
    protected override AutomationPeer OnCreateAutomationPeer() { return new EvidenceComboPeer(this); }
}

sealed class EvidenceComboPeer : FrameworkElementAutomationPeer, IValueProvider, ISelectionProvider {
    EvidenceCombo Combo { get { return (EvidenceCombo)Owner; } }
    internal EvidenceComboPeer(EvidenceCombo owner) : base(owner) {}
    protected override AutomationControlType GetAutomationControlTypeCore() { return AutomationControlType.ComboBox; }
    protected override string GetClassNameCore() { return "SyntheticBlankValueCombo"; }
    protected override bool IsControlElementCore() { return true; }
    protected override bool IsContentElementCore() { return true; }
    protected override List<AutomationPeer> GetChildrenCore() {
        var peers=new List<AutomationPeer>();
        if(Combo.Editor!=null)peers.Add(UIElementAutomationPeer.CreatePeerForElement(Combo.Editor));
        foreach(var option in Combo.Options)peers.Add(UIElementAutomationPeer.CreatePeerForElement(option));
        return peers;
    }
    public override object GetPattern(PatternInterface pattern) {
        if(pattern==PatternInterface.Value)return this;
        if(pattern==PatternInterface.Selection&&Combo.SelectedEvidence)return this;
        return base.GetPattern(pattern);
    }
    string IValueProvider.Value { get { return ""; } }
    bool IValueProvider.IsReadOnly { get { return false; } }
    void IValueProvider.SetValue(string value) { Combo.Write(value); }
    bool ISelectionProvider.CanSelectMultiple { get { return false; } }
    bool ISelectionProvider.IsSelectionRequired { get { return true; } }
    IRawElementProviderSimple[] ISelectionProvider.GetSelection() {
        foreach(var option in Combo.Options)if(Combo.IsSelected(option.Choice))
            return new[]{ProviderFromPeer(UIElementAutomationPeer.CreatePeerForElement(option))};
        return new IRawElementProviderSimple[0];
    }
}

sealed class EvidenceOption : Button {
    internal readonly EvidenceCombo Combo;
    internal readonly string Choice;
    internal EvidenceOption(EvidenceCombo combo,string choice) {
        Combo=combo; Choice=choice; Content=choice;
        AutomationProperties.SetAutomationId(this,combo.Key+"Option"+choice);
        AutomationProperties.SetName(this,choice);
        Click+=delegate { Combo.Change(Choice); };
    }
    protected override AutomationPeer OnCreateAutomationPeer() { return new EvidenceOptionPeer(this); }
}

sealed class EvidenceOptionPeer : ButtonAutomationPeer, ISelectionItemProvider {
    EvidenceOption Option { get { return (EvidenceOption)Owner; } }
    internal EvidenceOptionPeer(EvidenceOption owner) : base(owner) {}
    protected override AutomationControlType GetAutomationControlTypeCore() {
        return Option.Combo.SelectedEvidence?AutomationControlType.ListItem:AutomationControlType.Button;
    }
    public override object GetPattern(PatternInterface pattern) {
        if(pattern==PatternInterface.SelectionItem&&Option.Combo.SelectedEvidence)return this;
        return base.GetPattern(pattern);
    }
    bool ISelectionItemProvider.IsSelected { get { return Option.Combo.IsSelected(Option.Choice); } }
    IRawElementProviderSimple ISelectionItemProvider.SelectionContainer {
        get { return ProviderFromPeer(UIElementAutomationPeer.CreatePeerForElement(Option.Combo)); }
    }
    void ISelectionItemProvider.Select() { Option.Combo.Change(Option.Choice); }
    void ISelectionItemProvider.AddToSelection() { Option.Combo.Change(Option.Choice); }
    void ISelectionItemProvider.RemoveFromSelection() { throw new InvalidOperationException("selection_required"); }
}

sealed class ComboEvidenceFixture : Window {
    readonly JavaScriptSerializer json=new JavaScriptSerializer();
    readonly Dictionary<string,EvidenceCombo> controls=new Dictionary<string,EvidenceCombo>();
    internal ComboEvidenceFixture() {
        Title="Agent Desktop Workflows Synthetic Combo Evidence";
        Width=640; Height=420; Left=40; Top=40;
        WindowStartupLocation=WindowStartupLocation.Manual; ShowInTaskbar=false;
        var panel=new StackPanel();
        panel.Children.Add(new TextBlock { Text="Synthetic UIA: blank parent value, independent evidence", Margin=new Thickness(18), FontSize=16 });
        Add(panel,new EvidenceCombo("SelectionEvidenceCombo",true,false,false,false));
        Add(panel,new EvidenceCombo("EditorEvidenceCombo",false,true,false,false));
        Add(panel,new EvidenceCombo("OpaqueCombo",false,false,false,false));
        Add(panel,new EvidenceCombo("IgnoredWriteCombo",true,false,true,false));
        Add(panel,new EvidenceCombo("ConflictingCombo",true,true,false,true));
        Content=panel;
        Loaded+=delegate {
            Emit("ready");
            var reader=new Thread(Read) { IsBackground=true }; reader.Start();
        };
    }
    void Add(StackPanel panel,EvidenceCombo combo) { controls.Add(combo.Key,combo); panel.Children.Add(combo); }
    void Emit(string kind) {
        var values=new Dictionary<string,object>();
        foreach(var entry in controls)values.Add(entry.Key,new{value=entry.Value.Current,writes=entry.Value.WriteCount});
        Console.WriteLine(json.Serialize(new{kind=kind,pid=Process.GetCurrentProcess().Id,
            hwnd=new WindowInteropHelper(this).Handle.ToInt64(),controls=values}));
        Console.Out.Flush();
    }
    void Read() {
        string line;
        while((line=Console.ReadLine())!=null) {
            string command=line;
            try { Dispatcher.Invoke(new Action(delegate {
                if(command=="quit") { Close(); return; }
                if(command=="reset")foreach(var combo in controls.Values) { combo.Change("Y"); combo.WriteCount=0; }
                else if(command=="activate")Activate();
                else if(command.StartsWith("set|")) { var parts=command.Split('|'); controls[parts[1]].Change(parts[2]); }
                Emit(command=="state"?"state":"updated");
            })); } catch { return; }
        }
    }
    [STAThread] static void Main() { new Application().Run(new ComboEvidenceFixture()); }
}
