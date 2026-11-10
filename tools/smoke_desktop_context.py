"""Run a recorded query, result-grid context menu and Excel export on a Windows fixture.

The fixture's private command channel reads application state and configures
failure cases. All query, right-click and export actions during replay go
through the desktop-select CLI from --skill-dir.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from smoke_native import Fixture


ROOT = Path(__file__).resolve().parents[1]
TASK = "Device result export"
CASES = ("complete", "painted-result", "no-result", "stale-result", "no-file")


def compile_fixture(work):
    net = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319"
    libs = [net / "WPF" / f"{name}.dll" for name in (
        "PresentationFramework", "PresentationCore", "WindowsBase", "UIAutomationTypes", "UIAutomationProvider")]
    libs += [net / f"{name}.dll" for name in ("System.Web.Extensions", "System.Xaml", "System.IO.Compression")]
    src = ROOT / "tests/fixtures"
    exe = work / "DesktopContextFixture.exe"
    subprocess.run([str(net / "csc.exe"), "/nologo", "/target:exe", "/main:DesktopContextFixture",
                    "/out:" + str(exe), *["/r:" + str(lib) for lib in libs],
                    str(src / "ComboEvidenceFixture.cs"), str(src / "DesktopContextFixture.cs")], check=True)
    return exe


def decoded_cells(path):
    with ZipFile(path) as book:
        assert book.testzip() is None
        rels = ET.fromstring(book.read("_rels/.rels"))
        workbook_target = next(r.attrib["Target"] for r in rels if r.attrib["Type"].endswith("/officeDocument"))
        workbook = ET.fromstring(book.read(workbook_target))
        sheet = workbook.find(".//{*}sheet")
        assert sheet is not None and sheet.attrib["name"] == "Results"
        reference = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        sheet_target = next(r.attrib["Target"] for r in ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
                            if r.attrib["Id"] == reference and r.attrib["Type"].endswith("/worksheet"))
        sheet_data = ET.fromstring(book.read("xl/" + sheet_target))
        return {cell.attrib["r"]: cell.findtext(".//{*}t") for cell in sheet_data.findall(".//{*}c")}


def exercise(skill, work, exe, cases=CASES):
    sys.path.insert(0, str(skill / "scripts"))
    import desktop_select as app
    import context_menu
    from context_teach import raw_result_step
    import learned_tasks as tasks
    import row_action

    tool = skill / "scripts/desktop_select.py"
    app.bridge.set_dpi_awareness()
    results = []

    def check(case, condition, detail=None):
        print(json.dumps({"case": case, "passed": bool(condition)}), flush=True)
        if not condition:
            raise AssertionError((case, detail))
        results.append(case)

    def scenario(case):
        folder = work / case
        folder.mkdir()
        book = folder / "report.xlsx"
        fixture = Fixture(exe, str(book), ready_timeout=25)
        try:
            window = fixture.ready
            hwnd = window["hwnd"]
            binding = app.bridge.Binding(app.pick_window(app.windows(exe.name), window["title"], hwnd))
            row_action.foreground(binding)
            # Capture the actual native identities. The surface is a custom
            # drawing area whose visible data is absent from the UIA tree.
            with app.bridge.UIA() as uia:
                nodes, els = app.observe(binding, uia, identities_only=True)
                grid_index, = [i for i, n in enumerate(nodes) if n.get("automation_id") == "ResultSurface"]
                assert nodes[grid_index]["role"] == "table" and not list(uia.children(els[grid_index]))
                def observed(identity):
                    node, = [n for n in nodes if n.get("automation_id") == identity]
                    return {"role": node["role"], "automation_id": node["automation_id"]}
                def combo(identity, label, instruction):
                    raw = {"action": "teach-target", "field_label": label,
                           "target": {"kind": "element", "locator": observed(identity)},
                           "recognition": {"target": "identified", "action": "requires_instruction",
                                           "result": "manual", "reason": "combo_choice_unproven"}}
                    raw["instruction"] = tasks.compile_instruction(raw, instruction)
                    return raw
                steps = [combo("conditionState", "MES Setup", "MES SETUP조건을 N으로 해줘"),
                         combo("conditionFamily", "Family", "입력받은 변수를 FAMILY조건에 입력해줘")]
                trigger = observed("QueryCommand")
                ready = {"kind": "text", "target": observed("ResultIndicator"), "expected": "RESULT READY"}
                region = observed("ResultSurface")
            x, y, w, h = window["grid_bounds"]
            menu = context_menu.teach_at(binding, (x+w//2, y+h//2), ["EXPORT", "EXPORT TO EXCEL"],
                                         target=region["automation_id"], role=region["role"])
            if case == "painted-result":
                import result_menu
                fixture.command("reference-ready")
                # The box covers a status glyph visually painted by the cell-less surface.
                ready = result_menu.teach_visual(binding, menu["target"],
                    [int(w*.01), int(h*.03), int(w*.31), int(h*.22)])
                fixture.command("reset")
            steps.append(raw_result_step("Query and export", menu, trigger, ready,
                                         {"kind": "file", "path": str(book)}))
            plan = tasks.from_recording(TASK, exe.name, window["title"], steps)
            tasks.save_task(plan, folder / "tasks")
            env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(folder / "tasks"))
            def replay(*extra):
                args = ["replay", "--exe", exe.name, "--window", window["title"], "--hwnd", str(hwnd),
                        "--task", TASK, *extra]
                out = subprocess.run([sys.executable, "-X", "utf8", "-B", str(tool), *args], cwd=folder,
                                     capture_output=True, encoding="utf8", timeout=165, env=env)
                return json.loads(out.stdout)
            if case == "complete":
                baseline = fixture.command("state")
                missing = replay()
                check("missing_family_before_any_input", missing.get("status") == "needs_input"
                      and missing["input_dispatched"] is False and fixture.command("state") == baseline, missing)
            if case == "no-result":
                fixture.command("hold-query")
            if case == "stale-result":
                fixture.command("stale-result")
            if case == "no-file":
                fixture.command("skip-file")
            expected_family = "DRAM" if case == "painted-result" else "Hybrid"
            run = replay("--set", "Family=" + expected_family)
            actual = fixture.command("state")
            (folder / "receipt.json").write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf8")
            effect = run["steps"][-1]["receipt"]
            if case in ("complete", "painted-result"):
                prefix = "painted_" if case == "painted-result" else ""
                check(prefix + "single_full_replay_verified", run["status"] == "verified" and len(run["steps"]) == 3
                      and all(effect.get(key) is True for key in ("query_verified", "leaf_dispatched", "workbook_verified"))
                      and effect.get("menu_items") == ["EXPORT", "EXPORT TO EXCEL"], run)
                check(prefix + "submitted_fixed_and_runtime_conditions", (actual["status"], actual["family"],
                      actual["result_status"], actual["result_family"]) == ("N", expected_family, "N", expected_family), actual)
                check(prefix + "query_real_right_click_and_owned_menu_exactly_once", tuple(actual[k] for k in (
                    "queries", "generation", "right_clicks", "menus", "parents", "leaves", "wrong_point", "unrelated"))
                    == (1, 1, 1, 1, 1, 1, 0, 0), actual)
                check(prefix + "real_xlsx_contains_query_results", decoded_cells(book) == {
                    "A1": "MES Setup", "B1": "Family", "C1": "Generation", "A2": "N", "B2": expected_family, "C2": "1"})
            elif case == "no-result":
                check("no_right_click_when_query_never_completes", effect.get("diagnostic") == "query_fresh_result_not_observed"
                      and effect.get("leaf_dispatched") is False and actual["queries"] == 1
                      and not actual["ready"] and actual["right_clicks"] == 0 and actual["leaves"] == 0
                      and not book.exists(), (run, actual))
            elif case == "stale-result":
                check("previous_ready_is_not_a_new_query_result", effect.get("diagnostic") == "query_fresh_result_not_observed"
                      and effect.get("leaf_dispatched") is False and actual["queries"] == 1
                      and actual["ready"] and actual["generation"] == 0 and actual["right_clicks"] == actual["leaves"] == 0
                      and not book.exists(), (run, actual))
            else:
                check("missing_file_stops_without_reinvocation", effect.get("diagnostic") == "export_workbook_not_observed"
                      and effect.get("workbook_verified") is False and effect.get("leaf_dispatched") is True
                      and actual["queries"] == actual["right_clicks"] == actual["leaves"] == 1
                      and not book.exists(), (run, actual))
        finally:
            fixture.close()
    for case in cases:
        scenario(case)
    print(json.dumps({"passed": len(results), "total": len(results), "company_fx_tested": False}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=ROOT / "skills/desktop-select")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--compile-only", action="store_true")
    parser.add_argument("--case", action="append", choices=CASES, help="Limit the run to a specific scenario")
    args = parser.parse_args()
    if os.name != "nt" or not args.work_dir.is_absolute() or args.work_dir.exists():
        parser.error("This Windows test requires a new absolute work directory.")
    args.work_dir.mkdir(parents=True)
    exe = compile_fixture(args.work_dir)
    if not args.compile_only:
        exercise(args.skill_dir.resolve(), args.work_dir, exe, args.case or CASES)


if __name__ == "__main__":
    main()
