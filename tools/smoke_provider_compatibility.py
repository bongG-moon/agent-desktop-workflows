"""Exercise incomplete raw UIA providers through the normal Desktop Select CLI.

The private fixture channel only prepares a provider condition, minimizes the
test window, or independently reads model/call counters. Production actions
always use the installed/source CLI. No company application or driver is used.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from smoke_native import Fixture


ROOT = Path(__file__).resolve().parents[1]


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


def exercise(skill, work, exe):
    sys.path.insert(0, str(skill / "scripts"))
    import desktop_select as app
    import learned_tasks as tasks
    app.bridge.set_dpi_awareness()
    fixture = Fixture(exe)
    tool = skill / "scripts/desktop_select.py"
    results = []
    sequence = 0
    env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(work / "tasks"))

    def cli(command, *extra):
        nonlocal sequence
        sequence += 1
        args = [sys.executable, "-X", "utf8", "-B", str(tool), command,
                "--exe", exe.name, "--window", fixture.ready["title"], "--hwnd", str(fixture.ready["hwnd"]), *extra]
        process = subprocess.run(args, capture_output=True, encoding="utf8", timeout=45, env=env, cwd=work)
        try:
            receipt = json.loads(process.stdout)
        except ValueError as error:
            raise AssertionError((args, process.stdout, process.stderr)) from error
        (work / f"{sequence:02d}-{command}.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf8")
        return receipt

    def state():
        return fixture.command("state")

    def check(name, condition, detail=None):
        print(json.dumps({"case": name, "passed": bool(condition)}), flush=True)
        if not condition:
            raise AssertionError((name, detail))
        results.append(name)

    def control():
        return state()["controls"]["conditionState"]

    def snapshot():
        binding = app.bridge.Binding(app.pick_window(app.windows(exe.name), fixture.ready["title"], fixture.ready["hwnd"]))
        with app.bridge.UIA() as uia:
            nodes, elements = app.observe(binding, uia)
            out = []
            for node, element in zip(nodes, elements):
                if node.get("automation_id") == "root" or node.get("automation_id", "").startswith("condition"):
                    out.append(dict(node, raw_offscreen=element.property(30022), raw_value=element.property(30045),
                                    raw_legacy_value=element.property(30093), raw_legacy_supported=element.property(30090),
                                    expanded=element.property(30070), actions=uia.actions(element)))
            return out

    try:
        baseline = state()
        nodes = snapshot()
        (work / "raw-provider-observation.json").write_text(json.dumps(nodes, indent=2), encoding="utf8")
        combo, = [n for n in nodes if n.get("automation_id") == "conditionState"]
        editors = [n for n in nodes if n.get("automation_id", "").startswith("conditionStateEditor")]
        options = [n for n in nodes if n.get("automation_id", "").startswith("conditionStateOption")]
        check("real_missing_offscreen_and_duplicate_editors_reproduced", combo["raw_offscreen"] is None
              and combo["raw_value"] == "" and combo["expanded"] == 0 and len(editors) == 2
              and all(n["raw_value"] == "W" and n["raw_offscreen"] is None for n in editors)
              and len(options) == 4 and all(n["actions"] == ["Select"] and n["bounds"]["width"] == 0
                  and n["raw_offscreen"] is None for n in options), nodes)

        reply = cli("inspect", "--target", "MES Setup", "--role", "combobox")
        check("inspect_resolves_missing_visibility_without_input", reply.get("status") == "resolved"
              and reply.get("input_dispatched") is False and state() == baseline, reply)
        reply = cli("select", "--target", "MES Setup", "--value", "N")
        actual = control()
        check("direct_selection_uses_setvalue_and_correlated_readback", reply.get("status") == "verified"
              and actual["value"] == actual["parent_value"] == "N" and actual["writes"] == 1
              and actual["selects"] == 0 and actual["expands"] <= 1, (reply, actual))
        after = state()
        reply = cli("select", "--target", "MES Setup", "--value", "N")
        check("matching_value_is_not_dispatched_again", reply.get("status") == "verified"
              and reply.get("input_dispatched") is False and state() == after, reply)
        reply = cli("open", "--target", "Search", "--role", "button")
        check("open_invokes_missing_visibility_button_once", reply.get("status") == "delivered_unverified"
              and state()["opens"] == 1, reply)

        fixture.command("reset")
        fixture.command("minimize")
        minimized_nodes = snapshot()
        root, = [n for n in minimized_nodes if n.get("automation_id") == "root"]
        check("minimized_root_really_has_zero_bounds", root["bounds"]["width"] == 0 and state()["minimized"], root)
        before = state()
        reply = cli("inspect", "--target", "MES Setup", "--role", "combobox")
        check("inspect_does_not_restore_minimized_window", reply.get("input_dispatched") is False
              and state() == before, reply)
        reply = cli("select", "--target", "MES Setup", "--value", "N")
        after = state()
        check("select_restores_and_reobserves_before_one_write", reply.get("status") == "verified"
              and after["minimized"] is False and after["controls"]["conditionState"]["writes"] == 1
              and after["controls"]["conditionState"]["value"] == "N", (reply, after))
        fixture.command("minimize")
        reply = cli("open", "--target", "Search", "--role", "button")
        check("open_restores_and_invokes_once", reply.get("status") == "delivered_unverified"
              and not state()["minimized"] and state()["opens"] == 1, reply)

        fixture.command("reset")
        steps = []
        for identity, label, instruction in (("conditionState", "MES Setup", "MES SETUP조건을 N으로 해줘"),
                ("conditionFamily", "Family", "입력받은 변수를 FAMILY조건에 입력해줘")):
            raw = {"action": "teach-target", "field_label": label,
                   "target": {"kind": "element", "locator": {"role": "combobox", "automation_id": identity}},
                   "recognition": {"target": "identified", "action": "requires_instruction", "result": "manual",
                                   "reason": "combo_choice_unproven"}}
            raw["instruction"] = tasks.compile_instruction(raw, instruction)
            steps.append(raw)
        task = tasks.from_recording("Provider compatibility replay", exe.name, fixture.ready["title"], steps)
        tasks.save_task(task, work / "tasks")
        fixture.command("minimize")
        before = state()
        reply = cli("replay", "--task", task["name"])
        check("missing_parameter_precedes_restore_or_any_input", reply.get("status") == "needs_input"
              and reply.get("input_dispatched") is False and state() == before, reply)
        reply = cli("replay", "--task", task["name"], "--set", "Family=Hybrid")
        after = state()
        check("saved_replay_restores_and_sets_fixed_and_variable_values", reply.get("status") == "verified"
              and not after["minimized"] and tuple(after["controls"][key]["value"] for key in
                  ("conditionState", "conditionFamily")) == ("N", "Hybrid")
              and all(after["controls"][key]["writes"] == 1 and after["controls"][key]["selects"] == 0
                  for key in ("conditionState", "conditionFamily")), (reply, after))

        for mode in ("conflict-editors", "explicit-offscreen"):
            fixture.command("reset")
            fixture.command("mode|conditionState|" + mode)
            before = state()
            reply = cli("select", "--target", "MES Setup", "--value", "N")
            check(mode.replace("-", "_") + "_blocks_without_input", reply.get("status") == "blocked"
                  and reply.get("input_dispatched") is False and state() == before, (reply, state()))

        fixture.command("reset")
        fixture.command("mode|conditionState|unsupported-option")
        reply = cli("select", "--target", "MES Setup", "--value", "N")
        after = control()
        check("name_only_virtual_option_never_changes_value", reply.get("status") == "blocked"
              and after["writes"] == after["selects"] == 0 and after["expands"] <= 1
              and after["value"] == "W", (reply, after))

        for mode, expected_counts in (("uncertain-expand", (0, 1, 0)),
                                      ("uncertain-write", (1, 1, 0)),
                                      ("uncertain-select", (0, 0, 1))):
            fixture.command("reset")
            fixture.command("mode|conditionState|" + mode)
            reply = cli("select", "--target", "MES Setup", "--value", "N")
            after = control()
            check(mode.replace("-", "_") + "_is_never_retried", reply.get("status") == "uncertain"
                  and reply.get("input_dispatched") is None
                  and tuple(after[key] for key in ("writes", "expands", "selects")) == expected_counts,
                  (reply, after))

        fixture.command("reset")
        fixture.command("mode|conditionState|legacy-option")
        reply = cli("select", "--target", "MES Setup", "--value", "N")
        check("legacy_provider_value_proves_virtual_option", reply.get("status") == "verified"
              and control()["value"] == "N" and control()["writes"] == 1 and control()["selects"] == 0,
              (reply, control()))
        fixture.command("reset")
        fixture.command("mode|conditionState|legacy-all")
        legacy = snapshot()
        legacy_editors = [n for n in legacy if n.get("automation_id", "").startswith("conditionStateEditor")]
        check("actual_legacy_pattern_exposes_duplicate_editor_values", len(legacy_editors) == 2
              and all(n["raw_value"] == "" and n["raw_legacy_supported"] is True
                      and n["raw_legacy_value"] == "W" for n in legacy_editors), legacy)
        reply = cli("select", "--target", "MES Setup", "--value", "N")
        check("legacy_parent_and_editors_confirm_written_value", reply.get("status") == "verified"
              and control()["value"] == "N" and control()["writes"] == 1 and control()["selects"] == 0,
              (reply, control()))
    finally:
        fixture.close()
    summary = {"passed": len(results), "total": len(results), "company_fx_tested": False}
    (work / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf8")
    print(json.dumps(summary), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=ROOT / "skills/desktop-select")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--compile-only", action="store_true")
    args = parser.parse_args()
    if os.name != "nt" or not args.work_dir.is_absolute() or args.work_dir.exists():
        parser.error("This Windows test requires a new absolute work directory.")
    args.work_dir.mkdir(parents=True)
    exe = compile_fixture(args.work_dir)
    if not args.compile_only:
        exercise(args.skill_dir.resolve(), args.work_dir, exe)


if __name__ == "__main__":
    main()
