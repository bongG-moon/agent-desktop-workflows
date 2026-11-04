// Synthetic Windows UI only. stdin reads the fixture model independently of UIA.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

sealed class ActionFixture : Form {
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly ComboBox setup = new ComboBox();
    readonly TextBox notes = new TextBox();
    readonly CheckBox enabled = new CheckBox();
    readonly Button query = new Button();
    readonly Button save = new Button();
    readonly Button disabled = new Button();
    readonly Label status = new Label();
    readonly DataGridView grid = new DataGridView();
    readonly MenuStrip menu = new MenuStrip();
    readonly ToolStripMenuItem operations = new ToolStripMenuItem("Operations");
    readonly ToolStripMenuItem export = new ToolStripMenuItem("Export");
    readonly ToolStripMenuItem active = new ToolStripMenuItem("Active sheet");
    readonly ToolStripMenuItem all = new ToolStripMenuItem("All sheets");
    Form sibling;
    ComboBox siblingSetup;
    int queryCount, disabledCount, activeCount, allCount, siblingCount;
    int saveCount; string outputPath;
    protected override bool ShowWithoutActivation { get { return true; } }
    public ActionFixture() {
        Text = "Agent Desktop Workflows Synthetic Action Fixture";
        Name = "ActionFixtureMain"; AccessibleName = Text;
        StartPosition = FormStartPosition.Manual; Location = new Point(30, 30);
        ClientSize = new Size(640, 480); ShowInTaskbar = false;
        AutoScaleMode = AutoScaleMode.None;
        operations.Name = "OperationsMenu"; export.Name = "ExportMenu";
        active.Name = "ActiveSheetMenu"; all.Name = "AllSheetsMenu";
        active.Click += delegate { activeCount++; status.Text = "Active sheet requested"; };
        all.Click += delegate { allCount++; status.Text = "All sheets requested"; };
        export.DropDownItems.Add(active); export.DropDownItems.Add(all);
        operations.DropDownItems.Add(export); menu.Items.Add(operations);
        MainMenuStrip = menu; Controls.Add(menu);
        Controls.Add(new Label { Text = "MES Setup", Location = new Point(20, 48), Size = new Size(120, 25) });
        setup.Name = "SetupSelector"; setup.AccessibleName = "MES Setup";
        setup.Location = new Point(145, 45); setup.Size = new Size(150, 28);
        setup.DropDownStyle = ComboBoxStyle.DropDownList; setup.Items.AddRange(new object[] { "Y", "N" });
        setup.SelectedIndex = 0; Controls.Add(setup);
        Controls.Add(new Label { Text = "Notes", Location = new Point(20, 91), Size = new Size(120, 25) });
        notes.Name = "NotesEditor"; notes.AccessibleName = "Notes";
        notes.Location = new Point(145, 88); notes.Size = new Size(390, 28); notes.Text = "initial";
        Controls.Add(notes);
        enabled.Name = "EnabledOption"; enabled.AccessibleName = "Enable option";
        enabled.Text = "Enable option"; enabled.Location = new Point(20, 130); enabled.Size = new Size(160, 30);
        Controls.Add(enabled);
        query.Name = "QueryButton"; query.AccessibleName = "Run query"; query.Text = "Run query";
        query.Location = new Point(200, 130); query.Size = new Size(150, 30);
        query.Click += delegate { queryCount++; status.Text = "Query count " + queryCount; };
        Controls.Add(query);
        save.Name = "SaveFixtureButton"; save.AccessibleName = "Save synthetic result"; save.Text = "Save synthetic result";
        save.SetBounds(370, 165, 180, 25); save.Click += delegate {
            if (!String.IsNullOrEmpty(outputPath)) { File.WriteAllText(outputPath, "synthetic result " + (++saveCount)); }
        }; Controls.Add(save); save.BringToFront();
        disabled.Name = "DisabledButton"; disabled.AccessibleName = "Unavailable action"; disabled.Text = "Unavailable action";
        disabled.Location = new Point(370, 130); disabled.Size = new Size(180, 30); disabled.Enabled = false;
        disabled.Click += delegate { disabledCount++; }; Controls.Add(disabled);
        grid.Name = "ResultsGrid"; grid.AccessibleName = "Result table";
        grid.Location = new Point(20, 180); grid.Size = new Size(580, 210);
        grid.AllowUserToAddRows = false; grid.RowHeadersVisible = false;
        grid.Columns.Add("Key", "Key"); grid.Columns.Add("Value", "Value");
        grid.Columns[0].ReadOnly = true; grid.Columns[0].Width = 200; grid.Columns[1].Width = 270;
        grid.Rows.Add("ROW-001", "default"); grid.Rows.Add("ROW-002", "second");
        Controls.Add(grid);
        status.Name = "StatusText"; status.AccessibleName = "Operation status"; status.Text = "Ready";
        status.Location = new Point(20, 410); status.Size = new Size(580, 30); Controls.Add(status);
        Shown += delegate {
            Emit(new { ready = true, pid = Process.GetCurrentProcess().Id, hwnd = Handle.ToInt64() });
        };
    }
    void Emit(object value) { Console.WriteLine(json.Serialize(value)); Console.Out.Flush(); }
    object State() {
        return new { pid = Process.GetCurrentProcess().Id, hwnd = Handle.ToInt64(),
            combo = setup.Text, notes = notes.Text, option = enabled.Checked,
            query_count = queryCount, disabled_count = disabledCount,
            save_count = saveCount,
            active_count = activeCount, all_count = allCount, status = status.Text,
            row_value = Convert.ToString(grid.Rows[0].Cells[1].Value),
            editor_value = grid.EditingControl == null ? null : grid.EditingControl.Text,
            menu_open = operations.DropDown.Visible, submenu_open = export.DropDown.Visible,
            menu_hwnd = operations.DropDown.Visible ? operations.DropDown.Handle.ToInt64() : 0,
            submenu_hwnd = export.DropDown.Visible ? export.DropDown.Handle.ToInt64() : 0,
            notes_focused = notes.Focused,
            sibling_hwnd = sibling == null || sibling.IsDisposed ? 0 : sibling.Handle.ToInt64(),
            sibling_count = siblingCount, sibling_combo = siblingSetup == null ? null : siblingSetup.Text };
    }
    void Command(Dictionary<string, object> request) {
        string command = Convert.ToString(request["command"]);
        if (command == "quit") { Close(); return; }
        if (command == "reset") {
            operations.HideDropDown(); export.HideDropDown(); setup.DroppedDown = false;
            setup.SelectedIndex = 0; notes.Text = "initial"; enabled.Checked = false;
            queryCount = disabledCount = activeCount = allCount = siblingCount = 0;
            grid.CancelEdit(); grid.Rows[0].Cells[1].Value = "default"; status.Text = "Ready";
        } else if (command == "output-path") {
            outputPath = Convert.ToString(request["path"]);
        } else if (command == "menu") {
            operations.ShowDropDown(); export.ShowDropDown();
        } else if (command == "grid-editor") {
            grid.CurrentCell = grid.Rows[0].Cells[1]; grid.BeginEdit(true);
            if (grid.EditingControl != null) grid.EditingControl.AccessibleName = "Grid value editor";
        } else if (command == "grid-commit") {
            grid.EndEdit();
        } else if (command == "sibling") {
            if (sibling == null || sibling.IsDisposed) {
                sibling = new Form { Name = "ActionFixtureSibling", Text = "Synthetic sibling business window",
                    StartPosition = FormStartPosition.Manual, Location = new Point(710, 30),
                    ClientSize = new Size(280, 150), ShowInTaskbar = false };
                siblingSetup = new ComboBox { Name = "SetupSelector", AccessibleName = "MES Setup",
                    Location = new Point(20, 20), Size = new Size(160, 30), DropDownStyle = ComboBoxStyle.DropDownList };
                siblingSetup.Items.AddRange(new object[] { "Y", "N" }); siblingSetup.SelectedIndex = 0;
                var button = new Button { Name = "QueryButton", Text = "Run query", AccessibleName = "Run query",
                    Location = new Point(20, 65), Size = new Size(160, 30) };
                button.Click += delegate { siblingCount++; };
                sibling.Controls.Add(siblingSetup); sibling.Controls.Add(button); sibling.Show();
            }
        } else if (command != "state") { Emit(new { error = "unknown_command" }); return; }
        Emit(State());
    }
    protected override void OnFormClosed(FormClosedEventArgs e) {
        if (sibling != null && !sibling.IsDisposed) sibling.Close();
        base.OnFormClosed(e);
    }
    [STAThread] static void Main() {
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        var form = new ActionFixture(); var unused = form.Handle;
        var reader = new Thread(delegate() {
            string line;
            while ((line = Console.ReadLine()) != null) {
                try {
                    var request = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(line);
                    form.BeginInvoke((Action)delegate {
                        try { form.Command(request); }
                        catch { form.Emit(new { error = "fixture_command_failed" }); }
                    });
                } catch { break; }
            }
        });
        reader.IsBackground = true; reader.Start();
        var expiry = new System.Windows.Forms.Timer { Interval = 600000 };
        expiry.Tick += delegate { form.Close(); }; expiry.Start();
        Application.Run(form);
    }
}
