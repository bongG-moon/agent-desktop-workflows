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
    from live_action import combo_readback
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
                    if node.get("role") == "combobox":
                        out[-1].update(combo_readback(binding, uia, element))
                        if element.property(30037) is True:
                            selected = element.selected_items()
                            out[-1]["selected_values"] = None if selected is None else [
                                uia._selected_option_observation(item).get("value") for item in selected]
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
              and actual["selects"] == actual["expands"] == 0, (reply, actual))
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

        # The model's typed plan is supplied only on the first run. Reuse must
        # retain a variable binding, not the first run's concrete selected value.
        from intent_tasks import make_step, plan_digest
        fixture.command("reset")
        learned = make_step({"anchor": {"role": "combobox", "automation_id": "conditionState"},
                             "label": "MES Setup"}, "실행할 때 입력받은 {Status} 값을 이 항목에 설정해 줘")
        intent_plan = tasks.from_recording("Confirmed variable replay", exe.name, fixture.ready["title"], [learned])
        tasks.save_task(intent_plan, work / "tasks")
        resolution = {"schema": 1, "plan_digest": plan_digest(intent_plan), "steps": [{"number": 1,
            "actions": [{"action": "select", "role": "combobox", "match": "automation_id",
                         "target": "conditionState", "parameter": "Status"}]}]}
        resolution_path = work / "first-run-resolution.json"
        resolution_path.write_text(json.dumps(resolution, indent=2), encoding="utf8")
        first = cli("replay", "--task", intent_plan["name"], "--resolution", str(resolution_path), "--set", "Status=N")
        first_state = control()
        second = cli("replay", "--task", intent_plan["name"], "--set", "Status=W")
        second_state = control()
        check("confirmed_plan_reuses_structure_with_new_runtime_value", first.get("status") == "verified"
              and first.get("plan_reuse") == "saved" and first_state["value"] == "N" and first_state["writes"] == 1
              and second.get("status") == "verified" and second.get("resolution_source") == "confirmed"
              and second.get("plan_reuse") == "reused" and second_state["value"] == "W" and second_state["writes"] == 2
              and second_state["expands"] == second_state["selects"] == 0,
              (first, first_state, second, second_state))

        fixture.command("reset")
        fixture.command("mode|conditionState|explicit-offscreen")
        before = state()
        reply = cli("select", "--target", "MES Setup", "--value", "N")
        check("explicit_offscreen_blocks_without_input", reply.get("status") == "blocked"
              and reply.get("input_dispatched") is False and state() == before, (reply, state()))

        fixture.command("reset")
        fixture.command("mode|conditionState|conflict-editors")
        reply = cli("select", "--target", "MES Setup", "--value", "N")
        after = control()
        check("conflicting_readback_never_claims_success_or_repeats_write",
              reply.get("status") == "delivered_unverified" and reply.get("input_dispatched") is True
              and after["writes"] == 1 and after["selects"] == after["expands"] == 0,
              (reply, after))

        # A requested literal value is a valid SetValue input even when the
        # provider's optional inventory/Expand/Select implementations are bad.
        # Do not first invoke one of those methods and then retry after failure.
        for mode in ("unsupported-option", "uncertain-expand", "uncertain-select"):
            fixture.command("reset")
            fixture.command("mode|conditionState|" + mode)
            reply = cli("select", "--target", "MES Setup", "--value", "N")
            after = control()
            check(mode.replace("-", "_") + "_uses_one_verified_write_without_expand_or_select",
                  reply.get("status") == "verified" and reply.get("method") == "SetValue"
                  and after["writes"] == 1 and after["selects"] == after["expands"] == 0
                  and after["value"] == "N", (reply, after))

        fixture.command("reset")
        fixture.command("mode|conditionState|no-options-blank-before")
        hidden = snapshot()
        hidden_fields = [n for n in hidden if n.get("automation_id") == "conditionState"
                         or n.get("automation_id", "").startswith("conditionStateEditor")]
        check("empty_prewrite_value_and_no_option_inventory_reproduced", len(hidden_fields) == 3
              and all(n["raw_value"] == "" for n in hidden_fields)
              and next(n for n in hidden_fields if n.get("automation_id") == "conditionState").get("value_evidence", {}).get("status") == "unavailable"
              and not any(n.get("automation_id", "").startswith("conditionStateOption") for n in hidden), hidden)
        reply = cli("select", "--target", "MES Setup", "--value", "N")
        after = control()
        check("explicit_value_is_written_once_then_verified_without_inventory", reply.get("status") == "verified"
              and after["value"] == after["parent_value"] == "N" and after["writes"] == 1
              and after["expands"] == after["selects"] == 0, (reply, after))

        fixture.command("reset")
        fixture.command("mode|conditionState|selected-after-write")
        unselected = snapshot()
        (work / "selection-transition-before.json").write_text(json.dumps(unselected, indent=2), encoding="utf8")
        combo, = [n for n in unselected if n.get("automation_id") == "conditionState"]
        editors = [n for n in unselected if n.get("automation_id", "").startswith("conditionStateEditor")]
        check("real_selection_pattern_starts_empty_with_blank_parent_and_editors", combo.get("selected_values") == []
              and combo["raw_value"] == "" and combo.get("value_evidence", {}).get("status") == "unavailable"
              and len(editors) == 2 and all(n["raw_value"] == "" for n in editors), unselected)
        reply = cli("select", "--target", "MES Setup", "--value", "N")
        selected = snapshot()
        (work / "selection-transition-after.json").write_text(json.dumps(selected, indent=2), encoding="utf8")
        combo, = [n for n in selected if n.get("automation_id") == "conditionState"]
        editors = [n for n in selected if n.get("automation_id", "").startswith("conditionStateEditor")]
        after = control()
        check("one_write_is_confirmed_by_parent_duplicate_editors_and_real_selection", reply.get("status") == "verified"
              and after["writes"] == 1 and after["expands"] == after["selects"] == 0
              and combo["raw_value"] == combo["value"] == "N" and combo.get("selected_values") == ["N"]
              and combo.get("value_evidence", {}).get("source") == "corroborated"
              and len(editors) == 2 and all(n["raw_value"] == "N" for n in editors), (reply, selected))

        fixture.command("reset")
        fixture.command("mode|conditionState|select-only")
        reply = cli("select", "--target", "MES Setup", "--value", "N")
        after = control()
        check("select_only_provider_still_uses_one_select_and_readback", reply.get("status") == "verified"
              and reply.get("method") == "Select" and after["value"] == "N" and after["selects"] == 1
              and after["writes"] == after["expands"] == 0, (reply, after))

        for mode, expected_counts in (("uncertain-write", (1, 0, 0)),
                                      ("uncertain-select-only", (0, 0, 1))):
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
