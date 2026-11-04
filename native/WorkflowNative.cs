// Independent local workflow recorder and bounded Windows accessibility bridge.
// No network, clipboard, service, driver, or other automation-product dependency.
using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using System.Windows.Forms;

internal static class WorkflowNative {
    internal static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 16 * 1024 * 1024, RecursionLimit = 80 };
    [StructLayout(LayoutKind.Sequential)] internal struct POINT { public int X, Y; public POINT(int x, int y) { X = x; Y = y; } }
    [StructLayout(LayoutKind.Sequential)] internal struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] struct MOUSEHOOK { public POINT pt; public uint data, flags, time; public UIntPtr extra; }
    [StructLayout(LayoutKind.Sequential)] struct KEYHOOK { public uint code, scan, flags, time; public UIntPtr extra; }
    [StructLayout(LayoutKind.Sequential)] struct MOUSEINPUT { public int dx, dy; public uint data, flags, time; public UIntPtr extra; }
    [StructLayout(LayoutKind.Explicit)] struct INPUTUNION { [FieldOffset(0)] public MOUSEINPUT mouse; }
    [StructLayout(LayoutKind.Sequential)] struct INPUT { public uint type; public INPUTUNION value; }
    delegate bool EnumWindow(IntPtr window, IntPtr data);
    delegate IntPtr HookProc(int code, IntPtr message, IntPtr data);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindow callback, IntPtr data);
    [DllImport("user32.dll")] internal static extern bool IsWindow(IntPtr window);
    [DllImport("user32.dll")] internal static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] internal static extern bool IsIconic(IntPtr window);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr window, int command);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")] internal static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern IntPtr GetAncestor(IntPtr window, uint flags);
    [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr window, uint command);
    [DllImport("user32.dll")] static extern bool IsChild(IntPtr parent, IntPtr child);
    [DllImport("user32.dll")] static extern IntPtr WindowFromPoint(POINT point);
    [DllImport("user32.dll")] static extern bool GetCursorPos(out POINT point);
    [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr window, out RECT rect);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr window, StringBuilder text, int length);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr window, StringBuilder text, int length);
    [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr window);
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int metric);
    [DllImport("user32.dll")] static extern short GetAsyncKeyState(int key);
    [DllImport("user32.dll", SetLastError=true)] static extern uint SendInput(uint count, INPUT[] inputs, int size);
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr SetWindowsHookEx(int id, HookProc callback, IntPtr module, uint thread);
    [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr hook);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr hook, int code, IntPtr message, IntPtr data);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern IntPtr GetModuleHandle(string name);
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder name, ref uint size);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")] static extern bool GetProcessTimes(IntPtr process, out long created, out long exited, out long kernel, out long user);
    static bool dispatchStarted;

    internal static Dictionary<string, object> D(params object[] values) { var d = new Dictionary<string, object>(); for (int i=0; i<values.Length; i+=2) d[(string)values[i]]=values[i+1]; return d; }
    internal static string S(IDictionary<string, object> d, string key, string fallback="") { object value; return d.TryGetValue(key,out value) && value is string ? (string)value : fallback; }
    internal static bool B(IDictionary<string, object> d, string key) { object value; return d.TryGetValue(key,out value) && value is bool && (bool)value; }
    internal static Dictionary<string, object> O(IDictionary<string, object> d, string key) { object value; return d.TryGetValue(key,out value) ? value as Dictionary<string, object> : null; }
    internal static long L(IDictionary<string, object> d, string key) { object v; if (!d.TryGetValue(key,out v) || !(v is int || v is long)) throw new InvalidOperationException("invalid_integer"); return Convert.ToInt64(v); }
    static double Number(IDictionary<string, object> d, string key) { object value; if (!d.TryGetValue(key,out value) || !(value is int || value is long || value is double || value is decimal)) throw new InvalidOperationException("invalid_number"); double n=Convert.ToDouble(value); if (Double.IsNaN(n)||Double.IsInfinity(n)) throw new InvalidOperationException("invalid_number"); return n; }
    internal static string Text(IntPtr window) { var b=new StringBuilder(1024); GetWindowText(window,b,b.Capacity); return b.ToString(); }
    internal static string Class(IntPtr window) { var b=new StringBuilder(512); GetClassName(window,b,b.Capacity); return b.ToString(); }
    internal static uint Pid(IntPtr window) { uint pid; GetWindowThreadProcessId(window,out pid); return pid; }
    internal static uint Dpi(IntPtr window) { try { return GetDpiForWindow(window); } catch (EntryPointNotFoundException) { return 96; } }
    internal static string SafePath(string value, bool file, bool exists) {
        if (String.IsNullOrWhiteSpace(value) || !Path.IsPathRooted(value) || value.StartsWith(@"\\") || value.StartsWith("//")) throw new InvalidOperationException("local_path_required");
        string full=Path.GetFullPath(value); var drive=new DriveInfo(Path.GetPathRoot(full)); if (drive.DriveType!=DriveType.Fixed) throw new InvalidOperationException("fixed_drive_required");
        string current=file ? Path.GetDirectoryName(full) : full;
        while (!String.IsNullOrEmpty(current)) { if (!Directory.Exists(current) || (File.GetAttributes(current)&FileAttributes.ReparsePoint)!=0) throw new InvalidOperationException("unsafe_directory"); current=Path.GetDirectoryName(current.TrimEnd(Path.DirectorySeparatorChar)); }
        if (file && File.Exists(full) && (File.GetAttributes(full)&FileAttributes.ReparsePoint)!=0) throw new InvalidOperationException("unsafe_file");
        if (exists && (file ? !File.Exists(full) : !Directory.Exists(full))) throw new InvalidOperationException("path_missing");
        return full;
    }
    internal static void Write(string path, object value) {
        path=SafePath(path,true,false); string temporary=path+"."+Guid.NewGuid().ToString("N")+".tmp";
        using (var fs=new FileStream(temporary,FileMode.CreateNew,FileAccess.Write,FileShare.None)) { byte[] bytes=Encoding.UTF8.GetBytes(Json.Serialize(value)); fs.Write(bytes,0,bytes.Length); fs.Flush(true); }
        if (File.Exists(path)) File.Replace(temporary,path,null); else File.Move(temporary,path);
    }
    internal sealed class Binding {
        public readonly int PidValue; public readonly IntPtr Hwnd; public readonly string Exe, Created;
        public Binding(Dictionary<string, object> r) {
            long pid=L(r,"pid"), hwnd=L(r,"hwnd"); if(pid<1||pid>Int32.MaxValue||hwnd<1) throw new InvalidOperationException("invalid_target"); PidValue=(int)pid; Hwnd=new IntPtr(hwnd); Exe=S(r,"exe_name");
            if (Exe.Length==0||Exe.Length>200||Path.GetFileName(Exe)!=Exe||Exe.IndexOfAny(new char[]{'*','?','/','\\',':','\0'})>=0||!Exe.EndsWith(".exe",StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException("invalid_exe_name");
            Created=Identity(); string expected=S(r,"process_started_utc_ticks"); if(expected.Length>0&&expected!=Created) throw new InvalidOperationException("target_lifetime_changed");
        }
        string Identity() {
            if(!IsWindow(Hwnd)||Pid(Hwnd)!=(uint)PidValue) throw new InvalidOperationException("target_window_mismatch");
            using(var process=Process.GetProcessById(PidValue)) { if(process.SessionId!=Process.GetCurrentProcess().SessionId) throw new InvalidOperationException("different_session"); }
            IntPtr h=OpenProcess(0x1000,false,(uint)PidValue); if(h==IntPtr.Zero) throw new InvalidOperationException("target_unavailable");
            try { uint size=32768; var name=new StringBuilder((int)size); long created,exit,kernel,user;
                if(!QueryFullProcessImageName(h,0,name,ref size)||!GetProcessTimes(h,out created,out exit,out kernel,out user)) throw new InvalidOperationException("target_unavailable");
                if(!String.Equals(Path.GetFileName(name.ToString()),Exe,StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException("target_executable_mismatch");
                return DateTime.FromFileTimeUtc(created).Ticks.ToString(System.Globalization.CultureInfo.InvariantCulture);
            } finally { CloseHandle(h); }
        }
        public void Check() { if(Identity()!=Created) throw new InvalidOperationException("target_lifetime_changed"); }
        public bool ContainsWindow(IntPtr window) { if(window==IntPtr.Zero||Pid(window)!=(uint)PidValue) return false; IntPtr root=GetAncestor(window,2); return root==Hwnd||IsChild(Hwnd,window)||Owned(root,Hwnd); }
        public Dictionary<string,object> Info() { return D("pid",PidValue,"hwnd",Hwnd.ToInt64(),"exe_name",Exe,"title",Text(Hwnd),"class_name",Class(Hwnd),"owner_hwnd",GetWindow(Hwnd,4).ToInt64(),"root_owner_hwnd",GetAncestor(Hwnd,3).ToInt64(),"process_started_utc_ticks",Created,"visible",IsWindowVisible(Hwnd),"minimized",IsIconic(Hwnd),"dpi",Dpi(Hwnd)); }
    }
    static bool Owned(IntPtr child,IntPtr owner) { var seen=new HashSet<IntPtr>(); for(int i=0;i<12&&child!=IntPtr.Zero&&seen.Add(child);i++) { child=GetWindow(child,4); if(child==owner)return true; } return false; }
    static bool SameElement(AutomationElement a,AutomationElement b) { try { return Automation.Compare(a,b); } catch { return false; } }
    static bool Within(AutomationElement node,AutomationElement parent) { for(int i=0;i<32&&node!=null;i++) { if(SameElement(node,parent))return true; try { node=TreeWalker.ControlViewWalker.GetParent(node); }catch{return false;} } return false; }
    internal static string Role(AutomationElement e) {
        var t=e.Current.ControlType;
        if(t==ControlType.Edit) return "textfield"; if(t==ControlType.ComboBox)return "combobox"; if(t==ControlType.Button)return "button";
        if(t==ControlType.CheckBox)return "checkbox"; if(t==ControlType.MenuItem)return "menuitem"; if(t==ControlType.Menu||t==ControlType.MenuBar)return "menu";
        if(t==ControlType.Window)return "window"; if(t==ControlType.Text)return "statictext"; if(t==ControlType.DataGrid||t==ControlType.Table)return "table";
        if(t==ControlType.DataItem)return "row"; if(t==ControlType.List)return "list"; if(t==ControlType.ListItem)return "listitem";
        if(t==ControlType.Tree)return "tree"; if(t==ControlType.TreeItem)return "treeitem"; if(t==ControlType.Tab)return "tabgroup"; if(t==ControlType.TabItem)return "tab";
        if(t==ControlType.ScrollBar)return "scrollbar"; if(t==ControlType.Pane||t==ControlType.Group)return "group"; return t.ProgrammaticName.Replace("ControlType.","").ToLowerInvariant();
    }
    static bool Finite(double value) { return !Double.IsNaN(value)&&!Double.IsInfinity(value); }
    static bool ValidBounds(System.Windows.Rect r) { return !r.IsEmpty&&Finite(r.X)&&Finite(r.Y)&&Finite(r.Width)&&Finite(r.Height)&&Finite(r.Right)&&Finite(r.Bottom)&&r.Width>0&&r.Height>0&&r.X>=Int32.MinValue&&r.Y>=Int32.MinValue&&r.Right<=Int32.MaxValue&&r.Bottom<=Int32.MaxValue; }
    static Dictionary<string,object> SafeBounds(System.Windows.Rect r) { return ValidBounds(r)?D("x",r.X,"y",r.Y,"width",r.Width,"height",r.Height):D("x",0,"y",0,"width",0,"height",0); }
    internal static Dictionary<string,object> Bounds(AutomationElement e) { return SafeBounds(e.Current.BoundingRectangle); }
    static bool StableId(string id) { if(String.IsNullOrEmpty(id))return false; foreach(char c in id)if(!Char.IsDigit(c))return true; return false; }
    static Dictionary<string,object> BasicLocator(AutomationElement e) { var d=D("role",Role(e)); string id=e.Current.AutomationId,name=e.Current.Name; if(StableId(id))d["automation_id"]=id; if(!String.IsNullOrEmpty(name))d["name"]=name; return d; }
    internal static Dictionary<string,object> Locator(AutomationElement e,AutomationElement root) {
        var d=BasicLocator(e); var ancestors=new List<object>(); AutomationElement n=e;
        for(int i=0;i<10&&ancestors.Count<4;i++) { try{n=TreeWalker.ControlViewWalker.GetParent(n);}catch{break;} if(n==null||SameElement(n,root))break; var a=BasicLocator(n); if(a.ContainsKey("automation_id")||a.ContainsKey("name"))ancestors.Add(a); }
        if(ancestors.Count>0)d["ancestors"]=ancestors; return d;
    }
    internal static List<string> Actions(AutomationElement e) {
        var a=new List<string>(); object p; if(e.Current.IsPassword)return a;
        if(e.TryGetCurrentPattern(InvokePattern.Pattern,out p))a.Add("Click");
        if(e.TryGetCurrentPattern(ValuePattern.Pattern,out p)&&!((ValuePattern)p).Current.IsReadOnly)a.Add("SetValue");
        if(e.TryGetCurrentPattern(TogglePattern.Pattern,out p)){a.Add("Check");a.Add("Uncheck");}
        if(e.TryGetCurrentPattern(ExpandCollapsePattern.Pattern,out p)){a.Add("Expand");a.Add("Collapse");}
        if(e.TryGetCurrentPattern(SelectionItemPattern.Pattern,out p))a.Add("Select");
        if(e.TryGetCurrentPattern(ScrollPattern.Pattern,out p))a.Add("Scroll");
        if(e.Current.IsKeyboardFocusable)a.Add("SetFocus"); return a;
    }
    internal static object Value(AutomationElement e) { object p; if(e.Current.IsPassword)return null; if(e.TryGetCurrentPattern(ValuePattern.Pattern,out p))return ((ValuePattern)p).Current.Value; return null; }
    internal static List<string> States(AutomationElement e) {
        var s=new List<string>(); object p; if(e.Current.IsPassword)s.Add("protected"); if(!e.Current.IsEnabled)s.Add("disabled"); if(e.Current.IsOffscreen)s.Add("offscreen"); if(e.Current.HasKeyboardFocus)s.Add("focused");
        if(e.TryGetCurrentPattern(TogglePattern.Pattern,out p)){var v=((TogglePattern)p).Current.ToggleState;s.Add(v==ToggleState.On?"checked":v==ToggleState.Off?"unchecked":"indeterminate");}
        if(e.TryGetCurrentPattern(ExpandCollapsePattern.Pattern,out p)){var v=((ExpandCollapsePattern)p).Current.ExpandCollapseState;if(v==ExpandCollapseState.Expanded)s.Add("expanded");else if(v==ExpandCollapseState.Collapsed)s.Add("collapsed");}
        if(e.TryGetCurrentPattern(SelectionItemPattern.Pattern,out p)&&((SelectionItemPattern)p).Current.IsSelected)s.Add("selected");return s;
    }
    internal static Dictionary<string,object> Node(AutomationElement e,AutomationElement root,string path,string parent) {
        object pattern,check=null;if(e.TryGetCurrentPattern(TogglePattern.Pattern,out pattern)){var state=((TogglePattern)pattern).Current.ToggleState;if(state!=ToggleState.Indeterminate)check=state==ToggleState.On;}
        var rect=e.Current.BoundingRectangle;bool boundsValid=ValidBounds(rect);var states=States(e);if(!boundsValid&&!states.Contains("offscreen"))states.Add("offscreen");
        return D("name",e.Current.Name,"role",Role(e),"automation_id",e.Current.AutomationId,"value",Value(e),"checked",check,"enabled",e.Current.IsEnabled,"offscreen",e.Current.IsOffscreen||!boundsValid,"secure",e.Current.IsPassword,"states",states,"available_actions",Actions(e),"locator",Locator(e,root),"bounds",SafeBounds(rect),"bounds_valid",boundsValid,"hwnd",(long)e.Current.NativeWindowHandle,"path",path,"parent_path",parent);
    }
    sealed class Tree { public List<Dictionary<string,object>> Nodes=new List<Dictionary<string,object>>(); public List<AutomationElement> Elements=new List<AutomationElement>(); public bool Complete=true; }
    static Tree ReadTree(Binding binding) {
        binding.Check(); var root=AutomationElement.FromHandle(binding.Hwnd); if(root==null||root.Current.ProcessId!=binding.PidValue)throw new InvalidOperationException("root_unavailable");
        var tree=new Tree(); var queue=new Queue<Tuple<AutomationElement,string,string,int>>(); queue.Enqueue(Tuple.Create(root,"0","",0));
        while(queue.Count>0) { var item=queue.Dequeue(); if(tree.Nodes.Count>=2500){tree.Complete=false;break;} var e=item.Item1;
            try { if(e.Current.ProcessId!=binding.PidValue){tree.Complete=false;continue;} tree.Nodes.Add(Node(e,root,item.Item2,item.Item3));tree.Elements.Add(e);
                var child=TreeWalker.ControlViewWalker.GetFirstChild(e); if(item.Item4>=18){if(child!=null)tree.Complete=false;continue;} int index=0;
                while(child!=null&&index<400){queue.Enqueue(Tuple.Create(child,item.Item2+"/"+index,item.Item2,item.Item4+1));child=TreeWalker.ControlViewWalker.GetNextSibling(child);index++;} if(child!=null)tree.Complete=false;
            } catch(ElementNotAvailableException){tree.Complete=false;} catch(InvalidOperationException){tree.Complete=false;}
        } binding.Check(); return tree;
    }
    static bool MatchBasic(Dictionary<string,object> actual,Dictionary<string,object> expected) { foreach(string key in new[]{"automation_id","name","role"}) { if(expected.ContainsKey(key)&&S(expected,key)!=S(actual,key))return false; } return expected.ContainsKey("automation_id")||expected.ContainsKey("name"); }
    static bool MatchLocator(Dictionary<string,object> actual,Dictionary<string,object> expected) {
        if(expected==null||!MatchBasic(actual,expected))return false; object list;
        if(expected.TryGetValue("ancestors",out list)) { var wanted=list as IEnumerable; object av; var available=actual.TryGetValue("ancestors",out av)?av as IEnumerable:null; var got=new List<Dictionary<string,object>>(); if(available!=null)foreach(object x in available){var d=x as Dictionary<string,object>;if(d!=null)got.Add(d);} int at=0;
            if(wanted==null)return false;foreach(object x in wanted){var d=x as Dictionary<string,object>;if(d==null)return false;while(at<got.Count&&!MatchBasic(got[at],d))at++;if(at==got.Count)return false;at++;}
        } return true;
    }
    static int Unique(Tree tree,Dictionary<string,object> locator) { int found=-1;for(int i=0;i<tree.Nodes.Count;i++)if(MatchLocator(O(tree.Nodes[i],"locator"),locator)){if(found>=0)throw new InvalidOperationException("ambiguous_target");found=i;}if(found<0)throw new InvalidOperationException("target_not_found");return found; }
    static object Inventory() {
        var rows=new List<object>(); EnumWindows(delegate(IntPtr window,IntPtr unused){try{if(!IsWindowVisible(window))return true;uint pid=Pid(window);if(pid==0||pid==Process.GetCurrentProcess().Id)return true;IntPtr h=OpenProcess(0x1000,false,pid);if(h==IntPtr.Zero)return true;string exe;try{uint n=32768;var b=new StringBuilder((int)n);if(!QueryFullProcessImageName(h,0,b,ref n))return true;exe=Path.GetFileName(b.ToString());}finally{CloseHandle(h);}var binding=new Binding(D("pid",(long)pid,"hwnd",window.ToInt64(),"exe_name",exe));rows.Add(binding.Info());}catch{}return rows.Count<250;},IntPtr.Zero);return D("ok",true,"status","observed","data",rows);
    }
    static object Inspect(Dictionary<string,object> request) { var b=new Binding(request);var tree=ReadTree(b);var data=b.Info();data["nodes"]=tree.Nodes;data["complete"]=tree.Complete;return D("ok",true,"status","observed","data",data); }
    static object Probe(Dictionary<string,object> request) { var b=new Binding(request);POINT p; if(B(request,"cursor")){if(!GetCursorPos(out p))throw new InvalidOperationException("cursor_unavailable");}else p=new POINT((int)L(request,"x"),(int)L(request,"y")); IntPtr hit=WindowFromPoint(p);if(!b.ContainsWindow(hit))throw new InvalidOperationException("point_outside_target");IntPtr rootWindow=GetAncestor(hit,2);var root=AutomationElement.FromHandle(rootWindow);var e=AutomationElement.FromPoint(new System.Windows.Point(p.X,p.Y));if(e==null||e.Current.ProcessId!=b.PidValue||!Within(e,root))throw new InvalidOperationException("point_identity_unproven");b.Check();return D("ok",true,"status","observed","data",Node(e,root,"0",""),"window",D("hwnd",rootWindow.ToInt64(),"title",Text(rootWindow),"class_name",Class(rootWindow))); }
    static object Activate(Dictionary<string,object> request) { var b=new Binding(request);if(S(request,"process_started_utc_ticks").Length==0)throw new InvalidOperationException("lifetime_required");b.Check();dispatchStarted=true;if(IsIconic(b.Hwnd))ShowWindow(b.Hwnd,9);bool returned=SetForegroundWindow(b.Hwnd);Thread.Sleep(100);b.Check();bool matched=GetAncestor(GetForegroundWindow(),2)==b.Hwnd;return D("ok",matched,"status",matched?"completed":"unavailable","input_dispatched",true,"delivery","delivered_unverified","foreground_verified",matched,"activation_returned",returned); }
    static object PointAction(Dictionary<string,object> request) {
        var b=new Binding(request);if(S(request,"process_started_utc_ticks").Length==0)throw new InvalidOperationException("lifetime_required");string action=S(request,"action");if(action!="click"&&action!="right-click"&&action!="double-click")throw new InvalidOperationException("invalid_action");
        var point=O(request,"point");if(point==null)throw new InvalidOperationException("recorded_point_required");double xr=Number(point,"x_ratio"),yr=Number(point,"y_ratio");if(xr<0.02||xr>0.98||yr<0.02||yr>0.98)throw new InvalidOperationException("point_near_boundary");
        var tree=ReadTree(b);if(!tree.Complete)throw new InvalidOperationException("incomplete_tree");int index=Unique(tree,O(request,"locator"));var node=tree.Nodes[index];var element=tree.Elements[index];if(S(node,"role")=="window")throw new InvalidOperationException("window_root_point_rejected");
        if(element.Current.IsPassword||!element.Current.IsEnabled||element.Current.IsOffscreen)throw new InvalidOperationException("target_not_actionable");var rect=element.Current.BoundingRectangle;if(!ValidBounds(rect)||rect.Width<4||rect.Height<4)throw new InvalidOperationException("target_bounds_unavailable");
        POINT p=new POINT((int)Math.Round(rect.X+xr*rect.Width),(int)Math.Round(rect.Y+yr*rect.Height));if(!IsWindowVisible(b.Hwnd)||IsIconic(b.Hwnd))throw new InvalidOperationException("window_not_visible");
        IntPtr fg=GetAncestor(GetForegroundWindow(),2);if(fg!=b.Hwnd&&!Owned(b.Hwnd,fg))throw new InvalidOperationException("target_not_foreground");
        IntPtr hit=WindowFromPoint(p);if(GetAncestor(hit,2)!=b.Hwnd&&!IsChild(b.Hwnd,hit))throw new InvalidOperationException("point_occluded");if(Pid(hit)!=(uint)b.PidValue)throw new InvalidOperationException("point_process_mismatch");
        var live=AutomationElement.FromPoint(new System.Windows.Point(p.X,p.Y));if(live==null||live.Current.ProcessId!=b.PidValue||!Within(live,element))throw new InvalidOperationException("point_element_mismatch");if(live.Current.IsPassword)throw new InvalidOperationException("protected_point_target");
        foreach(int key in new[]{0x10,0x11,0x12,0x5B,0x5C,1,2})if((GetAsyncKeyState(key)&0x8000)!=0)throw new InvalidOperationException("physical_input_busy");
        b.Check();var now=element.Current.BoundingRectangle;if(now!=rect||WindowFromPoint(p)!=hit)throw new InvalidOperationException("target_moved");
        int sx=GetSystemMetrics(76),sy=GetSystemMetrics(77),sw=GetSystemMetrics(78),sh=GetSystemMetrics(79);if(sw<2||sh<2)throw new InvalidOperationException("desktop_bounds_unavailable");
        var inputs=new List<INPUT>();inputs.Add(Mouse((int)Math.Round((p.X-sx)*65535.0/(sw-1)),(int)Math.Round((p.Y-sy)*65535.0/(sh-1)),0x8000|0x4000|0x0001));uint down=action=="right-click"?0x0008u:0x0002u,up=action=="right-click"?0x0010u:0x0004u;inputs.Add(Mouse(0,0,down));inputs.Add(Mouse(0,0,up));if(action=="double-click"){inputs.Add(Mouse(0,0,down));inputs.Add(Mouse(0,0,up));}
        dispatchStarted=true;uint sent=SendInput((uint)inputs.Count,inputs.ToArray(),Marshal.SizeOf(typeof(INPUT)));if(sent!=(uint)inputs.Count)return D("ok",false,"status","uncertain","input_dispatched",null,"delivery","possibly_dispatched","diagnostic","partial_input","retry","unsafe");
        return D("ok",true,"status","executed_unverified","input_dispatched",true,"delivery","delivered_unverified","retry","unsafe","business_commit","not_verified");
    }
    static INPUT Mouse(int x,int y,uint flags) { return new INPUT{type=0,value=new INPUTUNION{mouse=new MOUSEINPUT{dx=x,dy=y,flags=flags}}}; }

    sealed class Capture { public POINT Point;public string Action;public DateTime At=DateTime.UtcNow;public Observation Before;public IntPtr Window,HitWindow; }
    sealed class Observation { public AutomationElement Element,Root;public Dictionary<string,object> Node;public IntPtr Window; }
    sealed class Recorder : Form {
        readonly Binding binding;readonly Dictionary<string,object> request;readonly string progress,draft,eventsPath,stopPath;readonly List<Dictionary<string,object>> steps=new List<Dictionary<string,object>>();
        readonly BlockingCollection<Action> queue=new BlockingCollection<Action>(128);readonly System.Windows.Forms.Timer timer=new System.Windows.Forms.Timer();readonly Label stateLabel=new Label(),detail=new Label();readonly ListView list=new ListView();readonly Button start=new Button(),pause=new Button(),finish=new Button();
        HookProc mouseCallback,keyCallback;IntPtr mouseHook,keyHook;bool recording,done,workerBusy,partial,finishing,focusPending,hoverPending;DateTime workerAt,lastWatch,lastKey,finishAt,hoverAt;Capture pending;Observation watched,hovered;POINT hoverPoint;IntPtr hoverWindow;string watchedValue;Dictionary<string,object> watchedStep;int warningCount;string state="ready";public object Result;
        public Recorder(Dictionary<string,object> r) {
            request=r;binding=new Binding(r);string output=SafePath(S(r,"output_dir"),false,true);progress=SafePath(S(r,"progress_path"),true,false);draft=SafePath(S(r,"draft_path"),true,false);stopPath=S(r,"stop_path");if(stopPath.Length>0)stopPath=SafePath(stopPath,true,false);eventsPath=Path.Combine(output,"recording-"+Guid.NewGuid().ToString("N")+".events.jsonl");
            Text="Agent Desktop Workflows · 작업 녹화";StartPosition=FormStartPosition.Manual;Location=new Point(Math.Max(0,Screen.PrimaryScreen.WorkingArea.Right-690),Math.Max(0,Screen.PrimaryScreen.WorkingArea.Bottom-430));Size=new Size(680,410);MinimumSize=new Size(620,350);TopMost=true;Font=new Font("Malgun Gothic",10);BackColor=Color.FromArgb(242,246,249);
            stateLabel.Dock=DockStyle.Top;stateLabel.Height=44;stateLabel.Padding=new Padding(14,10,10,4);stateLabel.Font=new Font(Font,FontStyle.Bold);stateLabel.Text="대상: "+TextOfBinding();Controls.Add(stateLabel);
            detail.Dock=DockStyle.Top;detail.Height=66;detail.Padding=new Padding(14,4,14,6);detail.Text="시작 후 평소처럼 작업하세요. 인식되지 않은 동작은 보완 단계로 남깁니다.\n녹화 중 실제 프로그램의 값과 상태가 변경됩니다.";Controls.Add(detail);detail.BringToFront();stateLabel.BringToFront();
            var buttons=new FlowLayoutPanel{Dock=DockStyle.Bottom,Height=52,Padding=new Padding(10,6,10,5)};start.Text="녹화 시작";pause.Text="일시정지";finish.Text="마치고 검토";var cancel=new Button{Text="취소"};foreach(var button in new[]{start,pause,finish,cancel}){button.Width=130;button.Height=32;buttons.Controls.Add(button);}Controls.Add(buttons);
            list.Dock=DockStyle.Fill;list.View=View.Details;list.FullRowSelect=true;list.GridLines=false;list.Columns.Add("단계",45);list.Columns.Add("동작 / 대상",230);list.Columns.Add("인식과 재생 상태",350);Controls.Add(list);list.BringToFront();buttons.BringToFront();detail.BringToFront();stateLabel.BringToFront();
            start.Click+=delegate{StartRecording();};pause.Click+=delegate{PauseRecording();};finish.Click+=delegate{Finish(false);};cancel.Click+=delegate{Finish(true);};FormClosing+=delegate(object sender,FormClosingEventArgs e){if(!done){e.Cancel=true;Finish(false);}};
            timer.Interval=250;timer.Tick+=delegate{Tick();};timer.Start();var worker=new Thread(Work);worker.IsBackground=true;worker.SetApartmentState(ApartmentState.MTA);worker.Start();
            Shown+=delegate{Progress();if(B(request,"auto_start"))StartRecording();};FormClosed+=delegate{Unhook();timer.Stop();queue.CompleteAdding();};
        }
        string TextOfBinding(){return Text(binding.Hwnd).Length>0?Text(binding.Hwnd):binding.Exe;}
        void UI(Action a){if(done||IsDisposed)return;try{BeginInvoke(a);}catch(InvalidOperationException){}}
        void Work(){foreach(Action action in queue.GetConsumingEnumerable()){if(done)break;workerBusy=true;workerAt=DateTime.UtcNow;try{action();}catch{partial=true;UI(delegate{AddManual("요소를 읽지 못한 동작","accessibility_unavailable");});}finally{workerBusy=false;}}}
        void Enqueue(Action a){if(done)return;if(!queue.TryAdd(a)){partial=true;UI(delegate{PauseRecording();AddManual("기록이 밀린 동작","recording_queue_full");});}}
        void StartRecording(){if(done||recording)return;try{binding.Check();mouseCallback=MouseHook;keyCallback=KeyHook;mouseHook=SetWindowsHookEx(14,mouseCallback,GetModuleHandle(null),0);keyHook=SetWindowsHookEx(13,keyCallback,GetModuleHandle(null),0);if(mouseHook==IntPtr.Zero||keyHook==IntPtr.Zero)throw new InvalidOperationException("hook_unavailable");recording=true;state="recording";start.Enabled=false;pause.Enabled=true;if(IsIconic(binding.Hwnd))ShowWindow(binding.Hwnd,9);SetForegroundWindow(binding.Hwnd);detail.Text="● 기록 중 · 대상 창에서 직접 작업하세요.\n각 단계의 대상 인식과 조작 가능 여부를 아래에서 확인합니다.";Progress();}catch{Unhook();state="failed";detail.Text="녹화를 시작하지 못했습니다. 대상 창과 현재 사용자 세션을 확인하세요.";Progress();}}
        void Unhook(){if(mouseHook!=IntPtr.Zero){UnhookWindowsHookEx(mouseHook);mouseHook=IntPtr.Zero;}if(keyHook!=IntPtr.Zero){UnhookWindowsHookEx(keyHook);keyHook=IntPtr.Zero;}recording=false;}
        void PauseRecording(){if(done)return;Unhook();state="paused";start.Enabled=true;pause.Enabled=false;Enqueue(delegate{FinalizeWatch(true);});detail.Text="일시정지 · 지금까지 기록은 보존됩니다.\n재시작 후 입력 대상을 클릭하세요.";Progress();}
        IntPtr MouseHook(int code,IntPtr message,IntPtr data){if(code>=0&&recording){try{int m=message.ToInt32();var mouse=(MOUSEHOOK)Marshal.PtrToStructure(data,typeof(MOUSEHOOK));IntPtr hit=WindowFromPoint(mouse.pt);if(Pid(hit)==Process.GetCurrentProcess().Id)return CallNextHookEx(mouseHook,code,message,data);if(!binding.ContainsWindow(hit)){state="outside_target";return CallNextHookEx(mouseHook,code,message,data);}state="recording";
                    if(m==0x200){hoverPoint=mouse.pt;hoverWindow=GetAncestor(hit,2);}
                    else if(m==0x201||m==0x204){var c=new Capture{Point=mouse.pt,Action=m==0x204?"right-click":"click",Window=GetAncestor(hit,2),HitWindow=hit};var cached=hovered;if(cached!=null&&cached.Window==c.Window&&DateTime.UtcNow.Subtract(hoverAt).TotalMilliseconds<1200){var bounds=O(cached.Node,"bounds");if(bounds!=null&&c.Point.X>=Number(bounds,"x")&&c.Point.Y>=Number(bounds,"y")&&c.Point.X<Number(bounds,"x")+Number(bounds,"width")&&c.Point.Y<Number(bounds,"y")+Number(bounds,"height"))c.Before=cached;}pending=c;if(c.Before==null)Enqueue(delegate{c.Before=ObserveDown(c);});}
                    else if(m==0x202||m==0x205){var c=pending;pending=null;if(c!=null)Enqueue(delegate{Thread.Sleep(100);CaptureAction(c);});}
                    else if(m==0x20A||m==0x20E)Enqueue(delegate{FinalizeWatch(true);UI(delegate{AddManual("스크롤 동작","scroll_needs_review");});});
                }catch{partial=true;}}return CallNextHookEx(mouseHook,code,message,data);}
        IntPtr KeyHook(int code,IntPtr message,IntPtr data){if(code>=0&&recording&&(message.ToInt32()==0x100||message.ToInt32()==0x104)){try{if(!binding.ContainsWindow(GetForegroundWindow()))return CallNextHookEx(keyHook,code,message,data);var key=(KEYHOOK)Marshal.PtrToStructure(data,typeof(KEYHOOK));lastKey=DateTime.UtcNow;
                    if(key.code==27){UI(delegate{AddManual("Escape 동작과 일시정지","keyboard_transition_needs_review");PauseRecording();});}
                    else if(key.code==13||key.code==9){Enqueue(delegate{FinalizeWatch(true);UI(delegate{AddManual(key.code==13?"Enter 확정 동작":"Tab 이동 동작","keyboard_transition_needs_review");});});}
                    else if((GetAsyncKeyState(0x11)&0x8000)!=0&&key.code!=0x11&&key.code!=65&&key.code!=67&&key.code!=86&&key.code!=88){Enqueue(delegate{UI(delegate{AddManual("단축키 동작","shortcut_needs_review");});});}
                    else if(watched==null&&!focusPending){focusPending=true;Enqueue(delegate{try{var e=AutomationElement.FocusedElement;if(e!=null&&e.Current.ProcessId==binding.PidValue){var root=AutomationElement.FromHandle(binding.Hwnd);if(Within(e,root))Watch(new Observation{Element=e,Root=root,Window=binding.Hwnd,Node=Node(e,root,"0","")});}}catch{}finally{focusPending=false;}});}
                }catch{partial=true;}}return CallNextHookEx(keyHook,code,message,data);}
        Observation Observe(POINT point,IntPtr window){binding.Check();if(!binding.ContainsWindow(window))throw new InvalidOperationException("outside_target");var root=AutomationElement.FromHandle(window);var e=AutomationElement.FromPoint(new System.Windows.Point(point.X,point.Y));if(e==null||e.Current.ProcessId!=binding.PidValue||!Within(e,root))throw new InvalidOperationException("point_identity_unproven");return new Observation{Element=e,Root=root,Window=window,Node=Node(e,root,"0","")};}
        Observation ObserveDown(Capture capture){
            // The native child HWND is witnessed synchronously by the mouse hook.
            // A context menu may cover the point before the asynchronous UIA read.
            // Never attribute that new popup to the preceding click. A real named
            // child control can still provide the original region anchor.
            try{if(WindowFromPoint(capture.Point)==capture.HitWindow)return Observe(capture.Point,capture.Window);}catch{}
            if(capture.HitWindow==capture.Window||!IsWindow(capture.HitWindow)||!IsChild(capture.Window,capture.HitWindow)||Pid(capture.HitWindow)!=(uint)binding.PidValue)return null;
            binding.Check();var root=AutomationElement.FromHandle(capture.Window);var e=AutomationElement.FromHandle(capture.HitWindow);if(e==null||e.Current.ProcessId!=binding.PidValue||!Within(e,root))return null;
            var node=Node(e,root,"0","");var bounds=O(node,"bounds");if(bounds==null||capture.Point.X<Number(bounds,"x")||capture.Point.Y<Number(bounds,"y")||capture.Point.X>=Number(bounds,"x")+Number(bounds,"width")||capture.Point.Y>=Number(bounds,"y")+Number(bounds,"height"))return null;
            return new Observation{Element=e,Root=root,Window=capture.Window,Node=node};
        }
        void Watch(Observation observation){if(observation==null)return;if(observation.Element.Current.IsPassword){FinalizeWatch(true);UI(delegate{AddManual("보호된 입력","protected_input_not_recorded");});return;}var value=Value(observation.Element) as string;if(value==null)return;if(watched!=null&&SameElement(watched.Element,observation.Element))return;FinalizeWatch(true);watched=observation;watchedValue=value;watchedStep=null;}
        Dictionary<string,object> Target(Observation observation,string kind,POINT? point){var t=D("kind",kind,"locator",O(observation.Node,"locator"));if(observation.Window!=binding.Hwnd)t["window"]=D("title",Text(observation.Window),"class_name",Class(observation.Window));if(point.HasValue){var bounds=O(observation.Node,"bounds");double w=Number(bounds,"width"),h=Number(bounds,"height");if(w<4||h<4)throw new InvalidOperationException("bounds_unavailable");t["point"]=D("x_ratio",(point.Value.X-Number(bounds,"x"))/w,"y_ratio",(point.Value.Y-Number(bounds,"y"))/h);t["recorded_bounds"]=bounds;}return t;}
        bool Identified(Observation o){var l=O(o.Node,"locator");return l!=null&&(l.ContainsKey("automation_id")||l.ContainsKey("name"));}
        Dictionary<string,object> Step(string action,Observation o,string kind,POINT? point){string name=S(o.Node,"name");if(name.Length==0)name=S(o.Node,"automation_id");if(name.Length==0)name=S(o.Node,"role");bool menu=S(o.Node,"role")=="menuitem";return D("id",Guid.NewGuid().ToString("N"),"label",name+" · "+action,"action",action,"target",Target(o,kind,point),"headed",kind=="region","expect",D("kind",menu?"none":"manual"),"recognition",D("target",kind=="element"?"identified":"region_identified","action",kind=="element"?"supported":"region_review_required","result",menu?"delivery_only":"manual","reason",kind=="element"?(menu?"business_completion_not_verified":"completion_condition_required"):"recorded_region_requires_review"));}
        void CaptureAction(Capture c){var o=c.Before;if(o==null){UI(delegate{AddManual("대상을 찾지 못한 클릭","capture_target_unavailable");});return;}try{if(o.Element.Current.IsPassword){UI(delegate{AddManual("보호된 입력","protected_input_not_recorded");});return;}}catch{UI(delegate{AddManual("사라진 요소 클릭","element_disappeared");});return;}
            if(!Identified(o)){FinalizeWatch(true);UI(delegate{AddManual("이름 없는 요소 클릭","stable_locator_unavailable");});return;}
            var actions=o.Node["available_actions"] as List<string>;string role=S(o.Node,"role");
            if(c.Action=="click"&&actions.Contains("SetValue")){Watch(o);return;}
            if(c.Action=="click"&&role=="listitem"&&watched!=null){Thread.Sleep(180);FinalizeWatch(true);return;}
            FinalizeWatch(true);Dictionary<string,object> step;
            if(c.Action=="click"&&actions.Contains("Check")){var states=States(o.Element);if(!states.Contains("checked")&&!states.Contains("unchecked")){UI(delegate{AddManual("체크 상태 확인 필요","indeterminate_check_state");});return;}string action=states.Contains("checked")?"check":"uncheck";step=Step(action,o,"element",null);step["expect"]=D("kind","checked","value",action=="check");step["recognition"]=D("target","identified","action","supported","result","checkable");}
            else if(c.Action=="click"&&actions.Contains("Click"))step=Step("click",o,"element",null);
            else if(c.Action=="click"&&actions.Contains("Select")){step=Step("select",o,"element",null);step["value"]=S(o.Node,"name");}
            else if(role!="window"){var bounds=O(o.Node,"bounds");double x=(c.Point.X-Number(bounds,"x"))/Number(bounds,"width"),y=(c.Point.Y-Number(bounds,"y"))/Number(bounds,"height");if(x<.02||x>.98||y<.02||y>.98){UI(delegate{AddManual("영역 경계 클릭","region_edge_requires_retarget");});return;}step=Step(c.Action,o,"region",c.Point);}
            else {UI(delegate{AddManual("창 배경 클릭","window_root_not_replay_target");});return;}
            UI(delegate{AddStep(step);});
        }
        void FinalizeWatch(bool force){var o=watched;if(o==null)return;string current;try{current=Value(o.Element) as string;}catch{if(force){watched=null;UI(delegate{AddManual("입력 결과를 읽지 못함","value_readback_unavailable");});}return;}if(current==null){if(force)watched=null;return;}if(current!=watchedValue||force){if(!Identified(o)){UI(delegate{AddManual("입력 대상 식별 필요","stable_locator_unavailable");});watched=null;return;}var step=Step("set-value",o,"element",null);step["value"]=current;step["expect"]=D("kind","value","value",current);step["recognition"]=D("target","identified","action","supported","result","checkable");if(watchedStep!=null)step["id"]=watchedStep["id"];watchedStep=step;watchedValue=current;UI(delegate{AddStep(step);});}if(force){watched=null;watchedStep=null;}}
        void AddManual(string label,string reason){var step=D("id",Guid.NewGuid().ToString("N"),"label",label,"action","manual","target",D("kind","manual","locator",D()),"expect",D("kind","manual"),"recognition",D("target","unrecognized","action","manual","result","manual","reason",reason));AddStep(step);}
        void AddStep(Dictionary<string,object> step){if(done)return;if(steps.Count>=300){partial=true;PauseRecording();detail.Text="300단계에 도달했습니다. 기록을 마치고 검토하세요.";return;}int index=steps.FindIndex(delegate(Dictionary<string,object> v){return S(v,"id")==S(step,"id");});bool replace=index>=0;if(!replace){index=steps.Count;steps.Add(step);}else steps[index]=step;
            var recognition=O(step,"recognition");string reason=S(recognition,"reason");bool warning=S(recognition,"action")!="supported"||S(recognition,"result")=="manual";string feedback=(S(recognition,"target")=="identified"?"요소 확인":S(recognition,"target")=="region_identified"?"영역 확인":"대상 보완 필요")+" / "+(S(recognition,"action")=="supported"?"조작 지원":"재생 검토 필요")+" / "+(S(recognition,"result")=="checkable"?"값 확인 가능":"완료 조건 필요");
            var row=new ListViewItem(new[]{(index+1).ToString(),S(step,"label"),feedback});row.ForeColor=warning?Color.FromArgb(154,87,0):Color.FromArgb(0,112,90);if(replace)list.Items[index]=row;else list.Items.Add(row);list.EnsureVisible(index);warningCount=0;foreach(var s in steps){var r=O(s,"recognition");if(S(r,"action")!="supported"||S(r,"result")=="manual")warningCount++;}
            detail.Text=(index+1)+"단계: "+S(step,"label")+"\n"+feedback+(reason.Length>0?" ("+reason+")":"");stateLabel.Text="● "+(recording?"녹화 중":"일시정지")+" · "+steps.Count+"단계 · 보완 "+warningCount+"개";
            try{using(var f=new FileStream(SafePath(eventsPath,true,false),FileMode.Append,FileAccess.Write,FileShare.Read)){byte[] bytes=Encoding.UTF8.GetBytes(Json.Serialize(D("at_utc",DateTime.UtcNow.ToString("o"),"kind",replace?"step_revision":"step_recorded","step",step))+"\n");f.Write(bytes,0,bytes.Length);f.Flush(true);}}catch{partial=true;Unhook();state="failed";detail.Text="기록 파일을 저장하지 못해 녹화를 중지했습니다.";}Progress();}
        void Progress(){if(done)return;var value=D("state",state,"event_count",steps.Count,"warning_count",warningCount,"partial",partial);if(steps.Count>0)value["last_step"]=steps[steps.Count-1];if(recording&&hovered!=null)value["hover_target"]=O(hovered.Node,"locator");if(workerBusy&&DateTime.UtcNow.Subtract(workerAt).TotalSeconds>2)value["diagnostic"]="accessibility_read_delayed";try{Write(progress,value);}catch{Unhook();state="failed";detail.Text="진행 상태를 저장하지 못했습니다. 기록을 마치고 검토하세요.";}}
        void Tick(){if(done)return;if(finishing){if(DateTime.UtcNow.Subtract(finishAt).TotalSeconds>2){partial=true;AddManual("분석이 끝나지 않은 마지막 동작","pending_capture_requires_review");CompleteFinish(false);}return;}try{binding.Check();}catch{partial=true;PauseRecording();state="failed";detail.Text="대상 창이 닫혔거나 변경됐습니다. 기록을 마치고 검토하세요.";}if(stopPath.Length>0&&File.Exists(stopPath)){Finish(false);return;}if(recording){POINT actual;if(GetCursorPos(out actual)){IntPtr under=WindowFromPoint(actual);if(binding.ContainsWindow(under)){hoverPoint=actual;hoverWindow=GetAncestor(under,2);}else{hovered=null;hoverWindow=IntPtr.Zero;}}}if(recording&&!hoverPending&&hoverWindow!=IntPtr.Zero&&DateTime.UtcNow.Subtract(hoverAt).TotalMilliseconds>200){hoverPending=true;POINT p=hoverPoint;IntPtr w=hoverWindow;Enqueue(delegate{try{if(binding.ContainsWindow(WindowFromPoint(p))){hovered=Observe(p,w);hoverAt=DateTime.UtcNow;}}catch{}finally{hoverPending=false;}});}if(recording&&watched!=null&&DateTime.UtcNow.Subtract(lastWatch).TotalMilliseconds>600&&DateTime.UtcNow.Subtract(lastKey).TotalMilliseconds>500){lastWatch=DateTime.UtcNow;Enqueue(delegate{FinalizeWatch(false);});}if(workerBusy&&DateTime.UtcNow.Subtract(workerAt).TotalSeconds>2)detail.Text="현재 요소 응답을 기다리고 있습니다. 늦은 단계는 보완 대상으로 남습니다.";Progress();}
        void Finish(bool cancel){if(done||finishing)return;Unhook();start.Enabled=pause.Enabled=finish.Enabled=false;finishing=true;finishAt=DateTime.UtcNow;detail.Text="기록을 정리하고 있습니다. 응답이 없는 단계는 보완 대상으로 보존합니다.";if(cancel){CompleteFinish(true);return;}Enqueue(delegate{FinalizeWatch(true);UI(delegate{CompleteFinish(false);});});}
        void CompleteFinish(bool cancel){if(done)return;done=true;timer.Stop();state=cancel?"cancelled":"review";
            if(!cancel){var workflow=D("schema_version",1,"id",Guid.NewGuid().ToString("N"),"name",S(request,"name","새 녹화 작업"),"revision",1,"target",D("exe_name",binding.Exe,"title",Text(binding.Hwnd),"class_name",Class(binding.Hwnd)),"parameters",D(),"steps",steps);Write(draft,workflow);Result=D("ok",true,"status","recorded","draft_path",draft,"recording_path",eventsPath,"event_count",steps.Count,"warning_count",warningCount,"partial",partial);}
            else Result=D("ok",true,"status","cancelled","event_count",steps.Count);
            try{Write(progress,D("state",state,"event_count",steps.Count,"warning_count",warningCount,"partial",partial));}catch{}Close();
        }
    }
    [STAThread] static int Main(string[] args) {
        string response=null;try {
            try{SetProcessDpiAwarenessContext(new IntPtr(-4));}catch{SetProcessDPIAware();}
            if(args.Length!=5||args[1]!="--request"||args[3]!="--response")return 2;string requestPath=SafePath(args[2],true,true);response=SafePath(args[4],true,false);if(new FileInfo(requestPath).Length>2*1024*1024)throw new InvalidOperationException("request_too_large");var request=Json.DeserializeObject(File.ReadAllText(requestPath,Encoding.UTF8)) as Dictionary<string,object>;if(request==null)throw new InvalidOperationException("invalid_request");object result;
            switch(args[0]){case "windows":result=Inventory();break;case "inspect":result=Inspect(request);break;case "probe":result=Probe(request);break;case "activate":result=Activate(request);break;case "point":result=PointAction(request);break;case "record":Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);using(var recorder=new Recorder(request)){Application.Run(recorder);result=recorder.Result??D("ok",false,"status","cancelled");}break;default:throw new InvalidOperationException("invalid_command");}
            Write(response,result);return 0;
        }catch(Exception e){if(response!=null){try{string diagnostic=e is InvalidOperationException?e.Message:"native_operation_failed";if(diagnostic.Length>80||diagnostic.IndexOf(' ')>=0||diagnostic.IndexOf('\\')>=0||diagnostic.IndexOf(':')>=0)diagnostic="native_operation_failed";Write(response,D("ok",false,"status",dispatchStarted?"uncertain":"blocked","diagnostic",diagnostic,"input_dispatched",dispatchStarted?(object)null:false,"delivery",dispatchStarted?"possibly_dispatched":"not_delivered","retry",dispatchStarted?"unsafe":"inspect_again"));}catch{}}return 2;}
    }
}
