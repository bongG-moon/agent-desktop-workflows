// Synthetic fixture for the independent workflow recorder. No business services.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

sealed class NativeFixture : Form {
    readonly JavaScriptSerializer json=new JavaScriptSerializer();
    readonly Panel region=new Panel();readonly TextBox notes=new TextBox();readonly TextBox secret=new TextBox();readonly ComboBox combo=new ComboBox();readonly CheckBox check=new CheckBox();readonly Button query=new Button();readonly ContextMenuStrip menu=new ContextMenuStrip();readonly ToolStripMenuItem export=new ToolStripMenuItem("Export");readonly ToolStripMenuItem excel=new ToolStripMenuItem("Export to Excel");
    int queries,exports,contexts;Form sibling;
    public NativeFixture(){Text="Workflow Native Synthetic Fixture";Name="WorkflowNativeFixture";AccessibleName=Text;StartPosition=FormStartPosition.Manual;Location=new Point(30,30);ClientSize=new Size(620,460);AutoScaleMode=AutoScaleMode.None;
        var bar=new MenuStrip();var operations=new ToolStripMenuItem("Operations");var hidden=new ToolStripMenuItem("Closed submenu item");operations.DropDownItems.Add(hidden);bar.Items.Add(operations);Controls.Add(bar);MainMenuStrip=bar;
        notes.Name="NotesEditor";notes.AccessibleName="Notes";notes.Text="initial";notes.SetBounds(30,35,240,30);Controls.Add(notes);
        combo.Name="SetupSelector";combo.AccessibleName="Setup";combo.DropDownStyle=ComboBoxStyle.DropDownList;combo.Items.AddRange(new object[]{"Y","N"});combo.SelectedIndex=0;combo.SetBounds(300,35,100,30);Controls.Add(combo);
        check.Name="EnableOption";check.AccessibleName="Enable option";check.Text="Enable option";check.SetBounds(30,80,170,30);Controls.Add(check);
        query.Name="QueryButton";query.AccessibleName="Run query";query.Text="Run query";query.SetBounds(220,80,120,30);query.Click+=delegate{queries++;};Controls.Add(query);
        secret.Name="ProtectedEditor";secret.AccessibleName="Protected input";secret.UseSystemPasswordChar=true;secret.Text="do-not-record";secret.SetBounds(370,80,180,30);Controls.Add(secret);
        region.Name="ResultRegion";region.AccessibleName="Result region";region.AccessibleRole=AccessibleRole.Grouping;region.BorderStyle=BorderStyle.FixedSingle;region.BackColor=Color.White;region.SetBounds(30,140,540,260);region.Paint+=delegate(object sender,PaintEventArgs e){e.Graphics.DrawString("Synthetic rows - children intentionally absent",Font,Brushes.Navy,new PointF(20,30));};Controls.Add(region);
        export.Name="ExportMenu";excel.Name="ExportExcelMenu";excel.Click+=delegate{exports++;};export.DropDownItems.Add(excel);menu.Items.Add(export);menu.Opening+=delegate{contexts++;};region.ContextMenuStrip=menu;
        Shown+=delegate{new Thread(Read){IsBackground=true}.Start();Write("ready");};}
    Dictionary<string,object> State(){return new Dictionary<string,object>{{"pid",Process.GetCurrentProcess().Id},{"hwnd",Handle.ToInt64()},{"notes",notes.Text},{"combo",combo.Text},{"checked",check.Checked},{"query_count",queries},{"export_count",exports},{"context_count",contexts},{"menu_open",menu.Visible},{"sibling_hwnd",sibling==null?0:sibling.Handle.ToInt64()}};}
    void Write(string kind){var s=State();s["kind"]=kind;Console.WriteLine(json.Serialize(s));Console.Out.Flush();}
    void Read(){string line;while((line=Console.ReadLine())!=null){string command=line;try{Invoke(new Action(delegate{switch(command){case "quit":Close();return;case "activate":Activate();break;case "close-menu":menu.Close();break;case "reset":notes.Text="initial";combo.SelectedIndex=0;check.Checked=false;queries=exports=contexts=0;menu.Close();break;case "move":Location=new Point(80,70);break;case "resize":region.Width=500;region.Height=240;break;case "set-notes":notes.Text="recorded final";break;case "set-combo":combo.SelectedItem="N";break;case "sibling":if(sibling==null){sibling=new Form{Text="Unrelated same-process window",Name="Sibling",StartPosition=FormStartPosition.Manual,Location=new Point(30,30),Size=new Size(620,460)};sibling.Controls.Add(new Label{Text="Do not touch",Dock=DockStyle.Fill});sibling.Show();}sibling.Activate();break;}Write(command);}));}catch{return;}}}
    [STAThread]static void Main(){Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);Application.Run(new NativeFixture());}
}
