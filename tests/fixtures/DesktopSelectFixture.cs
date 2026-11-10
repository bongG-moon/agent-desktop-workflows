// Compiled only by the development smoke test with ComboEvidenceFixture.cs.
using System;
using System.Diagnostics;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Interop;

sealed class DesktopSelectFixture : Window {
    const string MainTitle = "Synthetic Workbench / Setup";
    const string SiblingTitle = "Synthetic Workbench / Other";
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly EvidenceCombo status = new EvidenceCombo("conditionState", true, false, false, false);
    readonly ComboBox family = new ComboBox();
    readonly TreeViewItem section = new TreeViewItem { Header = "Setup screens" };
    readonly MenuItem export = new MenuItem { Header = "Export" };
    readonly Window sibling = new Window { Title = SiblingTitle, Width = 240, Height = 120, ShowInTaskbar = false };
    int innerQueries, outerQueries, siblingQueries, sheetCommands;

    DesktopSelectFixture() {
        Title = MainTitle; Width = 570; Height = 400; ShowInTaskbar = false;
        var panel = new StackPanel { Margin = new Thickness(12) };
        var toolbar = Group(panel, "Toolbar", "outerCommands");
        var conditions = Group(panel, "Conditions", "workPanel");
        AddButton(toolbar, "toolbarSearch", delegate { outerQueries++; });
        var navigation = new TreeView();
        AutomationProperties.SetAutomationId(navigation, "navigation");
        AutomationProperties.SetAutomationId(section, "setupSection");
        section.Items.Add(new TreeViewItem { Header = "Condition editor" });
        navigation.Items.Add(section);
        toolbar.Children.Add(navigation);
        var menu = new Menu();
        var sheet = new MenuItem { Header = "Active sheet" };
        AutomationProperties.SetAutomationId(export, "exportsMenu");
        AutomationProperties.SetAutomationId(sheet, "sheetAction");
        sheet.Click += delegate { sheetCommands++; };
        export.Items.Add(sheet); menu.Items.Add(export); conditions.Children.Add(menu);
        // The parent value stays empty. The selected child and the fixture model
        // have independent read paths, as in a custom Windows combo provider.
        AddField(conditions, "MES Setup", status);
        family.Items.Add("Standard"); family.Items.Add("Hybrid"); family.SelectedIndex = 0;
        AutomationProperties.SetAutomationId(family, "conditionFamily");
        AddField(conditions, "Family", family);
        AddButton(conditions, "contentSearch", delegate { innerQueries++; });
        Content = panel;
        var siblingPanel = new StackPanel();
        AddButton(siblingPanel, "contentSearch", delegate { siblingQueries++; });
        sibling.Content = siblingPanel;
        ContentRendered += delegate {
            if (!sibling.IsVisible) {
                sibling.ShowActivated = false; sibling.Show();
                Emit("ready");
                new Thread(ReadCommands) { IsBackground = true }.Start();
            }
        };
    }

    static StackPanel Group(StackPanel parent, string label, string id) {
        var group = new GroupBox { Header = label, Margin = new Thickness(0, 2, 0, 4) };
        AutomationProperties.SetName(group, label);
        AutomationProperties.SetAutomationId(group, id);
        var panel = new StackPanel(); group.Content = panel; parent.Children.Add(group);
        return panel;
    }

    static void AddField(StackPanel panel, string text, Control field) {
        var row = new StackPanel { Orientation = Orientation.Horizontal };
        var label = new Label { Content = text, Width = 100, Target = field };
        AutomationProperties.SetName(label, text);
        AutomationProperties.SetName(field, "");
        AutomationProperties.SetLabeledBy(field, label);
        field.Width = 360;
        row.Children.Add(label); row.Children.Add(field); panel.Children.Add(row);
    }

    static void AddButton(StackPanel panel, string id, Action click) {
        var button = new Button { Content = "Search", Margin = new Thickness(4, 2, 4, 2) };
        AutomationProperties.SetAutomationId(button, id);
        button.Click += delegate { click(); };
        panel.Children.Add(button);
    }

    void Emit(string kind) {
        Console.WriteLine(json.Serialize(new {
            kind = kind, pid = Process.GetCurrentProcess().Id,
            hwnd = new WindowInteropHelper(this).Handle.ToInt64(),
            title = Title, sibling_title = sibling.Title,
            status = status.Current, status_writes = status.WriteCount,
            family = family.SelectedItem, text = family.Text,
            expanded = section.IsExpanded, submenu_open = export.IsSubmenuOpen, sheet_commands = sheetCommands,
            inner_queries = innerQueries, outer_queries = outerQueries, sibling_queries = siblingQueries
        }));
        Console.Out.Flush();
    }

    void ReadCommands() {
        string line;
        while ((line = Console.ReadLine()) != null) {
            string command = line;
            Dispatcher.Invoke(new Action(delegate {
                if (command == "quit") { sibling.Close(); Close(); return; }
                if (command == "duplicate-title") sibling.Title = MainTitle;
                if (command == "restore-title") sibling.Title = SiblingTitle;
                Emit("state");
            }));
            if (command == "quit") break;
        }
    }

    [STAThread] static void Main() { new Application().Run(new DesktopSelectFixture()); }
}
