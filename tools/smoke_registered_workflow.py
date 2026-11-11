"""Real Windows acceptance and bounded timing for registered ID-based workflows.

No company application is used. The recipe and baseline interpretation are
synthetic. All mutations go through the generated launcher or baseline CLI;
the fixture command channel only sets up conditions and reads independent state.
Provider counters include UIA's internal search, not only client-side calls.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from smoke_native import Fixture
from smoke_provider_compatibility import compile_fixture


ROOT = Path(__file__).resolve().parents[1]
PROBE = r'''
import atexit, json, os, sys, time
sys.path.insert(0, os.environ['DS_SMOKE_SCRIPTS'])
import desktop_select as app
started = time.perf_counter()
counts = dict(pid=os.getpid(), argv=sys.argv, uia_sessions=0, full_tree_snapshots=0,
              element_properties=0, child_enumerations=0, value_reads=0)
def forbidden(*args, **kwargs):
    counts['full_tree_snapshots'] += 1
    raise AssertionError('registered_workflow_called_full_observe')
if os.environ.get('DS_SMOKE_FORBID_OBSERVE') == '1':
    app.observe = forbidden
original_init = app.bridge.UIA.__init__
def init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    counts['uia_sessions'] += 1
app.bridge.UIA.__init__ = init
original_handle = app.bridge.UIA.from_handle
def from_handle(self, *args, **kwargs):
    frame = sys._getframe(1)
    if frame.f_code.co_name == 'observe' and frame.f_code.co_filename.endswith('desktop_select.py'):
        counts['full_tree_snapshots'] += 1
        if os.environ.get('DS_SMOKE_FORBID_OBSERVE') == '1':
            raise AssertionError('registered_workflow_called_full_observe')
    return original_handle(self, *args, **kwargs)
app.bridge.UIA.from_handle = from_handle
original_property = app.bridge.Element.property
def prop(self, *args, **kwargs):
    counts['element_properties'] += 1
    return original_property(self, *args, **kwargs)
app.bridge.Element.property = prop
original_children = app.bridge.UIA.children
def children(self, *args, **kwargs):
    counts['child_enumerations'] += 1
    return original_children(self, *args, **kwargs)
app.bridge.UIA.children = children
original_value = app.bridge.UIA._accessible_value
def value(self, *args, **kwargs):
    counts['value_reads'] += 1
    return original_value(self, *args, **kwargs)
app.bridge.UIA._accessible_value = value
@atexit.register
def save():
    counts['lifetime_seconds'] = round(time.perf_counter() - started, 6)
    with open(os.environ['DS_SMOKE_METRICS'], 'a', encoding='utf8') as out:
        out.write(json.dumps(counts) + '\n')
'''


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")


def cells(path):
    with ZipFile(path) as book:
        assert book.testzip() is None
        data = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
        return {cell.attrib["r"]: cell.findtext(".//{*}t") for cell in data.findall(".//{*}c")}


def install_skill(source, target):
    shutil.copytree(source, target)
    # The checkout shares pinned bridge bytes; a distribution already contains them.
    for name in ("win_bridge.py", "recorder.py"):
        if not (target / "scripts" / name).is_file():
            shutil.copyfile(ROOT / "scripts" / name, target / "scripts" / name)


def profile_for(exe, title, *, wait=.1):
    def target(key, role):
        return {"automation_id": key, "role": role}
    return {"schema": 1, "name": "Registered query and Excel", "exe": exe, "window": title, "steps": [
        {"action": "set-value", "selector": target("conditionState", "combobox"), "value": "N"},
        {"action": "set-value", "selector": target("conditionFamily", "combobox"), "parameter": "Family"},
        {"action": "invoke", "selector": target("QueryCommand", "button")},
        {"action": "wait", "seconds": wait},
        {"action": "invoke", "selector": target("ExportCommand", "button")},
    ]}


def anchor_probe(skill, exe, title, hwnd):
    sys.path.insert(0, str(skill / "scripts"))
    import desktop_select as app
    import native_targets
    from intent_capture import PositionUIA, anchor_from_observation
    from row_action import foreground
    app.bridge.set_dpi_awareness()
    binding = app.bridge.Binding(app.pick_window(app.windows(exe), title, hwnd))
    foreground(binding)
    with PositionUIA(binding) as uia:
        combo = native_targets.resolve(binding, uia, {"automation_id": "conditionState", "role": "combobox"})
        box = uia.bounds(combo)
        point = (round(box["x"] + box["width"] / 2), round(box["y"] + box["height"] / 2))
        observation = uia.observe_point(binding, *point)
        result = anchor_from_observation(binding, uia, observation, point)
        print(json.dumps(dict(result, status="observed", input_dispatched=False,
                              point=point, hit=observation["node"]), ensure_ascii=False))


def exercise(skill, baseline, work, repeats, skip_benchmark=False):
    installed = work / "skills" / "desktop-select"
    install_skill(skill, installed)
    probe = work / "probe"
    probe.mkdir()
    (probe / "sitecustomize.py").write_text(PROBE, encoding="utf8")
    exe = compile_fixture(work)
    fixture = Fixture(exe)
    generated = work / "skills" / "registered-query"
    launcher = generated / "scripts" / "run.py"
    profile = profile_for(exe.name, fixture.ready["title"])
    recipe = work / "recipe.json"
    write_json(recipe, profile)
    book = work / "report.xlsx"
    cases, measurements = [], []
    sequence = 0

    def check(name, ok, detail=None):
        print(json.dumps({"case": name, "passed": bool(ok)}), flush=True)
        if not ok:
            raise AssertionError((name, detail))
        cases.append(name)

    def command(name, path, args, *, source=installed, forbid=True, extra_env=None, count_snapshot_check=True):
        nonlocal sequence
        sequence += 1
        stem = f"{sequence:02d}-{name}"
        metric_path = work / (stem + "-processes.jsonl")
        env = dict(os.environ, PYTHONPATH=str(probe), PYTHONDONTWRITEBYTECODE="1",
                   DS_SMOKE_SCRIPTS=str(source / "scripts"), DS_SMOKE_METRICS=str(metric_path),
                   DS_SMOKE_FORBID_OBSERVE="1" if forbid else "0")
        env.update(extra_env or {})
        fixture.command("reset-metrics")
        started = time.perf_counter()
        proc = subprocess.run([sys.executable, "-X", "utf8", "-B", str(path), *args],
                              cwd=work, env=env, capture_output=True, encoding="utf8", timeout=120)
        elapsed = time.perf_counter() - started
        try:
            receipt = json.loads(proc.stdout)
        except ValueError as error:
            raise AssertionError((stem, proc.returncode, proc.stdout, proc.stderr)) from error
        process_rows = [json.loads(line) for line in metric_path.read_text(encoding="utf8").splitlines()]
        measurement = {"name": name, "elapsed_seconds": round(elapsed, 6), "exit_code": proc.returncode,
                       "processes": len(process_rows), "uia_sessions": sum(r["uia_sessions"] for r in process_rows),
                       "full_tree_snapshots": sum(r["full_tree_snapshots"] for r in process_rows),
                       "client_properties": sum(r["element_properties"] for r in process_rows),
                       "client_child_enumerations": sum(r["child_enumerations"] for r in process_rows),
                       "value_reads": sum(r["value_reads"] for r in process_rows),
                       "provider": fixture.command("metrics")}
        measurements.append(measurement)
        write_json(work / (stem + ".json"), {"receipt": receipt, "measurement": measurement,
                                            "state": fixture.command("state"), "stderr": proc.stderr})
        if forbid and count_snapshot_check:
            check(name + "_uses_no_full_snapshot", measurement["full_tree_snapshots"] == 0, measurement)
        return receipt, measurement

    def run(name, *args):
        return command(name, launcher, list(args))

    def no_input(state):
        return (state["opens"] == state["exports"] == 0
                and all(row["writes"] == row["expands"] == row["selects"] == 0
                        for row in state["controls"].values()))

    def setup(mode="no-options-blank-before"):
        fixture.command("clear-extra")
        fixture.command("reset")
        for key in ("conditionState", "conditionFamily"):
            fixture.command("mode|" + key + "|" + mode)

    try:
        fixture.command("export-path|" + str(book))
        setup()
        bind = ["--exe", exe.name, "--window", fixture.ready["title"], "--hwnd", str(fixture.ready["hwnd"])]
        receipt, native_discovery = command("native_first_discover", installed / "scripts/desktop_select.py",
                                            ["discover", *bind])
        labels = {item.get("automation_id"): item.get("semantic_label") for item in receipt.get("targets", [])}
        check("native_discovery_finds_labels_without_reading_values", receipt.get("status") == "observed"
              and labels.get("conditionState") == "MES Setup" and labels.get("conditionFamily") == "Family"
              and native_discovery["value_reads"] == 0 and no_input(fixture.command("state")), receipt)
        receipt, metric = command("recorded_native_anchor", Path(__file__).resolve(),
            ["_anchor", str(installed), exe.name, fixture.ready["title"], str(fixture.ready["hwnd"])],
            count_snapshot_check=False)
        check("recording_editor_point_resolves_native_combo_anchor", receipt.get("status") == "observed"
              and receipt.get("anchor", {}).get("role") == "combobox"
              and receipt.get("anchor", {}).get("automation_id") == "conditionState"
              and receipt.get("label") == "MES Setup" and metric["full_tree_snapshots"] == 0
              and metric["value_reads"] == 0 and no_input(fixture.command("state")), (receipt, metric))
        receipt, registration = command("register", installed / "scripts/desktop_select.py",
                                        ["register", "--recipe", str(recipe), "--out", str(generated)])
        check("registration_creates_local_skill_without_input", registration["exit_code"] == 0
              and receipt.get("input_dispatched") is False and launcher.is_file()
              and (generated / "SKILL.md").is_file() and (generated / "workflow.json").is_file()
              and no_input(fixture.command("state")), receipt)

        fixture.command("minimize")
        receipt, metric = run("readonly_minimized", "--read-only")
        check("readonly_does_not_restore_or_mutate", receipt.get("status") == "observed"
              and receipt.get("input_dispatched") is False and fixture.command("state")["minimized"]
              and no_input(fixture.command("state")), receipt)
        fixture.command("restore")

        for i, family in enumerate(("Hybrid", "DRAM"), 1):
            receipt, metric = run("generated_run_" + str(i), "--set", "Family=" + family)
            state = fixture.command("state")
            check("generated_run_" + str(i) + "_sets_and_invokes_once", receipt.get("status") == "completed_unverified"
                  and state["controls"]["conditionState"]["value"] == "N"
                  and state["controls"]["conditionFamily"]["value"] == family
                  and state["opens"] == state["exports"] == i
                  and state["controls"]["conditionState"]["writes"] == 1
                  and state["controls"]["conditionFamily"]["writes"] == i
                  and all(v["expands"] == v["selects"] == 0 for v in state["controls"].values()), (receipt, state))
            check("generated_run_" + str(i) + "_has_one_uia_worker", metric["uia_sessions"] == 1
                  and metric["processes"] == 2, metric)
            actual_cells = cells(book)
            check("generated_run_" + str(i) + "_exports_current_values", actual_cells == {
                "A1": "MES Setup", "B1": "Family", "C1": "Generation", "A2": "N", "B2": family, "C2": str(i)}, actual_cells)
            shutil.copyfile(book, work / ("run-" + str(i) + ".xlsx"))

        setup()
        receipt, metric = run("missing_parameter")
        check("missing_parameter_stops_before_input", receipt.get("status") == "needs_input"
              and receipt.get("input_dispatched") is False and no_input(fixture.command("state"))
              and metric["uia_sessions"] == 0, receipt)

        fixture.command("title|Different synthetic view")
        receipt, metric = run("wrong_window", "--set", "Family=Hybrid")
        check("wrong_window_stops_before_input", receipt.get("status") == "blocked"
              and receipt.get("input_dispatched") is False and no_input(fixture.command("state")), receipt)
        fixture.command("title|" + fixture.ready["title"])

        for condition in ("hide|conditionState", "duplicate|conditionState"):
            setup()
            fixture.command(condition)
            name = "missing_id" if condition.startswith("hide") else "duplicate_id"
            receipt, metric = run(name, "--set", "Family=Hybrid")
            check(name + "_never_uses_first_or_nearby_match", receipt.get("status") == "blocked"
                  and receipt.get("input_dispatched") is False and no_input(fixture.command("state")), receipt)

        setup()
        fixture.command("mode|conditionState|uncertain-write")
        receipt, metric = run("uncertain_write", "--set", "Family=Hybrid")
        state = fixture.command("state")
        check("uncertain_write_is_not_repeated_and_buttons_do_not_run", receipt.get("status") == "uncertain"
              and receipt.get("input_dispatched") is None and state["controls"]["conditionState"]["writes"] == 1
              and state["controls"]["conditionFamily"]["writes"] == 0 and state["opens"] == state["exports"] == 0,
              (receipt, state))

        setup()
        fixture.command("mode|conditionState|conflict-editors")
        receipt, metric = run("conflicting_readback", "--set", "Family=Hybrid")
        state = fixture.command("state")
        check("conflicting_readback_stops_following_actions", receipt.get("status") == "delivered_unverified"
              and state["controls"]["conditionState"]["writes"] == 1
              and state["controls"]["conditionFamily"]["writes"] == 0 and state["opens"] == state["exports"] == 0,
              (receipt, state))

        if skip_benchmark:
            summary = {"passed": len(cases), "total": len(cases), "cases": cases, "company_fx_tested": False,
                       "recipe_and_baseline_resolution": "synthetic", "registration": registration,
                       "native_first_discover": native_discovery, "benchmark": "skipped",
                       "process_count_scope": "Includes one supervisor; execution worker and UIA session counts are separately instrumented."}
            write_json(work / "summary.json", summary)
            write_json(work / "measurements.json", measurements)
            print(json.dumps({"passed": len(cases), "total": len(cases), "benchmark": "skipped",
                              "company_fx_tested": False}), flush=True)
            return

        # Keep registration separate from natural-language discovery. The recipe
        # already contains IDs; no timing below claims to measure model quality.
        setup()
        fixture.command("clutter|120")
        _, discovery = command("baseline_first_inspect", baseline / "scripts/desktop_select.py",
                               ["inspect", *bind], source=baseline, forbid=False)
        _, native_discovery_large = command("native_large_discover", installed / "scripts/desktop_select.py",
                                            ["discover", *bind])

        sys.path.insert(0, str(installed / "scripts"))
        import intent_tasks
        import learned_tasks as tasks
        import registered_profile
        task_steps, actions = [], []
        for number, step in enumerate(profile["steps"], 1):
            if step["action"] == "wait":
                continue
            selector = step["selector"]
            note = "Set {Family}" if "parameter" in step else "Run the demonstrated control"
            task_steps.append(intent_tasks.make_step({"anchor": selector, "label": selector["automation_id"]}, note))
            action = {"action": "select" if step["action"] == "set-value" else "open", "role": selector["role"],
                      "target": selector["automation_id"], "match": "automation_id"}
            if "value" in step:
                action["value"] = step["value"]
            if "parameter" in step:
                action["parameter"] = step["parameter"]
            if action["action"] == "open":
                action["completion"] = "delivery"
            actions.append({"number": len(actions) + 1, "actions": [action]})
        plan = tasks.from_recording("Synthetic registered baseline", exe.name, fixture.ready["title"], task_steps)
        tasks.save_task(plan, work / "baseline-tasks")
        resolution = {"schema": 1, "plan_digest": intent_tasks.plan_digest(plan), "steps": actions}
        path = work / "synthetic-baseline-resolution.json"
        write_json(path, resolution)
        benchmark_profile = copy.deepcopy(profile)
        benchmark_profile["steps"] = [s for s in benchmark_profile["steps"] if s["action"] != "wait"]
        benchmark_out = work / "skills" / "registered-benchmark"
        registered_profile.export_skill(benchmark_profile, benchmark_out)
        benchmark_script = benchmark_out / "scripts/run.py"
        benchmark_runs = {"baseline_cached": [], "registered": []}
        # An initial baseline run creates the same confirmed template cache used
        # in 0.5.2. It is reported separately, never folded into repeated timing.
        baseline_args = ["replay", *bind, "--task", plan["name"], "--set", "Family=Hybrid"]
        receipt, _ = command("baseline_first_replay", baseline / "scripts/desktop_select.py",
                             [*baseline_args, "--resolution", str(path)], source=baseline, forbid=False,
                             extra_env={"DESKTOP_SELECT_TASK_DIR": str(work / "baseline-tasks")})
        check("baseline_first_replay_is_comparable", receipt.get("status") == "completed_unverified", receipt)
        for sample in range(repeats):
            family = ("Hybrid", "DRAM")[sample % 2]
            for variant in ("baseline_cached", "registered"):
                fixture.command("reset")
                for key in ("conditionState", "conditionFamily"):
                    fixture.command("mode|" + key + "|no-options-blank-before")
                if variant == "baseline_cached":
                    receipt, metric = command(variant + str(sample), baseline / "scripts/desktop_select.py",
                        ["replay", *bind, "--task", plan["name"], "--set", "Family=" + family], source=baseline,
                        forbid=False, extra_env={"DESKTOP_SELECT_TASK_DIR": str(work / "baseline-tasks")})
                else:
                    receipt, metric = command(variant + str(sample), benchmark_script, ["--set", "Family=" + family])
                state = fixture.command("state")
                check(variant + str(sample) + "_same_result", receipt.get("status") == "completed_unverified"
                      and state["opens"] == state["exports"] == 1 and state["result_state"] == "N"
                      and state["result_family"] == family and cells(book)["B2"] == family, (receipt, state))
                benchmark_runs[variant].append(metric)
        timing = {variant: {"samples": len(rows), "minimum_seconds": min(r["elapsed_seconds"] for r in rows),
                           "median_seconds": statistics.median(r["elapsed_seconds"] for r in rows),
                           "maximum_seconds": max(r["elapsed_seconds"] for r in rows), "runs": rows}
                  for variant, rows in benchmark_runs.items()}
        summary = {"passed": len(cases), "total": len(cases), "cases": cases, "company_fx_tested": False,
                   "recipe_and_baseline_resolution": "synthetic", "registration": registration,
                   "baseline_first_inspect": discovery, "native_first_discover": native_discovery,
                   "native_large_discover": native_discovery_large, "timing": timing,
                   "timing_scope": "Same isolated provider, 120 irrelevant nodes, fresh worker processes, four operations; no explicit query wait in either timed workflow. Registered readback delay remains part of runtime.",
                   "process_count_scope": "Includes one supervisor; execution worker and UIA session counts are separately instrumented."}
        write_json(work / "summary.json", summary)
        write_json(work / "measurements.json", measurements)
        print(json.dumps({"passed": len(cases), "total": len(cases), "timing": {
            name: {k: v for k, v in row.items() if k != "runs"} for name, row in timing.items()},
            "company_fx_tested": False}), flush=True)
    finally:
        fixture.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=ROOT / "skills/desktop-select")
    parser.add_argument("--baseline-skill", type=Path, default=ROOT / ".build/desktop-select-052-accept-r1/desktop-select")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--skip-benchmark", action="store_true", help="Only repeat functional acceptance on the final ZIP")
    args = parser.parse_args()
    if os.name != "nt" or not args.work_dir.is_absolute() or args.work_dir.exists() or not 1 <= args.repeats <= 10:
        parser.error("Requires Windows, a new absolute work directory, and 1 to 10 repeats")
    args.work_dir.mkdir(parents=True)
    exercise(args.skill_dir.resolve(), args.baseline_skill.resolve(), args.work_dir, args.repeats, args.skip_benchmark)


if __name__ == "__main__":
    if sys.argv[1:2] == ["_anchor"]:
        anchor_probe(Path(sys.argv[2]), sys.argv[3], sys.argv[4], int(sys.argv[5]))
    else:
        main()
