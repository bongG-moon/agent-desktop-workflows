// A native WPF table whose headers are absent from the control view and whose cells
// expose their contents only through a read-only ValuePattern.
using System;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Peers;
using System.Windows.Automation.Provider;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

public sealed class ProviderResultGrid : Grid {
    public int HeaderReads, ItemReads, ValueReads, WrongColumns;
    public int KeyColumn, MatchRow;
    public int VisibleRows = 2;
    readonly bool rowOnly = Array.IndexOf(Environment.GetCommandLineArgs(), "--row-only") >= 0;
    public string Scenario = "match";
    public Action<string> OpenResult;
    readonly DisplayCell[,] cells = new DisplayCell[3,2];

    public ProviderResultGrid() {
        Height = 100;
        Margin = new Thickness(8);
        AutomationProperties.SetAutomationId(this, "ProviderResults");
        for (int i=0; i<3; i++) RowDefinitions.Add(new RowDefinition { Height = new GridLength(28) });
        for (int j=0; j<2; j++) ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(150) });
        for (int row=0; row<3; row++) for (int column=0; column<2; column++) {
            var cell = new DisplayCell(this, row, column);
            cells[row, column] = cell;
            SetRow(cell, row); SetColumn(cell, column); Children.Add(cell);
        }
        MouseLeftButtonDown += delegate(object sender, MouseButtonEventArgs e) {
            var cell = e.OriginalSource as DisplayCell;
            if (e.ClickCount != 2 || cell == null || cell.Row == 0) return;
            if (!rowOnly && cell.Column != KeyColumn) WrongColumns++;
            OpenResult(cells[cell.Row, KeyColumn].Caption);
        };
    }
    public void Fill(string expected, int queryNumber) {
        KeyColumn = queryNumber % 2;
        VisibleRows = Scenario == "empty" ? 0 : Scenario == "single" ? 1 : 2;
        MatchRow = Scenario == "single" || Scenario == "first" ? 1 : (queryNumber % 2 == 0 ? 1 : 2);
        for (int column=0; column<2; column++) {
            cells[0, column].Show(column == KeyColumn ? "Equip ID" : "Category");
            AutomationProperties.SetAutomationId(cells[0, column], column == KeyColumn ? "KeyHeader" : "CategoryHeader");
            for (int row=1; row<=2; row++) {
                cells[row, column].Visibility = row <= VisibleRows ? Visibility.Visible : Visibility.Collapsed;
                // The other row has the requested text in a DIFFERENT column.
                string key = Scenario == "missing" ? "OTHER-" + row :
                    (row == MatchRow || Scenario == "duplicate" ? expected : "OTHER");
                cells[row, column].Show(column == KeyColumn
                    ? key
                    : (row == MatchRow ? "requested" : expected));
            }
        }
    }
    protected override AutomationPeer OnCreateAutomationPeer() { return new TablePeer(this); }

    sealed class TablePeer : FrameworkElementAutomationPeer, ITableProvider, IGridProvider {
        readonly ProviderResultGrid grid;
        public TablePeer(ProviderResultGrid owner) : base(owner) { grid=owner; }
        // Some business grids expose only a Group role despite implementing Grid/Table.
        protected override AutomationControlType GetAutomationControlTypeCore() { return AutomationControlType.Group; }
        public override object GetPattern(PatternInterface pattern) {
            return pattern == PatternInterface.Table || pattern == PatternInterface.Grid ? this : base.GetPattern(pattern);
        }
        public int ColumnCount { get { return 2; } }
        public int RowCount { get { return grid.VisibleRows; } }
        public RowOrColumnMajor RowOrColumnMajor { get { return RowOrColumnMajor.RowMajor; } }
        public IRawElementProviderSimple[] GetColumnHeaders() {
            grid.HeaderReads++;
            return new [] { ProviderFromPeer(CreatePeerForElement(grid.cells[0,0])),
                            ProviderFromPeer(CreatePeerForElement(grid.cells[0,1])) };
        }
        public IRawElementProviderSimple[] GetRowHeaders() { return new IRawElementProviderSimple[0]; }
        public IRawElementProviderSimple GetItem(int row, int column) {
            grid.ItemReads++;
            return ProviderFromPeer(CreatePeerForElement(grid.cells[row+1, column]));
        }
    }

    sealed class DisplayCell : FrameworkElement {
        public readonly int Row, Column;
        public readonly ProviderResultGrid Table;
        public string Caption = "";
        public DisplayCell(ProviderResultGrid table, int row, int column) { Table=table; Row=row; Column=column; }
        public void Show(string caption) { Caption=caption; InvalidateVisual(); }
        protected override AutomationPeer OnCreateAutomationPeer() { return new CellPeer(this); }
        protected override void OnRender(DrawingContext drawing) {
            drawing.DrawRectangle(Brushes.White, null, new Rect(RenderSize));
            var label = new FormattedText(Caption, System.Globalization.CultureInfo.InvariantCulture,
                FlowDirection.LeftToRight, new Typeface("Segoe UI"), 15, Brushes.Black,
                VisualTreeHelper.GetDpi(this).PixelsPerDip);
            drawing.DrawText(label, new Point(4, 2));
        }
    }
    sealed class CellPeer : FrameworkElementAutomationPeer, IGridItemProvider, IValueProvider {
        readonly DisplayCell cell;
        public CellPeer(DisplayCell owner) : base(owner) { cell=owner; }
        protected override AutomationControlType GetAutomationControlTypeCore() {
            return cell.Row == 0 ? AutomationControlType.HeaderItem : AutomationControlType.DataItem;
        }
        protected override bool IsControlElementCore() { return cell.Row != 0; }
        protected override bool IsContentElementCore() { return cell.Row != 0; }
        protected override string GetNameCore() { return cell.Row == 0 ? cell.Caption : ""; }
        public override object GetPattern(PatternInterface pattern) {
            return cell.Row != 0 && (pattern == PatternInterface.GridItem || pattern == PatternInterface.Value)
                ? this : base.GetPattern(pattern);
        }
        public int Row { get { return cell.Row-1; } }
        public int Column { get { return cell.Column; } }
        public int RowSpan { get { return 1; } }
        public int ColumnSpan { get { return 1; } }
        public IRawElementProviderSimple ContainingGrid { get { return ProviderFromPeer(CreatePeerForElement(cell.Table)); } }
        public bool IsReadOnly { get { return true; } }
        public string Value { get { cell.Table.ValueReads++; return cell.Caption; } }
        public void SetValue(string value) { throw new InvalidOperationException("read-only"); }
    }
}
