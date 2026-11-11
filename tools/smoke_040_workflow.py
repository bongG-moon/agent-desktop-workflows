"""Exercise the simple 0.4 rebuild's generated Python workflow on Windows.

Registration and every action use the shipped interface. Only independent
fixture observation/setup uses its pipe. Workbooks are reopened to validate
the actual current query values, separately from the action receipts.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

from smoke_040_rebuild import ROOT, Fixture, cells, compile_fixture, install_skill, write_json


PROBE = r'''
import atexit, json, os, sys
sys.path.insert(0, os.environ['DS_SMOKE_SCRIPTS'])
import desktop_select as app
counts = dict(pid=os.getpid(), uia_sessions=0, full_tree_snapshots=0, native_findall=0, value_reads=0)
original_init = app.bridge.UIA.__init__
def init(self, *args, **kwargs):
    counts['uia_sessions'] += 1
    original_init(self, *args, **kwargs)
app.bridge.UIA.__init__ = init
original_handle = app.bridge.UIA.from_handle
def from_handle(self, *args, **kwargs):
    frame = sys._getframe(1)
    if frame.f_code.co_name == 'observe' and frame.f_code.co_filename.endswith('desktop_select.py'):
        counts['full_tree_snapshots'] += 1
    return original_handle(self, *args, **kwargs)
app.bridge.UIA.from_handle = from_handle
original_call = app.bridge.Element.call
def element_call(self, slot, *args, **kwargs):
    if slot == 6:
        counts['native_findall'] += 1
    return original_call(self, slot, *args, **kwargs)
app.bridge.Element.call = element_call
original_value = app.bridge.UIA._accessible_value
def accessible_value(self, *args, **kwargs):
    counts['value_reads'] += 1
    return original_value(self, *args, **kwargs)
app.bridge.UIA._accessible_value = accessible_value
@atexit.register
def save():
    with open(os.environ['DS_SMOKE_METRICS'], 'a', encoding='utf8') as stream:
        stream.write(json.dumps(counts) + '\n')
'''


def recipe_for(exe, title):
    def selector(identifier, role):
        return dict(automation_id=identifier, role=role)
    return dict(schema=1, name="Registered query and Excel", exe=exe, window=title, steps=[
        dict(action="set-value", selector=selector("conditionState", "combobox"), value="N"),
        dict(action="set-value", selector=selector("conditionFamily", "combobox"), parameter="Family"),
        dict(action="invoke", selector=selector("QueryCommand", "button")),
        dict(action="wait", seconds=.1),
        dict(action="invoke", selector=selector("ExportCommand", "button")),
    ])


def exercise(source, work, repeats=3):
    installed = work / "skills" / "desktop-select"
    install_skill(source, installed)
    probe = work / "probe"
    probe.mkdir()
    (probe / "sitecustomize.py").write_text(PROBE, encoding="utf8")
    exe = compile_fixture(work)
    fixture = Fixture(exe)
    helper = installed / "scripts/python_workflow.py"
    direct = installed / "scripts/desktop_select.py"
    generated = work / "skills" / "registered-query"
    launcher = generated / "scripts/run.py"
    recipe = work / "recipe.json"
    write_json(recipe, recipe_for(exe.name, fixture.ready["title"]))
    book = work / "report.xlsx"
    fixture.command("export-path|" + str(book))
    results, checks = [], []
    sequence = 0

    def check(name, condition, detail=None):
        checks.append(dict(case=name, passed=bool(condition)))
        print(json.dumps(checks[-1]), flush=True)
        if not condition:
            write_json(work / "failed-case.json", dict(case=name, detail=detail))
            raise AssertionError((name, detail))

    def call(name, script, *args):
        nonlocal sequence
        sequence += 1
        metrics_path = work / (f"{sequence:02d}-{name}-processes.jsonl")
        env = dict(os.environ, PYTHONPATH=str(probe), PYTHONDONTWRITEBYTECODE="1",
                   DS_SMOKE_SCRIPTS=str(installed / "scripts"), DS_SMOKE_METRICS=str(metrics_path),
                   DESKTOP_SELECT_TASK_DIR=str(work / "unused-task-directory"))
        fixture.command("reset-metrics")
        start = time.perf_counter()
        proc = subprocess.run([sys.executable, "-X", "utf8", "-B", str(script), *args],
                              capture_output=True, encoding="utf8", cwd=work, env=env, timeout=90)
        elapsed = time.perf_counter() - start
        try:
            receipt = json.loads(proc.stdout)
        except ValueError as error:
            raise AssertionError((name, proc.returncode, proc.stdout, proc.stderr)) from error
        processes = [json.loads(line) for line in metrics_path.read_text(encoding="utf8").splitlines()]
        metric = dict(seconds=round(elapsed, 6), processes=len(processes))
        for key in ("uia_sessions", "full_tree_snapshots", "native_findall", "value_reads"):
            metric[key] = sum(row[key] for row in processes)
        result = dict(name=name, receipt=receipt, metrics=metric, exit_code=proc.returncode,
                      stderr=proc.stderr, state=fixture.command("state"), provider=fixture.command("metrics"))
        results.append(result)
        write_json(work / f"{sequence:02d}-{name}.json", result)
        print(json.dumps(dict(name=name, status=receipt.get("status"), metrics=metric)), flush=True)
        return result

    def setup(clutter=0):
        fixture.command("restore")
        fixture.command("clear-extra")
        fixture.command("reset")
        fixture.command("label-direct")
        if clutter:
            fixture.command("clutter|" + str(clutter))
        for key in ("conditionState", "conditionFamily"):
            fixture.command("mode|" + key + "|selected-after-write")

    try:
        setup()
        fixture.command("label-wrappers")
        before = fixture.command("state")
        discovered = call("discover", helper, "discover", "--exe", exe.name, "--window", fixture.ready["title"])
        targets = {item.get("automation_id"): item for item in discovered["receipt"].get("targets", [])}
        check("discovery_maps_wrapped_labels_without_input", discovered["receipt"].get("status") == "observed"
              and targets.get("conditionState", {}).get("semantic_label") == "MES Setup"
              and targets.get("conditionFamily", {}).get("semantic_label") == "Family"
              and discovered["state"] == before, discovered)
        registration = call("register", helper, "register", "--recipe", str(recipe), "--out", str(generated))
        check("registration_is_readonly", registration["receipt"].get("status") == "registered"
              and registration["receipt"].get("required_parameters") == ["Family"]
              and registration["receipt"].get("input_dispatched") is False and registration["state"] == before, registration)
        files = sorted(str(path.relative_to(generated)).replace("\\", "/") for path in generated.rglob("*") if path.is_file())
        check("registration_creates_three_text_files", files == ["SKILL.md", "scripts/run.py", "workflow.json"], files)
        stored = json.loads((generated / "workflow.json").read_text(encoding="utf8"))
        check("variable_binding_stays_required", stored["steps"][1].get("parameter") == "Family"
              and "value" not in stored["steps"][1], stored)

        fixture.command("minimize")
        before = fixture.command("state")
        missing = call("missing_parameter", launcher)
        check("missing_parameter_precedes_restore_and_input", missing["receipt"].get("status") == "needs_input"
              and missing["receipt"].get("input_dispatched") is False and missing["state"] == before, missing)
        observed = call("readonly_minimized", launcher, "--read-only")
        check("readonly_never_restores_or_mutates", observed["receipt"].get("status") == "observed"
              and observed["receipt"].get("input_dispatched") is False and observed["state"] == before, observed)

        for family in ("Hybrid", "DRAM"):
            setup()
            fixture.command("minimize")
            outcome = call("run_" + family, launcher, "--set", "Family=" + family)
            state, receipt, metrics = outcome["state"], outcome["receipt"], outcome["metrics"]
            value = cells(book)
            check(family + "_generated_runner_executes_once_and_creates_current_workbook",
                  receipt.get("status") == "completed_unverified" and outcome["exit_code"] == 3
                  and state["opens"] == state["exports"] == 1
                  and all(state["controls"][key]["writes"] == 1 and state["controls"][key]["expands"] == 0
                          and state["controls"][key]["selects"] == 0 for key in ("conditionState", "conditionFamily"))
                  and value.get("A2") == "N" and value.get("B2") == family and value.get("C2") == "1", outcome)
            check(family + "_one_uia_session_no_full_tree_snapshots", metrics["uia_sessions"] == 1
                  and metrics["full_tree_snapshots"] == 0 and metrics["native_findall"] > 0, metrics)

        setup()
        fixture.command("hide|QueryCommand")
        failed = call("missing_query", launcher, "--set", "Family=Hybrid")
        check("failed_query_stops_before_excel", failed["receipt"].get("status") not in ("verified", "completed_unverified")
              and failed["state"]["opens"] == failed["state"]["exports"] == 0, failed)

        for mode in ("uncertain-write", "conflict-editors"):
            setup()
            fixture.command("mode|conditionState|" + mode)
            failed = call(mode, launcher, "--set", "Family=Hybrid")
            state = failed["state"]
            check(mode + "_does_not_retry_or_continue", failed["receipt"].get("status") not in ("verified", "completed_unverified")
                  and state["controls"]["conditionState"]["writes"] == 1
                  and state["controls"]["conditionFamily"]["writes"] == 0
                  and state["opens"] == state["exports"] == 0, failed)

        setup()
        fixture.command("duplicate|conditionState")
        failed = call("duplicate_target", launcher, "--set", "Family=Hybrid")
        check("duplicate_target_stops_before_any_input", failed["receipt"].get("input_dispatched") is False
              and failed["state"]["controls"]["conditionState"]["writes"] == 0, failed)

        # Same final actions and explicit wait, on the same populated provider.
        # Compare repeated direct CLI calls with one generated Python worker.
        timing = dict(direct=[], registered=[])
        bind = ["--exe", exe.name, "--window", fixture.ready["title"]]
        for iteration in range(repeats):
            setup(120)
            elapsed = 0
            for name, command, args in (
                ("state", "select", ["--target", "conditionState", "--value", "N"]),
                ("family", "select", ["--target", "conditionFamily", "--value", "Hybrid"]),
                ("query", "open", ["--target", "QueryCommand"]),
                ("excel", "open", ["--target", "ExportCommand"]),
            ):
                result = call(f"bench_direct_{iteration}_{name}", direct, command, *bind, *args)
                check(f"bench_direct_{iteration}_{name}_delivered", result["receipt"].get("status") in
                      ("verified", "delivered_unverified"), result)
                elapsed += result["metrics"]["seconds"]
                if name == "query":
                    time.sleep(.1)
                    elapsed += .1
            timing["direct"].append(round(elapsed, 6))
            setup(120)
            result = call(f"bench_registered_{iteration}", launcher, "--set", "Family=Hybrid")
            check(f"bench_registered_{iteration}_current_workbook", result["receipt"].get("status") == "completed_unverified"
                  and cells(book).get("B2") == "Hybrid", result)
            timing["registered"].append(result["metrics"]["seconds"])
        setup(120)
        before = fixture.command("state")
        first = call("initial_full_inspect", direct, "inspect", *bind)
        compact = call("initial_discover", helper, "discover", "--exe", exe.name, "--window", fixture.ready["title"])
        mapped = {item.get("automation_id"): item for item in compact["receipt"].get("targets", [])}
        check("first_registration_discovery_uses_form_query_without_full_snapshot_or_values",
              compact["receipt"].get("status") == "observed"
              and mapped.get("conditionState", {}).get("semantic_label") == "MES Setup"
              and mapped.get("conditionFamily", {}).get("semantic_label") == "Family"
              and compact["metrics"]["full_tree_snapshots"] == compact["metrics"]["value_reads"] == 0
              and first["metrics"]["full_tree_snapshots"] == 1 and compact["state"] == before, compact)
        timing["first_full_inspect_seconds"] = first["metrics"]["seconds"]
        timing["first_compact_discover_seconds"] = compact["metrics"]["seconds"]
        timing["discovery_note"] = "Single observations with different output scope; not a general speed guarantee."
        if repeats:
            timing["median_direct_seconds"] = statistics.median(timing["direct"])
            timing["median_registered_seconds"] = statistics.median(timing["registered"])
        write_json(work / "timing.json", timing)
    finally:
        fixture.close()
        summary = dict(passed=sum(c["passed"] for c in checks), total=len(checks), checks=checks,
                       company_fx_tested=False, original_040_base=True)
        write_json(work / "summary.json", summary)
        write_json(work / "results.json", results)
        print(json.dumps({k: v for k, v in summary.items() if k != "checks"}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=ROOT / "skills/desktop-select")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if os.name != "nt" or not args.work_dir.is_absolute() or args.work_dir.exists() or not 0 <= args.repeats <= 10:
        parser.error("Requires Windows, a new absolute work directory and 0..10 timing repeats")
    args.work_dir.mkdir(parents=True)
    exercise(args.skill_dir.resolve(), args.work_dir, args.repeats)


if __name__ == "__main__":
    main()
