"""Real Windows acceptance of location-first capture and model-resolved intent.

Tk controls are operated as a synthetic user; native UIA and input are not mocked.
The resolution JSON is an explicitly synthetic internal-model response, so this
test establishes the typed execution contract, not natural-language model quality.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time
import traceback

from smoke_native import Fixture
from smoke_provider_compatibility import compile_fixture


ROOT = Path(__file__).resolve().parents[1]
TASK = "Location first intent acceptance"
INSTRUCTIONS = (
    "MES SETUP은 항상 N으로 설정하세요.\n이 조건만 바꿔 주세요.",
    "실행할 때 입력받은 {Family}를 FAMILY 조건으로 선택하세요.\n녹화 당시 보인 값은 사용하지 마세요.",
    "실수로 추가한 단계입니다.",
    "조회 버튼을 한 번 눌러 주세요.",
)
EDITED_QUERY = "설정한 조건으로 조회 버튼을 한 번 실행하세요.\n버튼 전달 여부와 업무 완료 여부를 구분해 주세요."


def gui_driver(tool, folder, mode, argv):
    """Operate the actual record/edit GUI; only automate this fixture's choices."""
    import tkinter as tk
    original = tk.Misc.mainloop

    def loop(root, *args):
        stage = "start" if mode == "record" else "edit-start"
        annotated = 0

        def walk(parent):
            for child in parent.winfo_children():
                yield child
                yield from walk(child)

        def widget(name):
            return next(w for w in walk(root) if w.winfo_name() == name)

        def button(caption):
            return next(w for w in walk(root) if w.winfo_class() in ("Button", "TButton")
                        and str(w.cget("text")) == caption)

        def select(index):
            tree = widget("steps")
            iid = tree.get_children()[index]
            tree.selection_set(iid)
            tree.focus(iid)
            tree.event_generate("<<TreeviewSelect>>")
            root.update_idletasks()

        def enter(name, text):
            entry = widget(name)
            first = "1.0" if entry.winfo_class() == "Text" else 0
            entry.delete(first, "end")
            entry.insert(first, text)

        def text():
            entry = widget("instruction")
            return entry.get("1.0", "end-1c") if entry.winfo_class() == "Text" else entry.get()

        def tick():
            nonlocal stage, annotated
            try:
                if stage == "start":
                    start = next(w for w in walk(root) if w.winfo_class() in ("Button", "TButton")
                                 and str(w.cget("text")).startswith("시작"))
                    start.invoke()
                    root.iconify()
                    (folder / "ready").touch()
                    stage = "capturing"
                elif stage == "capturing" and (folder / "finish-capture").exists():
                    button("마치고 검토").invoke()
                    stage = "annotate"
                elif stage == "annotate":
                    tree = widget("steps")
                    if len(tree.get_children()) != 4:
                        raise AssertionError(("expected_four_real_anchors", len(tree.get_children())))
                    select(annotated)
                    enter("alias", ("MES 조건", "Family 조건", "잘못된 단계", "조회")[annotated])
                    enter("instruction", INSTRUCTIONS[annotated])
                    enter("parameters", "Family" if annotated == 1 else "")
                    button("적용 후 다음").invoke()
                    annotated += 1
                    if annotated == 4:
                        button("전체 저장").invoke()
                        return
                elif stage == "edit-start":
                    select(1)
                    assert text() == INSTRUCTIONS[1], text()
                    (folder / "position-family").touch()
                    stage = "wait-position"
                elif stage == "wait-position" and (folder / "family-positioned").exists():
                    button("대상 다시 지정 (3초)").invoke()
                    root.iconify()
                    stage = "replacement"
                    root.after(3800, finish_replacement)
                    return
                root.after(100, tick)
            except BaseException:
                (folder / "gui-error.txt").write_text(traceback.format_exc(), encoding="utf8")
                root.destroy()

        def finish_replacement():
            try:
                # Wait for the actual sample worker result, not a guessed UIA latency.
                labels = []
                for w in walk(root):
                    if w.winfo_class() in ("Label", "TLabel"):
                        variable = w.cget("textvariable")
                        labels.append(str(w.getvar(variable) if variable else w.cget("text")))
                if any(value.startswith("3초 안에") for value in labels):
                    root.after(100, finish_replacement)
                    return
                assert any(value.startswith("대상을 지정했습니다.") for value in labels), labels
                assert text() == INSTRUCTIONS[1], ("replacement_lost_text", text())
                button("다음").invoke()
                assert text() == INSTRUCTIONS[2], ("next_failed", text())
                button("단계 삭제").invoke()
                assert len(widget("steps").get_children()) == 3
                select(2)
                assert text() == INSTRUCTIONS[3], ("delete_removed_wrong_stage", text())
                enter("instruction", EDITED_QUERY)
                button("적용 후 다음").invoke()
                button("이전").invoke()
                assert text() == INSTRUCTIONS[1], ("previous_lost_family_text", text())
                button("전체 저장").invoke()
            except BaseException:
                (folder / "gui-error.txt").write_text(traceback.format_exc(), encoding="utf8")
                root.destroy()

        root.after(250, tick)
        root.after(30000, lambda: root.destroy() if root.winfo_exists() else None)
        return original(root, *args)

    tk.Misc.mainloop = loop
    sys.path.insert(0, str(tool.parent))
    sys.argv = [str(tool), *argv]
    runpy.run_path(str(tool), run_name="__main__")


def exercise(skill, work, expected_version):
    sys.path.insert(0, str(skill / "scripts"))
    import desktop_select as app
    import intent_tasks
    import learned_tasks as tasks
    import row_action
    from provider_visibility import visible

    app.bridge.set_dpi_awareness()
    exe = compile_fixture(work)
    fixture = Fixture(exe)
    tool = skill / "scripts/desktop_select.py"
    env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(work / "tasks"))
    bind_args = ["--exe", exe.name, "--window", fixture.ready["title"], "--hwnd", str(fixture.ready["hwnd"])]
    binding = app.bridge.Binding(app.pick_window(app.windows(exe.name), fixture.ready["title"], fixture.ready["hwnd"]))
    results, receipts = [], []
    gui = None

    def check(name, condition, detail=None):
        print(json.dumps({"case": name, "passed": bool(condition)}), flush=True)
        if not condition:
            raise AssertionError((name, detail))
        results.append(name)

    def cli(command, *args):
        proc = subprocess.run([sys.executable, "-X", "utf8", "-B", str(tool), command, *args],
                              capture_output=True, encoding="utf8", timeout=120, cwd=work, env=env)
        try:
            result = json.loads(proc.stdout)
        except ValueError as error:
            raise AssertionError((command, proc.stdout, proc.stderr)) from error
        receipts.append(result)
        (work / f"receipt-{len(receipts):02d}-{command}.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf8")
        return result

    def state():
        return fixture.command("state")

    def wait_file(path, seconds=20):
        deadline = time.monotonic() + seconds
        while not path.exists():
            if gui.poll() is not None:
                raise AssertionError(("gui_exited", gui.communicate()))
            if (path.parent / "gui-error.txt").exists():
                raise AssertionError((path.parent / "gui-error.txt").read_text(encoding="utf8"))
            if time.monotonic() > deadline:
                raise AssertionError(("gui_timeout", path))
            time.sleep(.08)

    def point(identity, click=False):
        row_action.foreground(binding)
        with app.bridge.UIA() as uia:
            nodes, elements = app.observe(binding, uia)
            index, = [i for i, node in enumerate(nodes) if node.get("automation_id") == identity]
            node, element = nodes[index], elements[index]
            assert visible(binding, uia, element) and element.property(30010) is True
            box = uia.bounds(element)
            x, y = round(box["x"]+box["width"]/2), round(box["y"]+box["height"]/2)
            assert binding.contains_window(app.bridge.window_from_point(x, y))
            assert app.bridge.user32.SetCursorPos(x, y)
            time.sleep(.55)
            actual = uia.from_point(x, y)
            for _ in range(12):
                assert actual is not None and uia.pid(actual) == binding.pid
                if uia.same(actual, element):
                    break
                actual = uia.parent(actual)
            else:
                raise AssertionError("point_did_not_hit_fixture_control")
            if click:
                assert binding.contains_window(app.bridge.window_from_point(x, y))
                assert app.bridge._send_point(x, y, "click")["input_dispatched"] is True
                time.sleep(.55)
            return node, element.property(30045)

    def gui_process(mode, folder):
        folder.mkdir()
        args = [mode, *bind_args, "--task", TASK]
        return subprocess.Popen([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                                 "_gui", str(tool), str(folder), mode, *args],
                                cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf8")

    try:
        doctor = cli("doctor")
        check("installed_version_identifies_exact_package", doctor.get("version") == expected_version
              and Path(doctor.get("skill_path", "")).resolve() == skill.resolve()
              and doctor.get("runtime_integrity") == "matched"
              and {"position_recording", "free_text_instructions", "edit_delete_steps", "model_resolution"}
                  <= set(doctor.get("capabilities", [])), doctor)
        selected = cli("select", *bind_args, "--target", "Family", "--value", "Hybrid")
        assert selected["status"] == "verified"
        node, value = point("conditionState")
        check("mes_parent_value_is_empty_before_location_capture", value == "", (node, value))
        initial = state()
        folder = work / "record-gui"
        gui = gui_process("record", folder)
        wait_file(folder / "ready")
        for identity in ("conditionState", "QueryCommand", "conditionFamily", "QueryCommand"):
            point(identity, click=True)
        (folder / "finish-capture").touch()
        out, err = gui.communicate(timeout=35)
        if (folder / "gui-error.txt").exists():
            raise AssertionError((folder / "gui-error.txt").read_text(encoding="utf8"))
        reply = json.loads(out)
        check("real_record_cli_saved_four_location_intents", gui.returncode == 0
              and reply["status"] == "recorded", (reply, err))
        original_plan = tasks.load_task(TASK, work / "tasks")
        check("location_capture_adds_no_value_or_invoke_pattern_calls", state() == initial, state())
        check("recorded_value_is_not_a_fixed_intent", len(original_plan["steps"]) == 4
              and all(s["action"] == "intent" for s in original_plan["steps"])
              and "Hybrid" not in json.dumps(original_plan)
              and original_plan["steps"][1]["instruction"]["parameters"] == ["Family"], original_plan)
        check("free_multiline_text_survives_record_save", [s["instruction"]["text"] for s in original_plan["steps"]]
              == list(INSTRUCTIONS), original_plan)

        folder = work / "edit-gui"
        gui = gui_process("edit", folder)
        wait_file(folder / "position-family")
        point("conditionFamily")
        (folder / "family-positioned").touch()
        out, err = gui.communicate(timeout=30)
        if (folder / "gui-error.txt").exists():
            raise AssertionError((folder / "gui-error.txt").read_text(encoding="utf8"))
        reply = json.loads(out)
        check("edit_cli_reopens_and_saves", gui.returncode == 0, (reply, err))
        plan = tasks.load_task(TASK, work / "tasks")
        check("replace_target_preserves_written_action_and_variable", plan["steps"][1]["anchor"]["automation_id"]
              == "conditionFamily" and plan["steps"][1]["instruction"] == original_plan["steps"][1]["instruction"], plan)
        check("delete_wrong_stage_and_edit_query_persist", len(plan["steps"]) == 3
              and plan["steps"][2]["instruction"]["text"] == EDITED_QUERY, plan)
        backup_path = Path(reply.get("backup", ""))
        check("saved_edit_preserves_original_plan_backup", backup_path.is_file()
              and json.loads(backup_path.read_text(encoding="utf8")) == original_plan, reply)

        resolution = {"schema": 1, "plan_digest": intent_tasks.plan_digest(plan), "steps": [
            {"number": 1, "actions": [{"action": "select", "role": "combobox", "target": "conditionState",
                                      "match": "automation_id", "value": "N"}]},
            {"number": 2, "actions": [{"action": "select", "role": "combobox", "target": "conditionFamily",
                                      "match": "automation_id", "parameter": "Family"}]},
            {"number": 3, "actions": [{"action": "open", "role": "button", "target": "QueryCommand",
                                      "match": "automation_id", "completion": "delivery"}]},
        ]}
        resolution_path = work / "synthetic-internal-model-resolution.json"
        resolution_path.write_text(json.dumps(resolution, indent=2), encoding="utf8")
        before = state()
        reply = cli("replay", *bind_args, "--task", TASK, "--set", "Family=DRAM")
        check("free_text_requests_model_interpretation_without_execution", reply.get("status") == "needs_interpretation"
              and reply.get("input_dispatched") is False and reply.get("plan_digest") == intent_tasks.plan_digest(plan)
              and state() == before, reply)
        old = copy.deepcopy(resolution)
        old["plan_digest"] = intent_tasks.plan_digest(original_plan)
        old_path = work / "stale-resolution.json"
        old_path.write_text(json.dumps(old), encoding="utf8")
        reply = cli("replay", *bind_args, "--task", TASK, "--resolution", str(old_path), "--set", "Family=DRAM")
        check("edited_plan_rejects_previous_resolution_without_input", reply.get("diagnostic") == "resolution_plan_changed"
              and reply.get("input_dispatched") is False and state() == before, reply)
        guessed = copy.deepcopy(resolution)
        guessed["steps"][1]["actions"][0].pop("parameter")
        guessed["steps"][1]["actions"][0]["value"] = "Hybrid"
        guessed_path = work / "invalid-fixed-family-resolution.json"
        guessed_path.write_text(json.dumps(guessed), encoding="utf8")
        reply = cli("replay", *bind_args, "--task", TASK, "--resolution", str(guessed_path), "--set", "Family=DRAM")
        check("model_cannot_replace_runtime_variable_with_recorded_choice", reply.get("diagnostic") == "resolution_parameter_not_used"
              and reply.get("input_dispatched") is False and state() == before, reply)
        reply = cli("replay", *bind_args, "--task", TASK, "--resolution", str(resolution_path))
        check("required_variable_blocks_all_inputs", reply.get("status") == "needs_input"
              and reply.get("input_dispatched") is False and state() == before, reply)
        reply = cli("replay", *bind_args, "--task", TASK, "--resolution", str(resolution_path), "--set", "Family=DRAM")
        actual = state()
        check("typed_plan_executes_fixed_value_runtime_variable_and_query_once", reply.get("status") == "completed_unverified"
              and actual["controls"]["conditionState"]["value"] == "N"
              and actual["controls"]["conditionFamily"]["value"] == "DRAM"
              and actual["opens"] == 1, (reply, actual))
        check("replay_does_not_rewrite_saved_natural_instructions", tasks.load_task(TASK, work / "tasks") == plan)
    finally:
        if gui is not None and gui.poll() is None:
            gui.kill()
            gui.communicate(timeout=3)
        fixture.close()
    exercise_export(skill, work / "export", results)
    summary = {"passed": len(results), "total": len(results), "model_resolution": "synthetic",
               "company_fx_tested": False}
    (work / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf8")
    print(json.dumps(summary), flush=True)


def exercise_export(skill, work, results):
    """Resolve free instructions to the real query/context/export path."""
    import desktop_select as app
    import context_menu
    import intent_capture
    import intent_tasks
    import learned_tasks as tasks
    import row_action
    from context_teach import raw_result_step
    from smoke_desktop_context import compile_fixture as compile_context, decoded_cells

    work.mkdir()
    exe = compile_context(work)
    book = work / "report.xlsx"
    fixture = Fixture(exe, str(book), ready_timeout=25)
    try:
        binding = app.bridge.Binding(app.pick_window(app.windows(exe.name), fixture.ready["title"], fixture.ready["hwnd"]))
        row_action.foreground(binding)
        with app.bridge.UIA() as uia:
            nodes, elements = app.observe(binding, uia)
            def identity(key):
                n, = [n for n in nodes if n.get("automation_id") == key]
                return {"role": n["role"], "automation_id": key}
            def location(key):
                index, = [i for i, n in enumerate(nodes) if n.get("automation_id") == key]
                box = uia.bounds(elements[index])
                return (round(box["x"]+box["width"]/2), round(box["y"]+box["height"]/2))
            locations = {key: location(key) for key in ("conditionState", "conditionFamily", "ResultSurface")}
            trigger, marker = identity("QueryCommand"), identity("ResultIndicator")
        steps = [intent_tasks.make_step(intent_capture.pick_anchor(binding, locations[key]), text,
                                       parameters=["Family"] if key == "conditionFamily" else [])
                 for key, text in (("conditionState", INSTRUCTIONS[0]), ("conditionFamily", INSTRUCTIONS[1]),
                                   ("ResultSurface", "조회가 끝나면 이 결과 표를 우클릭해서 EXPORT 다음 EXPORT TO EXCEL을 실행하세요.\n생성된 파일까지 확인하세요."))]
        menu = context_menu.teach_at(binding, locations["ResultSurface"], ["EXPORT", "EXPORT TO EXCEL"],
                                     target="ResultSurface", role="table")
        raw = raw_result_step("조회 후 결과 내보내기", menu, trigger,
                              {"kind": "text", "target": marker, "expected": "RESULT READY"},
                              {"kind": "file", "path": str(book)})
        typed = tasks.recorded_step(raw)
        task = tasks.from_recording("Free intent Excel acceptance", exe.name, fixture.ready["title"], steps)
        tasks.save_task(task, work / "tasks")
        resolution = {"schema": 1, "plan_digest": intent_tasks.plan_digest(task), "steps": [
            {"number": 1, "actions": [{"action": "select", "role": "combobox", "target": "conditionState",
                                      "match": "automation_id", "value": "N"}]},
            {"number": 2, "actions": [{"action": "select", "role": "combobox", "target": "conditionFamily",
                                      "match": "automation_id", "parameter": "Family"}]},
            {"number": 3, "actions": [typed]},
        ]}
        path = work / "synthetic-internal-model-resolution.json"
        path.write_text(json.dumps(resolution, ensure_ascii=False, indent=2), encoding="utf8")
        args = [sys.executable, "-X", "utf8", "-B", str(skill / "scripts/desktop_select.py"), "replay",
                "--exe", exe.name, "--window", fixture.ready["title"], "--hwnd", str(fixture.ready["hwnd"]),
                "--task", task["name"], "--resolution", str(path), "--set", "Family=DRAM"]
        proc = subprocess.run(args, capture_output=True, encoding="utf8", timeout=120,
                              env=dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(work / "tasks")))
        run = json.loads(proc.stdout)
        (work / "receipt.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf8")
        actual = fixture.command("state")
        def check(name, condition):
            print(json.dumps({"case": name, "passed": bool(condition)}), flush=True)
            if not condition:
                raise AssertionError((name, run, actual))
            results.append(name)
        check("resolved_free_intents_complete_query_context_excel", run.get("status") == "verified"
              and (actual["status"], actual["family"], actual["result_status"], actual["result_family"])
                  == ("N", "DRAM", "N", "DRAM"))
        check("resolved_export_query_rightclick_leaf_each_once", tuple(actual[key] for key in (
            "queries", "generation", "right_clicks", "menus", "parents", "leaves", "wrong_point", "unrelated"))
              == (1, 1, 1, 1, 1, 1, 0, 0))
        check("resolved_export_workbook_contains_supplied_runtime_value", decoded_cells(book) == {
            "A1": "MES Setup", "B1": "Family", "C1": "Generation", "A2": "N", "B2": "DRAM", "C2": "1"})
    finally:
        fixture.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=ROOT / "skills/desktop-select")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    if os.name != "nt" or not args.work_dir.is_absolute() or args.work_dir.exists():
        parser.error("This Windows test requires a new absolute work directory")
    args.work_dir.mkdir(parents=True)
    exercise(args.skill_dir.resolve(), args.work_dir, args.expected_version)


if __name__ == "__main__":
    if sys.argv[1:2] == ["_gui"]:
        gui_driver(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[5:])
    else:
        main()
