"""Actual Windows acceptance for the rebuilt 0.4 branch.

The raw UIA fixture reproduces missing IsOffscreen, duplicate edit peers,
virtual options and a provider that accepts SetValue but not Select. Fixture
commands prepare conditions or observe counters only; all tested actions use
the product CLI. The fixture is development-only and never ships in the ZIP.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from smoke_native import Fixture

ROOT = Path(__file__).resolve().parents[1]

CAPTURE_PROBE = r'''
import json, queue, sys, time
sys.path.insert(0, sys.argv[1])
import desktop_select as app
import control_lookup
from teach_capture import CaptureNotes, _CaptureBridge, _CaptureUIA, _Worker
app.bridge.set_dpi_awareness()
binding = app.bridge.Binding(app.pick_window(app.windows(sys.argv[2]), sys.argv[3], int(sys.argv[4])))
app.bridge.user32.SetForegroundWindow(binding.hwnd)
with _CaptureUIA(binding) as uia:
    combo = control_lookup.resolve(binding, uia, {'automation_id':'conditionState','role':'combobox'})
    box = uia.bounds(combo)
    x, y = round(box['x']+box['width']/2), round(box['y']+box['height']/2)
    observed = uia.observe_point(binding, x, y)
    notes = CaptureNotes()
    worker = _Worker(binding, _CaptureBridge(binding), notes, queue.Queue(), queue.Queue())
    worker.uia = uia
    worker.capture_action({'action':'click','x':x,'y':y,'at':time.monotonic()}, observed)
    print(json.dumps({'steps':notes.snapshot(), 'point_hit_role':observed['node']['role'],
                      'point_value':observed['node'].get('value'), 'input_dispatched':False}))
'''


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")


def compile_fixture(work):
    net = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319"
    refs = [net / "WPF" / (name + ".dll") for name in (
        "WindowsBase", "UIAutomationTypes", "UIAutomationProvider")]
    refs += [net / (name + ".dll") for name in (
        "System.Windows.Forms", "System.Drawing", "System.Web.Extensions")]
    exe = work / "ProviderCompatibilityFixture.exe"
    subprocess.run([str(net / "csc.exe"), "/nologo", "/target:exe", "/out:" + str(exe),
                    *["/r:" + str(ref) for ref in refs],
                    str(ROOT / "tests/fixtures/ProviderCompatibilityFixture.cs")], check=True)
    return exe


def install_skill(source, target):
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("win_bridge.py", "recorder.py"):
        if not (target / "scripts" / name).is_file():
            shutil.copyfile(ROOT / "scripts" / name, target / "scripts" / name)


def cells(path):
    with ZipFile(path) as book:
        assert book.testzip() is None
        data = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
        return {cell.attrib["r"]: cell.findtext(".//{*}t") for cell in data.findall(".//{*}c")}


def exercise(source, baseline, work, baseline_only=False):
    installed = work / "skills" / "desktop-select"
    if not baseline_only:
        install_skill(source, installed)
    exe = compile_fixture(work)
    fixture = Fixture(exe)
    checks, results = [], []
    sequence = 0

    def check(name, condition, detail=None):
        checks.append({"case": name, "passed": bool(condition)})
        print(json.dumps(checks[-1]), flush=True)
        if not condition:
            write_json(work / "failed-case.json", {"case": name, "detail": detail})
            raise AssertionError((name, detail))

    def setup(mode="normal", wrapped=False):
        fixture.command("restore")
        fixture.command("clear-extra")
        fixture.command("reset")
        fixture.command("label-wrappers" if wrapped else "label-direct")
        fixture.command("query-caption|조회")
        for key in ("conditionState", "conditionFamily"):
            fixture.command("mode|" + key + "|" + mode)

    def cli(variant, name, command, *extra):
        nonlocal sequence
        sequence += 1
        skill = baseline if variant == "baseline" else installed
        stem = f"{sequence:02d}-{variant}-{name}"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                   DESKTOP_SELECT_TASK_DIR=str(work / "unused-task-directory"))
        bind = ["--exe", exe.name, "--window", fixture.ready["title"], "--hwnd", str(fixture.ready["hwnd"])]
        fixture.command("reset-metrics")
        start = time.perf_counter()
        proc = subprocess.run([sys.executable, "-X", "utf8", "-B", str(skill / "scripts/desktop_select.py"),
                               command, *bind, *extra], capture_output=True, encoding="utf8", env=env,
                              cwd=work, timeout=90)
        elapsed = time.perf_counter() - start
        try:
            receipt = json.loads(proc.stdout)
        except ValueError as error:
            raise AssertionError((stem, proc.returncode, proc.stdout, proc.stderr)) from error
        result = {"variant": variant, "name": name, "receipt": receipt,
                  "seconds": round(elapsed, 6), "state": fixture.command("state"),
                  "provider": fixture.command("metrics"), "exit_code": proc.returncode, "stderr": proc.stderr}
        results.append(result)
        write_json(work / (stem + ".json"), result)
        print(json.dumps({"variant": variant, "name": name, "status": receipt.get("status"),
                          "diagnostic": receipt.get("diagnostic"), "seconds": result["seconds"]}), flush=True)
        return result

    def selected(name, result, key="conditionState", value="N"):
        receipt, state = result["receipt"], result["state"]["controls"][key]
        check(name, receipt.get("status") == "verified" and state["value"] == value
              and state["writes"] == 1 and state["expands"] == state["selects"] == 0, result)

    try:
        if baseline is not None:
            for name, command, extra in (
                ("label_select", "select", ("--target", "MES Setup", "--value", "N")),
                ("id_select", "select", ("--target", "conditionState", "--value", "N")),
                ("query", "open", ("--target", "조회")),
            ):
                setup()
                result = cli("baseline", name, command, *extra)
                check("original_040_" + name + "_failure_reproduced", result["receipt"].get("status") != "verified"
                      and result["state"]["controls"]["conditionState"]["writes"] == 0
                      and result["state"]["opens"] == 0, result)
        if baseline_only:
            return

        for wrapped in (False, True):
            prefix = "wrapped_" if wrapped else "direct_"
            for name, target, key, value in (
                ("label", "MES Setup", "conditionState", "N"),
                ("id", "conditionState", "conditionState", "N"),
                ("family", "Family", "conditionFamily", "Hybrid"),
            ):
                setup(wrapped=wrapped)
                result = cli("current", prefix + name, "select", "--target", target, "--value", value)
                selected(prefix + name + "_one_setvalue_no_expand_or_select", result, key, value)
            setup(wrapped=wrapped)
            result = cli("current", prefix + "query", "open", "--target", "조회")
            check(prefix + "query_invoked_once", result["receipt"].get("status") == "delivered_unverified"
                  and result["receipt"].get("input_dispatched") is True and result["state"]["opens"] == 1, result)

        for wrapped in (False, True):
            setup("no-options-blank-before", wrapped=wrapped)
            before = fixture.command("state")
            process = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", CAPTURE_PROBE,
                                      str(installed / "scripts"), exe.name, fixture.ready["title"],
                                      str(fixture.ready["hwnd"])], capture_output=True, encoding="utf8", timeout=30)
            try:
                captured = json.loads(process.stdout)
            except ValueError as error:
                raise AssertionError((process.stdout, process.stderr)) from error
            name = ("wrapped" if wrapped else "direct") + "_point_capture"
            write_json(work / (name + ".json"), captured)
            steps = captured.get("steps", [])
            check(name + "_keeps_unreadable_combo_as_target", process.returncode == 0 and len(steps) == 1
                  and steps[0].get("action") == "teach-target"
                  and steps[0].get("target", {}).get("locator", {}).get("automation_id") == "conditionState"
                  and steps[0].get("field_label") == "MES Setup"
                  and fixture.command("state") == before, captured)

        setup()
        before = fixture.command("state")
        result = cli("current", "inspect", "inspect", "--target", "MES Setup", "--role", "combobox")
        check("inspect_missing_visibility_and_duplicate_peers_without_input",
              result["receipt"].get("status") == "resolved" and result["receipt"].get("value") == "W"
              and result["receipt"].get("input_dispatched") is False and result["state"] == before, result)

        fixture.command("minimize")
        before = fixture.command("state")
        result = cli("current", "minimized_inspect", "inspect", "--target", "MES Setup", "--role", "combobox")
        check("readonly_inspect_never_restores", result["receipt"].get("input_dispatched") is False
              and result["state"] == before and result["state"]["minimized"], result)
        result = cli("current", "minimized_select", "select", "--target", "MES Setup", "--value", "N")
        selected("minimized_select_restores_and_writes_once", result)
        check("restored_before_write", not result["state"]["minimized"], result)
        fixture.command("minimize")
        result = cli("current", "minimized_open", "open", "--target", "조회")
        check("minimized_button_restores_and_invokes_once", result["receipt"].get("status") == "delivered_unverified"
              and result["state"]["opens"] == 1 and not result["state"]["minimized"], result)

        for mode in ("unsupported-option", "uncertain-expand", "uncertain-select", "no-options-blank-before",
                     "selected-after-write"):
            setup(mode)
            result = cli("current", mode, "select", "--target", "MES Setup", "--value", "N")
            selected(mode + "_one_setvalue_no_failed_fallback", result)

        setup("name-is-value")
        result = cli("current", "combo_name_follows_value", "select", "--target", "MES Setup", "--value", "N")
        updated = cli("current", "combo_name_after_write", "inspect", "--target", "conditionState", "--role", "combobox")
        state = updated["state"]["controls"]["conditionState"]
        check("same_combo_name_changes_with_value_without_invalidating_identity",
              result["receipt"].get("status") == "verified"
              and result["receipt"].get("target", {}).get("name") == "W"
              and updated["receipt"].get("target", {}).get("name") == "N"
              and state["value"] == "N" and state["writes"] == 1
              and state["selects"] == state["expands"] == 0, (result, updated))

        setup("conflict-editors")
        result = cli("current", "conflicting_readback", "select", "--target", "MES Setup", "--value", "N")
        state = result["state"]["controls"]["conditionState"]
        check("true_conflict_not_verified_or_retried", result["receipt"].get("status") == "delivered_unverified"
              and result["receipt"].get("input_dispatched") is True and state["writes"] == 1
              and state["expands"] == state["selects"] == 0, result)

        setup("uncertain-write")
        result = cli("current", "uncertain_write", "select", "--target", "MES Setup", "--value", "N")
        state = result["state"]["controls"]["conditionState"]
        check("post_mutation_exception_not_retried", result["receipt"].get("status") == "uncertain"
              and result["receipt"].get("input_dispatched") is None and state["writes"] == 1
              and state["expands"] == state["selects"] == 0, result)

        for mode, prepare in (("explicit_offscreen", "mode|conditionState|explicit-offscreen"),
                              ("duplicate_id", "duplicate|conditionState"),
                              ("missing_id", "hide|conditionState")):
            setup()
            fixture.command(prepare)
            result = cli("current", mode, "select", "--target", "conditionState", "--value", "N")
            check(mode + "_no_mutation", result["receipt"].get("status") == "blocked"
                  and result["receipt"].get("input_dispatched") is False
                  and result["state"]["controls"]["conditionState"]["writes"] == 0, result)

        for family in ("Hybrid", "DRAM"):
            setup("selected-after-write")
            book = work / (family + ".xlsx")
            fixture.command("export-path|" + str(book))
            selected(family + "_fixed_condition", cli("current", family + "_status", "select", "--target", "MES Setup", "--value", "N"))
            selected(family + "_variable_condition", cli("current", family + "_family", "select", "--target", "Family", "--value", family), "conditionFamily", family)
            query = cli("current", family + "_query", "open", "--target", "QueryCommand")
            export = cli("current", family + "_export", "open", "--target", "ExportCommand")
            values = cells(book)
            check(family + "_real_workbook_matches_current_query", query["state"]["opens"] == 1
                  and export["state"]["exports"] == 1 and values.get("A2") == "N"
                  and values.get("B2") == family and values.get("C2") == "1", values)
        check("direct_commands_need_no_saved_task", not (work / "unused-task-directory").exists())
    finally:
        fixture.close()
        summary = {"passed": sum(c["passed"] for c in checks), "total": len(checks), "checks": checks,
                   "baseline_only": baseline_only, "company_fx_tested": False,
                   "note": "Synthetic raw Windows UIA provider; no company binary or CUA driver used."}
        write_json(work / "summary.json", summary)
        write_json(work / "results.json", results)
        print(json.dumps({k: v for k, v in summary.items() if k != "checks"}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=ROOT / "skills/desktop-select")
    parser.add_argument("--baseline-skill", type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--baseline-only", action="store_true")
    args = parser.parse_args()
    if (os.name != "nt" or not args.work_dir.is_absolute() or args.work_dir.exists()
            or args.baseline_only and args.baseline_skill is None):
        parser.error("Requires Windows, a new absolute work directory, and baseline path for baseline-only")
    args.work_dir.mkdir(parents=True)
    exercise(args.skill_dir.resolve(), args.baseline_skill.resolve() if args.baseline_skill else None,
             args.work_dir, args.baseline_only)


if __name__ == "__main__":
    main()
