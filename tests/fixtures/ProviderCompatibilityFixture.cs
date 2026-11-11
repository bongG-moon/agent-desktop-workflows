// Development-only raw UIA provider. These intentionally incomplete providers
// reproduce structural compatibility cases without company binaries or data.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Packaging;
using System.Runtime.InteropServices;
using System.Security;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Provider;
using System.Windows.Forms;

sealed class ProviderCompatibilityFixture : Form {
    internal readonly RawPeer Root;
    internal readonly Dictionary<string, ComboModel> Combos = new Dictionary<string, ComboModel>();
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    internal Rectangle ScreenBounds;
    internal bool Minimized;
    internal int Opens;
    internal int Exports, PropertyReads, Navigations, PatternReads, BoundsReads;
    internal string HiddenKey, WorkbookPath, ResultState, ResultFamily;
    internal ProviderCompatibilityFixture() {
        Text = "Synthetic provider compatibility"; ClientSize = new System.Drawing.Size(660, 250);
        StartPosition = FormStartPosition.Manual; Location = new System.Drawing.Point(45, 45);
        ShowInTaskbar = false; DoubleBuffered = true;
        Root = new RawPeer(this, null, 1, "root", "Synthetic provider compatibility", 50032, new Rectangle());
        AddCombo("conditionState", "MES Setup", new[] { "W", "N", "P", "C" }, "W", 32);
        AddCombo("conditionFamily", "Family", new[] { "Standard", "Hybrid", "DRAM" }, "Standard", 93);
        Root.Children.Add(new RawPeer(this, Root, 50, "QueryCommand", "Search", 50000, new Rectangle(180, 165, 150, 34)));
        Shown += delegate {
            RefreshBounds(); Emit("ready");
            new Thread(Read) { IsBackground = true }.Start();
        };
        LocationChanged += delegate { RefreshBounds(); };
        SizeChanged += delegate { RefreshBounds(); };
    }
    void AddCombo(string id, string label, string[] choices, string initial, int y) {
        int idBase = 10 + Combos.Count * 15;
        var labelPeer = new RawPeer(this, Root, idBase, id+"Label", label, 50020, new Rectangle(20, y, 145, 30));
        Root.Children.Add(labelPeer);
        var model = new ComboModel(id, initial, choices);
        Combos.Add(id, model);
        var combo = new RawPeer(this, Root, idBase+1, id, "", 50003, new Rectangle(180, y, 260, 32));
        combo.Combo = model; combo.Label = labelPeer; Root.Children.Add(combo);
        for (int i=0; i<2; i++) {
            var editor = new RawPeer(this, combo, idBase+2+i, id+"Editor"+i, "[Editor] Edit Area", 50004,
                new Rectangle(184, y+3, 235, 26));
            editor.Combo=model; editor.EditorIndex=i; combo.Children.Add(editor);
        }
        for (int i=0; i<choices.Length; i++) {
            var option = new RawPeer(this, combo, idBase+5+i, id+"Option"+i,
                "[Editor] [valuelist] ValueListItem "+i, 50007, new Rectangle());
            option.Combo=model; option.Choice=choices[i]; combo.Children.Add(option);
        }
    }
    internal void RefreshBounds() {
        Minimized = WindowState == FormWindowState.Minimized;
        ScreenBounds = Minimized ? new Rectangle() : RectangleToScreen(ClientRectangle);
        Invalidate();
    }
    internal void RefreshDisplay() { if (!IsDisposed) BeginInvoke(new Action(Invalidate)); }
    internal void CountPattern() { Interlocked.Increment(ref PatternReads); }
    internal void CountProperty() { Interlocked.Increment(ref PropertyReads); }
    internal void CountNavigation() { Interlocked.Increment(ref Navigations); }
    internal void CountBounds() { Interlocked.Increment(ref BoundsReads); }
    internal void Query() {
        Interlocked.Increment(ref Opens);
        ResultState=Combos["conditionState"].Current; ResultFamily=Combos["conditionFamily"].Current;
    }
    internal void Export() {
        Interlocked.Increment(ref Exports);
        if(String.IsNullOrEmpty(WorkbookPath))throw new InvalidOperationException("fixture_output_required");
        const string ss="http://schemas.openxmlformats.org/spreadsheetml/2006/main";
        const string rel="http://schemas.openxmlformats.org/officeDocument/2006/relationships";
        using(var package=Package.Open(WorkbookPath,FileMode.Create,FileAccess.ReadWrite)) {
            package.CreateRelationship(new Uri("xl/workbook.xml",UriKind.Relative),TargetMode.Internal,rel+"/officeDocument","rId1");
            var book=package.CreatePart(new Uri("/xl/workbook.xml",UriKind.Relative),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml");
            book.CreateRelationship(new Uri("worksheets/sheet1.xml",UriKind.Relative),TargetMode.Internal,rel+"/worksheet","rId1");
            WritePart(book,"<workbook xmlns='"+ss+"' xmlns:r='"+rel+"'><sheets><sheet name='Results' sheetId='1' r:id='rId1'/></sheets></workbook>");
            var sheet=package.CreatePart(new Uri("/xl/worksheets/sheet1.xml",UriKind.Relative),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml");
            WritePart(sheet,"<worksheet xmlns='"+ss+"'><sheetData><row r='1'>"+Cell("A1","MES Setup")+
                Cell("B1","Family")+Cell("C1","Generation")+"</row><row r='2'>"+Cell("A2",ResultState)+
                Cell("B2",ResultFamily)+Cell("C2",Opens.ToString())+"</row></sheetData></worksheet>");
        }
    }
    static void WritePart(PackagePart part,string text) {
        using(var writer=new StreamWriter(part.GetStream(),new UTF8Encoding(false)))writer.Write(text);
    }
    static string Cell(string key,string value) {
        return "<c r='"+key+"' t='inlineStr'><is><t>"+SecurityElement.Escape(value??"")+"</t></is></c>";
    }
    void ClearExtras() {
        Root.Children.RemoveAll(delegate(RawPeer peer) { return peer.Id>=900; }); HiddenKey=null;
    }
    void AddClutter(int count) {
        ClearExtras();
        for(int group=0,number=0;number<count;group++) {
            var peer=new RawPeer(this,Root,1000+group*400,"noiseGroup"+group,"Unrelated group",50026,new Rectangle(5,5,10,10));
            for(int child=0;child<100 && number<count;child++,number++)
                peer.Children.Add(new RawPeer(this,peer,1001+group*400+child,"noise"+number,"Unrelated field",50020,new Rectangle(5,5,5,5)));
            Root.Children.Insert(group,peer);
        }
    }
    protected override void OnPaint(PaintEventArgs e) {
        base.OnPaint(e);
        foreach (var peer in Root.Children) {
            if (peer.Role == 50003 || peer.Role == 50000) e.Graphics.DrawRectangle(Pens.Gray, peer.Relative);
            var text = peer.Role == 50003 ? peer.Combo.Current : peer.Name;
            e.Graphics.DrawString(text, Font, Brushes.Black, peer.Relative.X+5, peer.Relative.Y+7);
            if(peer.Combo!=null && (peer.Combo.Mode=="uncertain-select" || peer.Combo.Mode=="select-only" ||
                    peer.Combo.Mode=="uncertain-select-only")) {
                int index=0;
                foreach(var choice in peer.Combo.Choices) {
                    e.Graphics.DrawRectangle(Pens.Gray,470,25+index*32,160,28);
                    e.Graphics.DrawString(choice,Font,Brushes.Black,475,30+index*32); index++;
                }
            }
        }
        e.Graphics.DrawString("Raw UIA provider: missing visibility property; duplicate editors; virtual options", Font,
            Brushes.DarkSlateGray, 20, 220);
    }
    protected override void WndProc(ref Message m) {
        if (m.Msg == 0x003D && m.LParam.ToInt64() == AutomationInteropProvider.RootObjectId && Root != null) {
            m.Result = AutomationInteropProvider.ReturnRawElementProvider(Handle, m.WParam, m.LParam, Root); return;
        }
        base.WndProc(ref m);
    }
    void Emit(string kind) {
        var values = new Dictionary<string, object>();
        foreach (var pair in Combos) {
            var model=pair.Value;
            values[pair.Key]=new { value=model.Current, writes=model.Writes, expands=model.Expands,
                selects=model.Selects, mode=model.Mode, parent_value=model.Writes==0?"":model.Current };
        }
        Console.WriteLine(json.Serialize(new { kind=kind, title=Text, pid=Process.GetCurrentProcess().Id,
            hwnd=Handle.ToInt64(), minimized=Minimized, opens=Opens, exports=Exports, controls=values,
            result_state=ResultState, result_family=ResultFamily }));
        Console.Out.Flush();
    }
    void Read() {
        string line;
        while ((line=Console.ReadLine())!=null) {
            string command=line;
            try { Invoke(new Action(delegate {
                if(command=="quit") { Close(); return; }
                if(command=="metrics") {
                    Console.WriteLine(json.Serialize(new {kind="metrics",properties=PropertyReads,
                        navigations=Navigations,patterns=PatternReads,bounds=BoundsReads}));
                    Console.Out.Flush(); return;
                }
                if(command=="reset") { foreach(var model in Combos.Values)model.Reset(); Opens=Exports=0; ResultState=ResultFamily=""; HiddenKey=null; }
                else if(command=="minimize")WindowState=FormWindowState.Minimized;
                else if(command=="restore") { WindowState=FormWindowState.Normal; Activate(); }
                else if(command.StartsWith("title|"))Text=command.Substring(6);
                else if(command=="reset-metrics")PropertyReads=Navigations=PatternReads=BoundsReads=0;
                else if(command=="clear-extra")ClearExtras();
                else if(command.StartsWith("clutter|"))AddClutter(Int32.Parse(command.Substring(8)));
                else if(command.StartsWith("hide|"))HiddenKey=command.Substring(5);
                else if(command.StartsWith("duplicate|")) {
                    string key=command.Substring(10); var original=Root.Children.Find(delegate(RawPeer p) { return p.Key==key; });
                    var duplicate=new RawPeer(this,Root,900,key,original.Name,original.Role,original.Relative);
                    duplicate.Combo=original.Combo; duplicate.Label=original.Label; Root.Children.Add(duplicate);
                }
                else if(command.StartsWith("export-path|")) {
                    WorkbookPath=command.Substring(12);
                    if(!Root.Children.Exists(delegate(RawPeer p) { return p.Key=="ExportCommand"; }))
                        Root.Children.Add(new RawPeer(this,Root,60,"ExportCommand","Excel",50000,new Rectangle(360,165,150,34)));
                }
                else if(command.StartsWith("mode|")) { var parts=command.Split('|'); Combos[parts[1]].Mode=parts[2]; }
                RefreshBounds(); Emit(command=="state"?"state":"updated");
            })); } catch { return; }
        }
    }
    [STAThread] static void Main() { Application.EnableVisualStyles(); Application.Run(new ProviderCompatibilityFixture()); }
}

sealed class ComboModel {
    internal readonly string Id, Initial;
    internal readonly string[] Choices;
    internal string Current, Mode="normal";
    internal int Writes, Expands, Selects;
    internal ComboModel(string id, string initial, string[] choices) { Id=id; Initial=Current=initial; Choices=choices; }
    internal void Reset() { Current=Initial; Mode="normal"; Writes=Expands=Selects=0; }
}

// This pattern is absent from the .NET 4 provider assembly. The ABI below is
// the Windows SDK UIAutomationCore.idl ILegacyIAccessibleProvider definition.
[ComVisible(true), Guid("e44c3566-915d-4070-99c6-047bff5a08f5"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IFixtureLegacyProvider {
    void Select(int flags);
    void DoDefaultAction();
    void SetValue([MarshalAs(UnmanagedType.LPWStr)] string value);
    [return: MarshalAs(UnmanagedType.Interface)] object GetIAccessible();
    int ChildId { get; }
    string Name { [return: MarshalAs(UnmanagedType.BStr)] get; }
    string Value { [return: MarshalAs(UnmanagedType.BStr)] get; }
    string Description { [return: MarshalAs(UnmanagedType.BStr)] get; }
    uint Role { get; }
    uint State { get; }
    string Help { [return: MarshalAs(UnmanagedType.BStr)] get; }
    string KeyboardShortcut { [return: MarshalAs(UnmanagedType.BStr)] get; }
    [return: MarshalAs(UnmanagedType.SafeArray, SafeArraySubType=VarEnum.VT_UNKNOWN)]
    IRawElementProviderSimple[] GetSelection();
    string DefaultAction { [return: MarshalAs(UnmanagedType.BStr)] get; }
}

sealed class RawPeer : IRawElementProviderFragmentRoot, IValueProvider, IExpandCollapseProvider,
    ISelectionItemProvider, ISelectionProvider, IInvokeProvider, IFixtureLegacyProvider {
    internal readonly ProviderCompatibilityFixture Host;
    internal readonly RawPeer Parent;
    internal readonly int Id, Role;
    internal readonly string Key, Name;
    internal readonly Rectangle Relative;
    internal readonly List<RawPeer> Children=new List<RawPeer>();
    internal ComboModel Combo;
    internal RawPeer Label;
    internal string Choice;
    internal int EditorIndex;
    internal RawPeer(ProviderCompatibilityFixture host, RawPeer parent, int id, string key, string name,
        int role, Rectangle relative) { Host=host; Parent=parent; Id=id; Key=key; Name=name; Role=role; Relative=relative; }
    public ProviderOptions ProviderOptions { get { return ProviderOptions.ServerSideProvider; } }
    public IRawElementProviderSimple HostRawElementProvider {
        get { return Parent==null ? AutomationInteropProvider.HostProviderFromHandle(Host.Handle) : null; }
    }
    public object GetPatternProvider(int id) {
        Host.CountPattern();
        if (id==10002 && ValueSupported)return this;
        if (id==10001 && Role==50003 && SelectionAvailable)return this;
        if (id==10005 && Role==50003)return this;
        if (id==10010 && Role==50007)return this;
        if (id==10000 && Role==50000)return this;
        if (id==10018 && LegacyEnabled)return this;
        return null;
    }
    bool SelectOnly { get { return Combo!=null && (Combo.Mode=="select-only" || Combo.Mode=="uncertain-select-only"); } }
    bool SelectionAvailable { get { return Combo!=null && Combo.Mode=="selected-after-write"; } }
    bool ValueSupported { get { return (Role==50003 && !SelectOnly) || Role==50004 ||
        (Role==50007 && Combo.Mode!="unsupported-option"); } }
    bool LegacyEnabled { get { return Combo!=null && (Combo.Mode=="legacy-all" ||
        (Role==50007 && Combo.Mode=="legacy-option")); } }
    public object GetPropertyValue(int id) {
        Host.CountProperty();
        switch(id) {
            case 30002: return Process.GetCurrentProcess().Id;
            case 30003: return Role;
            case 30005: return Role==50007 && Combo.Mode=="unsupported-option" ? Choice : Name;
            case 30008: return false;
            case 30009: return Role==50003 || Role==50004 || Role==50000;
            case 30010: return true;
            case 30011: return Key;
            case 30012: return "RawCompatibilityProvider";
            case 30013: return "";
            case 30016: case 30017: return true;
            case 30018: return Label;
            case 30019: return false;
            case 30020: return Parent==null ? Host.Handle.ToInt32() : 0;
            // null is UIA NotSupported, not false. GetCurrentPropertyValueEx(true)
            // must preserve this distinction; WPF bool-only peers cannot model it.
            case 30022: return Combo!=null && Combo.Mode=="explicit-offscreen" ? (object)true : null;
            case 30024: return "SyntheticRawUIA";
            case 30028: return Role==50003;
            case 30031: return Role==50000;
            case 30036: return Role==50007;
            case 30037: return Role==50003 && SelectionAvailable;
            case 30043: return ValueSupported;
            case 30045: return ((IValueProvider)this).Value;
            case 30046: return Role!=50003 || SelectOnly;
            case 30070: return 0; // Expand succeeds but this custom provider never updates its state.
            case 30079: return ((ISelectionItemProvider)this).IsSelected;
            case 30080: return Role==50007 ? Parent : null;
            case 30090: return LegacyEnabled;
            case 30093: return LegacyEnabled ? ((IFixtureLegacyProvider)this).Value : null;
        }
        return null;
    }
    public IRawElementProviderFragment Navigate(NavigateDirection dir) {
        Host.CountNavigation();
        if(dir==NavigateDirection.Parent)return Parent;
        var children=NavigableChildren();
        if(dir==NavigateDirection.FirstChild)return children.Count==0?null:children[0];
        if(dir==NavigateDirection.LastChild)return children.Count==0?null:children[children.Count-1];
        if(Parent==null)return null;
        var siblings=Parent.NavigableChildren();
        int index=siblings.IndexOf(this)+(dir==NavigateDirection.NextSibling?1:-1);
        return index<0 || index>=siblings.Count ? null : siblings[index];
    }
    List<RawPeer> NavigableChildren() {
        var result=new List<RawPeer>();
        foreach(var child in Children)
            if(child.Key!=Host.HiddenKey && !(Combo!=null && Combo.Mode=="no-options-blank-before" && child.Role==50007))result.Add(child);
        return result;
    }
    public int[] GetRuntimeId() { return Parent==null ? null : new[] { AutomationInteropProvider.AppendRuntimeId, Id }; }
    public Rect BoundingRectangle {
        get {
            Host.CountBounds();
            var host=Host.ScreenBounds;
            if(host.Width==0 || host.Height==0)return Rect.Empty;
            if(Parent==null)return new Rect(host.X,host.Y,host.Width,host.Height);
            if(Role==50007 && (Combo.Mode=="uncertain-select" || SelectOnly))
                return new Rect(host.X+470,host.Y+25+Array.IndexOf(Combo.Choices,Choice)*32,160,28);
            if(Relative.Width==0 || Relative.Height==0)return Rect.Empty;
            return new Rect(host.X+Relative.X,host.Y+Relative.Y,Relative.Width,Relative.Height);
        }
    }
    public IRawElementProviderSimple[] GetEmbeddedFragmentRoots() { return null; }
    public void SetFocus() {}
    public IRawElementProviderFragmentRoot FragmentRoot { get { return Host.Root; } }
    public IRawElementProviderFragment ElementProviderFromPoint(double x,double y) {
        foreach(var child in Children)if(child.BoundingRectangle.Contains(x,y))return child.ElementProviderFromPoint(x,y);
        return this;
    }
    public IRawElementProviderFragment GetFocus() { return Host.Root; }
    string IValueProvider.Value {
        get {
            if(Combo==null)return "";
            if(Combo.Mode=="legacy-all")return "";
            if(Combo.Mode=="no-options-blank-before" && Combo.Writes==0)return "";
            if(SelectionAvailable && Combo.Writes==0 && (Role==50003 || Role==50004))return "";
            if(Role==50003)return Combo.Writes==0 ? "" : Combo.Current;
            if(Role==50007)return Combo.Mode=="legacy-option" ? "" : Choice;
            if(Role==50004 && Combo.Mode=="conflict-editors" && EditorIndex==1)return "CONFLICT";
            return Combo.Current;
        }
    }
    bool IValueProvider.IsReadOnly { get { return Role!=50003 || SelectOnly; } }
    void IValueProvider.SetValue(string value) {
        if(Role!=50003)throw new InvalidOperationException("fixture_readonly");
        Combo.Writes++;
        if(Array.IndexOf(Combo.Choices,value)<0)throw new ArgumentException("fixture_unknown_choice");
        Combo.Current=value; Host.RefreshDisplay();
        if(Combo.Mode=="uncertain-write")throw new COMException("fixture_mutated_then_failed",unchecked((int)0x80131515));
    }
    ExpandCollapseState IExpandCollapseProvider.ExpandCollapseState { get { return ExpandCollapseState.Collapsed; } }
    void IExpandCollapseProvider.Expand() {
        Combo.Expands++;
        if(Combo.Mode=="uncertain-expand")throw new COMException("fixture_expand_failed",unchecked((int)0x80131515));
    }
    void IExpandCollapseProvider.Collapse() {}
    bool ISelectionItemProvider.IsSelected { get { return Role==50007 && SelectionAvailable &&
        Combo.Writes>0 && Combo.Current==Choice; } }
    IRawElementProviderSimple ISelectionItemProvider.SelectionContainer { get { return Parent; } }
    bool ISelectionProvider.CanSelectMultiple { get { return false; } }
    bool ISelectionProvider.IsSelectionRequired { get { return false; } }
    IRawElementProviderSimple[] ISelectionProvider.GetSelection() {
        var selected=new List<IRawElementProviderSimple>();
        if(Role==50003 && SelectionAvailable && Combo.Writes>0)
            foreach(var child in Children)
                if(child.Role==50007 && child.Choice==Combo.Current)selected.Add(child);
        return selected.ToArray();
    }
    void ISelectionItemProvider.Select() {
        Combo.Selects++;
        if(Combo.Mode=="select-only") { Combo.Current=Choice; Host.RefreshDisplay(); return; }
        throw new COMException("virtual_option_select_failed",unchecked((int)0x80131515));
    }
    void ISelectionItemProvider.AddToSelection() { ((ISelectionItemProvider)this).Select(); }
    void ISelectionItemProvider.RemoveFromSelection() { throw new InvalidOperationException(); }
    void IInvokeProvider.Invoke() { if(Key=="ExportCommand")Host.Export(); else Host.Query(); }
    void IFixtureLegacyProvider.Select(int flags) { throw new InvalidOperationException("legacy_readonly"); }
    void IFixtureLegacyProvider.DoDefaultAction() { throw new InvalidOperationException("legacy_readonly"); }
    void IFixtureLegacyProvider.SetValue(string value) { throw new InvalidOperationException("legacy_readonly"); }
    object IFixtureLegacyProvider.GetIAccessible() { return null; }
    int IFixtureLegacyProvider.ChildId { get { return 0; } }
    string IFixtureLegacyProvider.Name { get { return Name; } }
    string IFixtureLegacyProvider.Value { get {
        if(Role==50007)return Choice;
        if(Role==50003)return Combo.Writes==0 ? "" : Combo.Current;
        return Combo.Current;
    } }
    string IFixtureLegacyProvider.Description { get { return ""; } }
    uint IFixtureLegacyProvider.Role { get { return Role==50007 ? 0x22u : Role==50003 ? 0x2eu : 0x2au; } }
    uint IFixtureLegacyProvider.State { get { return 0; } }
    string IFixtureLegacyProvider.Help { get { return ""; } }
    string IFixtureLegacyProvider.KeyboardShortcut { get { return ""; } }
    IRawElementProviderSimple[] IFixtureLegacyProvider.GetSelection() { return new IRawElementProviderSimple[0]; }
    string IFixtureLegacyProvider.DefaultAction { get { return ""; } }
}
