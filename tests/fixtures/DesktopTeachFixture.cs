// Uses the custom UIA combo providers in ComboPopupFixture.cs. Commands on
// stdin reorder the form and report independent application state.
using System;
using System.Diagnostics;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Interop;

sealed class DesktopTeachFixture : Window {
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly PopupCombo first = new PopupCombo("ComboAlpha", true, false, false);
    PopupCombo second = new PopupCombo("ComboBeta", true, false, false);
    readonly PopupCombo unrelated = new PopupCombo("OtherField", true, false, false);
    readonly StackPanel rows = new StackPanel { Margin = new Thickness(20) };
    readonly Button search = new Button { Content = "Search", Margin = new Thickness(0, 12, 0, 0) };
    int queries;

    DesktopTeachFixture() {
        Title = "Desktop Select synthetic teaching form";
        Left = 45; Top = 65; Width = 540; Height = 345;
        WindowStartupLocation = WindowStartupLocation.Manual;
        ShowInTaskbar = false;
        rows.Children.Add(Field("MES Setup", first));
        rows.Children.Add(Field("Family", second));
        AutomationProperties.SetAutomationId(search, "QueryCommand");
        search.Click += delegate { queries++; };
        rows.Children.Add(search);
        var main = new StackPanel();
        var conditions = new GroupBox { Header = "Conditions", Content = rows };
        AutomationProperties.SetAutomationId(conditions, "ConditionsGroup");
        main.Children.Add(conditions);
        var other = new GroupBox { Header = "Other", Content = Field("Outside", unrelated) };
        AutomationProperties.SetAutomationId(other, "OtherGroup");
        main.Children.Add(other);
        Content = main;
        ContentRendered += delegate {
            Emit("ready");
            new Thread(ReadCommands) { IsBackground = true }.Start();
        };
    }

    void EmitTarget(string id) {
        FrameworkElement target = null;
        string role = "button";
        foreach (var combo in new [] { first, second }) {
            if (id == combo.Key + "Open") target = combo.Arrow;
            if (id == combo.Key + "Current") { target = combo.Editor; role = "textfield"; }
            if (id.StartsWith(combo.Key + "Option") && combo.Popup != null) {
                target = combo.Options.Find(item => AutomationProperties.GetAutomationId(item) == id);
                role = "listitem";
            }
        }
        if (id == "QueryCommand") target = search;
        if (target == null || !target.IsVisible) {
            Console.WriteLine("{\"available\":false}");
        } else {
            Point top = target.PointToScreen(new Point(0, 0));
            Point end = target.PointToScreen(new Point(target.ActualWidth, target.ActualHeight));
            Console.WriteLine(json.Serialize(new { available = true, role = role,
                automation_id = AutomationProperties.GetAutomationId(target),
                bounds = new { x = top.X, y = top.Y, width = end.X - top.X, height = end.Y - top.Y } }));
        }
        Console.Out.Flush();
    }

    static UIElement Field(string name, PopupCombo combo) {
        var row = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 5, 0, 5) };
        var label = new Label { Content = name, Target = combo, Width = 100 };
        AutomationProperties.SetLabeledBy(combo, label);
        row.Children.Add(label);
        row.Children.Add(combo);
        return row;
    }

    void Emit(string kind) {
        Console.WriteLine(json.Serialize(new {
            kind = kind, hwnd = new WindowInteropHelper(this).Handle.ToInt64(),
            pid = Process.GetCurrentProcess().Id, title = Title, queries = queries,
            mode = first.Current, state = second.Current, outside = unrelated.Current, mode_clicks = first.ClickCount,
            state_clicks = second.ClickCount, stale_selects = first.StaleSelectCount + second.StaleSelectCount,
            mode_open = first.Popup != null, state_open = second.Popup != null
        }));
        Console.Out.Flush();
    }

    void ReadCommands() {
        string command;
        while ((command = Console.ReadLine()) != null) {
            Dispatcher.Invoke(new Action(delegate {
                if (command == "quit") { first.ClosePopup(); second.ClosePopup(); Close(); return; }
                if (command.StartsWith("target|")) { EmitTarget(command.Substring(7)); return; }
                if (command == "activate") Activate();
                if (command == "reset") { first.Reset(); second.Reset(); queries = 0; }
                if (command == "reorder") {
                    UIElement original = rows.Children[0];
                    rows.Children.RemoveAt(0); rows.Children.Insert(1, original);
                    Left += 40; UpdateLayout();
                }
                if (command == "reuse-family-id") {
                    int index = rows.Children.IndexOf((UIElement)second.Parent);
                    rows.Children.RemoveAt(index);
                    second = new PopupCombo("ComboBeta", true, false, false);
                    rows.Children.Insert(index, Field("Packaging", second));
                    UpdateLayout();
                }
                Emit("state");
            }));
            if (command == "quit") break;
        }
    }
    [STAThread] static void Main() { new Application().Run(new DesktopTeachFixture()); }
}
