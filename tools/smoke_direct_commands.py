"""Test ordinary select/open commands without registering a workflow.

The fixture is synthetic Windows UIA. A label wrapper has one caption child,
is a sibling of its combo, and supplies no LabeledBy relationship. Baseline
results are observations, never assertions that a blocked command works.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from smoke_native import Fixture
from smoke_provider_compatibility import compile_fixture
from smoke_registered_workflow import PROBE, install_skill, write_json


ROOT = Path(__file__).resolve().parents[1]
DIRECT_PROBE = PROBE + r'''
import live_action
counts.update(option_inventories=0, native_findall_calls=0)
original_options = live_action._option_rows
def options(*args, **kwargs):
    counts['option_inventories'] += 1
    yield from original_options(*args, **kwargs)
live_action._option_rows = options
original_element_call = app.bridge.Element.call
def element_call(self, slot, *args, **kwargs):
    if slot == 6:
        counts['native_findall_calls'] += 1
    return original_element_call(self, slot, *args, **kwargs)
app.bridge.Element.call = element_call
'''


def exercise(source, baseline, work, baseline_only=False, skip_baseline=False):
    probe = work / "probe"
    probe.mkdir()
    (probe / "sitecustomize.py").write_text(DIRECT_PROBE, encoding="utf8")
    installed = work / "skills" / "desktop-select"
    if not baseline_only:
        install_skill(source, installed)
    exe = compile_fixture(work)
    fixture = Fixture(exe)
    results, checks = [], []
    sequence = 0

    def check(name, condition, detail=None):
        print(json.dumps({"case": name, "passed": bool(condition)}), flush=True)
        if not condition:
            raise AssertionError((name, detail))
        checks.append(name)

    def setup(wrapped=False):
        fixture.command("clear-extra")
        fixture.command("reset")
        fixture.command("label-wrappers" if wrapped else "label-direct")
        assert fixture.command("query-caption|조회")["query_caption"] == "조회", "fixture_encoding_mismatch"
        for key in ("conditionState", "conditionFamily"):
            fixture.command("mode|" + key + "|no-options-blank-before")

    def cli(variant, name, command, *extra):
        nonlocal sequence
        sequence += 1
        skill = baseline if variant == "baseline" else installed
        stem = f"{sequence:02d}-{variant}-{name}"
        metrics_path = work / (stem + "-processes.jsonl")
        env = dict(os.environ, PYTHONPATH=str(probe), PYTHONDONTWRITEBYTECODE="1",
                   DS_SMOKE_SCRIPTS=str(skill / "scripts"), DS_SMOKE_METRICS=str(metrics_path),
                   DS_SMOKE_FORBID_OBSERVE="0",
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
        rows = [json.loads(line) for line in metrics_path.read_text(encoding="utf8").splitlines()]
        metric = {"seconds": round(elapsed, 6), "processes": len(rows)}
        for key in ("uia_sessions", "full_tree_snapshots", "option_inventories", "native_findall_calls",
                    "element_properties", "child_enumerations", "value_reads"):
            metric[key] = sum(row[key] for row in rows)
        result = {"variant": variant, "name": name, "receipt": receipt, "metrics": metric,
                  "state": fixture.command("state"), "provider": fixture.command("metrics"),
                  "exit_code": proc.returncode, "stderr": proc.stderr}
        results.append(result)
        write_json(work / (stem + ".json"), result)
        print(json.dumps({"variant": variant, "name": name, "status": receipt.get("status"),
                          "diagnostic": receipt.get("diagnostic"), "metrics": metric}), flush=True)
        return result

    try:
        variants = ("baseline",) if baseline_only else ("current",) if skip_baseline else ("baseline", "current")
        for variant in variants:
            for wrapped in (False, True):
                label = "wrapped" if wrapped else "direct"
                scenarios = (
                    ("select_label", "select", ["--target", "MES Setup", "--value", "N"], "conditionState", "N"),
                    ("select_id", "select", ["--target", "conditionState", "--value", "N"], "conditionState", "N"),
                    ("family_label", "select", ["--target", "Family", "--value", "Hybrid"], "conditionFamily", "Hybrid"),
                    ("query_name", "open", ["--target", "조회"], None, None),
                    ("query_id", "open", ["--target", "QueryCommand"], None, None),
                )
                for name, command, args, key, value in scenarios:
                    setup(wrapped)
                    result = cli(variant, label + "_" + name, command, *args)
                    receipt, state, metric = result["receipt"], result["state"], result["metrics"]
                    if variant == "current":
                        if key:
                            check(label + "_" + name + "_writes_once_and_reads_back", receipt.get("status") == "verified"
                                  and state["controls"][key]["value"] == value and state["controls"][key]["writes"] == 1
                                  and state["controls"][key]["expands"] == state["controls"][key]["selects"] == 0,
                                  result)
                            check(label + "_" + name + "_keeps_one_snapshot_and_no_option_scan",
                                  metric["full_tree_snapshots"] == 1 and metric["option_inventories"] == 0, result)
                        else:
                            check(label + "_" + name + "_invokes_once", receipt.get("status") == "delivered_unverified"
                                  and receipt.get("input_dispatched") is True and state["opens"] == 1
                                  and metric["full_tree_snapshots"] == 1, result)
                setup(wrapped)
                result = cli(variant, label + "_discover", "discover")
                if variant == "current" and not wrapped:
                    targets = {item.get("automation_id"): item for item in result["receipt"].get("targets", [])}
                    check(label + "_discover_maps_display_labels", result["receipt"].get("status") == "observed"
                          and targets.get("conditionState", {}).get("semantic_label") == "MES Setup"
                          and targets.get("conditionFamily", {}).get("semantic_label") == "Family"
                          and result["metrics"]["value_reads"] == 0 and result["state"]["opens"] == 0, result)

            if variant == "current":
                for name, command in (("duplicate_id", "duplicate|conditionState"), ("missing_id", "hide|conditionState")):
                    setup(True)
                    fixture.command(command)
                    result = cli(variant, name, "select", "--target", "conditionState", "--value", "N")
                    check(name + "_blocks_without_mutation", result["receipt"].get("status") == "blocked"
                          and result["receipt"].get("input_dispatched") is False
                          and result["state"]["controls"]["conditionState"]["writes"] == 0, result)
                setup(True)
                fixture.command("mode|conditionState|uncertain-write")
                result = cli(variant, "uncertain_write", "select", "--target", "MES Setup", "--value", "N")
                check("uncertain_direct_write_is_never_retried", result["receipt"].get("status") == "uncertain"
                      and result["receipt"].get("input_dispatched") is None
                      and result["state"]["controls"]["conditionState"]["writes"] == 1, result)
        if not baseline_only:
            check("direct_commands_require_no_registered_task", not (work / "unused-task-directory").exists())
        summary = {"passed": len(checks), "total": len(checks), "checks": checks, "baseline_only": baseline_only,
                   "observations": len(results),
                   "company_fx_tested": False, "registered_workflow_used": False,
                   "baseline_observations": [{"name": r["name"], "status": r["receipt"].get("status"),
                       "diagnostic": r["receipt"].get("diagnostic"), "metrics": r["metrics"]}
                       for r in results if r["variant"] == "baseline"],
                   "note": "Baseline observations report success or failure as measured; no blocked case counts as functional success."}
        write_json(work / "summary.json", summary)
        write_json(work / "results.json", results)
        print(json.dumps({"passed": len(checks), "total": len(checks), "baseline_only": baseline_only,
                          "company_fx_tested": False}), flush=True)
    finally:
        fixture.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=ROOT / "skills/desktop-select")
    parser.add_argument("--baseline-skill", type=Path, default=ROOT / ".build/desktop-select-060-accept-r2/desktop-select")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--skip-baseline", action="store_true", help="Only verify the updated or final package")
    args = parser.parse_args()
    if (os.name != "nt" or not args.work_dir.is_absolute() or args.work_dir.exists()
            or args.baseline_only and args.skip_baseline):
        parser.error("Requires Windows and a new absolute work directory")
    args.work_dir.mkdir(parents=True)
    exercise(args.skill_dir.resolve(), args.baseline_skill.resolve(), args.work_dir, args.baseline_only, args.skip_baseline)


if __name__ == "__main__":
    main()
