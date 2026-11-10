"""Teach a task with real window input, move its fields, and run it through the CLI.

Development-only Windows acceptance test. The test drives the recorder's own
Tk buttons and sends guard-checked mouse input only to the synthetic WPF app.
Observed values and command counters come from that app via stdin.
"""
import argparse
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TASK = "Synthetic condition search"
ALIAS = "패밀리 조건"
INSTRUCTIONS = ("MES SETUP조건을 N으로 해줘", "입력받은 변수를 FAMILY조건에 입력해줘")


def compile_fixture(work):
    framework = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319"
    libraries = [framework / "WPF" / name for name in (
        "PresentationFramework.dll", "PresentationCore.dll", "WindowsBase.dll",
        "UIAutomationTypes.dll", "UIAutomationProvider.dll")]
    libraries += [framework / "System.Xaml.dll", framework / "System.Web.Extensions.dll"]
    exe = work / "DesktopTeachFixture.exe"
    src = ROOT / "tests/fixtures"
    subprocess.run([str(framework / "csc.exe"), "/nologo", "/target:exe", "/main:DesktopTeachFixture",
                    "/out:" + str(exe), *["/r:" + str(lib) for lib in libraries],
                    str(src / "ComboPopupFixture.cs"), str(src / "DesktopTeachFixture.cs")], check=True)
    return exe


def gui_driver(cli, work, argv):
    # This process drives the actual Tk controls. Production argument parsing,
    # hook capture, review handlers and persistence run unchanged.
    import tkinter as tk

    original_loop = tk.Misc.mainloop
    def mainloop(window, *args):
        def find(parent, *, caption=None, name=None):
            for child in parent.winfo_children():
                if child.winfo_name() == name or (caption is not None and
                        child.winfo_class() in ("Button", "TButton") and child.cget("text") == caption):
                    return child
                nested = find(child, caption=caption, name=name)
                if nested is not None:
                    return nested
            return None

        ready = work / "recording-ready"
        stage = "ready"
        edited = 0
        previews = []
        def operate():
            nonlocal stage, edited
            if stage == "ready":
                start = find(window, caption="시작")
                if start is not None and str(start.cget("state")) != "disabled":
                    start.invoke()
                    window.iconify()
                    ready.touch()
                    stage = "recording"
            elif stage == "recording" and (work / "end-demonstration").exists():
                finish = find(window, caption="마치고 검토")
                if finish is not None and str(finish.cget("state")) != "disabled":
                    finish.invoke()
                    stage = "review"
            elif stage == "review":
                tree = find(window, name="steps")
                if tree is not None and len(tree.get_children()) >= 2:
                    step = tree.get_children()[edited]
                    tree.selection_set(step)
                    tree.focus(step)
                    stage = "annotate"
            elif stage == "annotate":
                for field, value in (("alias", ALIAS if edited == 1 else "상태 조건"),
                                     ("instruction", INSTRUCTIONS[edited])):
                    entry = find(window, name=field)
                    entry.delete(0, "end")
                    entry.insert(0, value)
                find(window, caption="동작 적용").invoke()
                preview = find(window, name="preview")
                textvariable = preview.cget("textvariable")
                previews.append(str(preview.getvar(textvariable) if textvariable else preview.cget("text")))
                edited += 1
                if edited == 2:
                    (work / "ui-preview.json").write_text(json.dumps(previews), encoding="utf-8")
                    find(window, caption="저장").invoke()
                    return
                stage = "review"
            window.after(80, operate)

        window.after(150, operate)
        return original_loop(window, *args)
    tk.Misc.mainloop = mainloop
    # This child imports no development fixtures or runtime modules from the checkout.
    sys.path.insert(0, str(cli.parent))
    sys.argv = [str(cli), *argv]
    runpy.run_path(str(cli), run_name="__main__")


def exercise(skill, work):
    from smoke_combo_popup import Actor, bridge
    from smoke_native import Fixture
    cli = skill / "scripts/desktop_select.py"
    exe = compile_fixture(work)
    bridge.set_dpi_awareness()
    fixture = Fixture(exe, ready_timeout=30)
    env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(work / "tasks"))
    binding = ["--exe", exe.name, "--window", fixture.ready["title"], "--hwnd", str(fixture.ready["hwnd"])]
    python = [sys.executable, "-X", "utf8", "-B"]
    results = []
    def call(verb, *args):
        output = subprocess.run([*python, str(cli), verb, *args], cwd=work, env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=90)
        return json.loads(output.stdout)

    def check(case, passed, detail=None):
        results.append({"case": case, "passed": bool(passed)})
        print(json.dumps(results[-1]), flush=True)
        if not passed:
            raise AssertionError((case, detail))

    task_args = ["--task", TASK]
    recorder = None
    try:
        actor = Actor(fixture, exe)
        direct = call("select", *binding, "--target", "MES Setup", "--value", "P")
        check("one_off_command_verified", direct["status"] == "verified" and
              fixture.command("state")["mode"] == "P", direct)
        fixture.command("reset")
        recorder = subprocess.Popen([*python, str(Path(__file__).resolve()), "_gui", str(cli), str(work),
                                     "record", *binding, *task_args], cwd=work, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8")
        deadline = time.monotonic() + 25
        while not (work / "recording-ready").exists():
            if recorder.poll() is not None:
                raise RuntimeError(("record_exited_before_ready", recorder.communicate()))
            if time.monotonic() >= deadline:
                raise RuntimeError("record_not_ready")
            time.sleep(.1)
        def target(key):
            # WPF supplies a candidate rectangle from this fixture only. The
            # input actor independently confirms the same HWND/PID/UIA ID,
            # role, bounds, visibility and hit-test before dispatch.
            deadline = time.monotonic() + 3
            while True:
                node = fixture.command("target|" + key)
                if node["available"]:
                    return node
                if time.monotonic() >= deadline:
                    raise AssertionError(("test_target_unavailable", key, fixture.command("state")))
                time.sleep(.1)

        def click(key):
            candidate = target(key)
            bounds = candidate["bounds"]
            x, y = round(bounds["x"] + bounds["width"] / 2), round(bounds["y"] + bounds["height"] / 2)
            actor.ready()
            hit = bridge.window_from_point(x, y)
            if not actor.binding.contains_window(hit):
                raise AssertionError("synthetic_control_occluded")
            # Hover lets the production recorder observe a short-lived popup.
            if not bridge.user32.SetCursorPos(x, y):
                raise AssertionError("synthetic_hover_failed")
            time.sleep(.5)
            with bridge.UIA() as uia:
                current = uia.from_point(x, y)
                for _ in range(12):
                    if current is None or uia.pid(current) != fixture.proc.pid:
                        raise AssertionError("synthetic_hover_outside_process")
                    if (current.property(30011) == key and bridge.ROLES.get(current.property(30003)) == candidate["role"]
                            and not uia.secure(current) and current.property(30010) is True):
                        break
                    current = uia.parent(current)
                else:
                    raise AssertionError("synthetic_target_changed")
                actor.ready()
                if bridge.window_from_point(x, y) != hit or not actor.binding.contains_window(hit):
                    raise AssertionError("synthetic_target_moved")
                # One atomic move/down/up batch, all three directed at this
                # point. No down or up is sent at the user's current cursor.
                delivery = bridge._send_point(x, y, "click")
                assert delivery.get("input_dispatched") is True, "synthetic_input_uncertain"
            time.sleep(.5)
        # The first field is only pointed out. Its fixed choice must come from
        # the user's later instruction. For the second field, demonstrate N,
        # then supply P as the runtime variable when replaying.
        actor.activate()
        click("ComboAlphaCurrent")
        click("ComboBetaOpen")
        click("ComboBetaOptionN")
        actor.activate()
        click("QueryCommand")
        model = fixture.command("state")
        check("pointed_field_unchanged_and_other_real_selection_witnessed",
              (model["mode"], model["state"], model["queries"], model["mode_clicks"]) == ("Y", "N", 1, 0), model)
        (work / "end-demonstration").touch()
        stdout, stderr = recorder.communicate(timeout=25)
        captured = json.loads(stdout)
        check("record_cli_returns_saved_task", recorder.returncode == 0 and
              captured.get("input_dispatched") is False, (captured, stderr))
        shown = call("show", *task_args)
        task = shown["task"]
        steps = task["steps"]
        check("named_task_preserves_exact_step_order", [s["action"] for s in steps] == ["select", "select", "open"]
              and steps[0]["instruction"].get("value") == "N", task)
        check("review_saves_executable_user_instructions", steps[1].get("alias") == ALIAS and
              [s["instruction"]["text"] for s in steps[:2]] == list(INSTRUCTIONS) and
              [s["instruction"]["mode"] for s in steps[:2]] == ["fixed", "required"], task)
        preview = json.loads((work / "ui-preview.json").read_text(encoding="utf-8"))
        check("operator_preview_shows_fixed_value_and_required_parameter",
              "N" in preview[0] and "family" in preview[1].lower(), preview)
        check("task_found_by_name", TASK in json.dumps(call("tasks"), ensure_ascii=False))

        fixture.command("reset")
        fixture.command("reorder")
        # After an unverified final button, the task must stop with the
        # button actually invoked exactly once, even on the changed layout.
        baseline = fixture.command("state")
        run = call("replay", *binding, *task_args)
        check("missing_variable_stops_before_first_fixed_action", run.get("diagnostic") == "required_parameter_missing"
              and run.get("input_dispatched") is False and fixture.command("state") == baseline, run)
        parameter = steps[1]["instruction"]["parameter"]
        run = call("replay", *binding, *task_args, "--set", parameter + "=P")
        model = fixture.command("state")
        check("replay_after_reorder_invokes_once", (model["mode"], model["state"], model["queries"],
              model["stale_selects"]) == ("N", "P", 1, 0) and run.get("status") == "delivered_unverified", (run, model))
        fixture.command("reset")
        run = call("replay", *binding, *task_args, "--only", ALIAS, "--set", ALIAS + "=P")
        model = fixture.command("state")
        check("natural_language_target_and_value_override", run.get("status") == "verified" and
              (model["mode"], model["state"], model["queries"]) == ("Y", "P", 0), (run, model))
        before = dict(model)
        run = call("replay", *binding, *task_args, "--set", "unknown=N")
        check("bad_override_sends_no_input", run.get("input_dispatched") is False and
              fixture.command("state") == before, run)
        check("replay_preserves_original_recording", call("show", *task_args)["task"] == task)
        near = call("inspect", *binding, *task_args, "--near", ALIAS)
        check("alias_inspects_demonstrated_container", "MES Setup" in json.dumps(near) and
              "Outside" not in json.dumps(near) and near.get("input_dispatched") is False, near)
        near = call("select", *binding, *task_args, "--near", ALIAS, "--target", "MES Setup", "--value", "P")
        model = fixture.command("state")
        check("new_command_targets_neighbor_in_same_container", near["status"] == "verified" and
              model["mode"] == "P" and model["outside"] == "Y", (near, model))
        near = call("select", *binding, *task_args, "--near", ALIAS, "--target", "Outside", "--value", "N")
        check("neighbor_command_cannot_escape_container", near["status"] == "blocked" and
              fixture.command("state") == model, near)
        baseline = fixture.command("reuse-family-id")
        observed = call("inspect", *binding, "--target", "ComboBeta", "--role", "combobox")
        run = call("replay", *binding, *task_args, "--only", ALIAS, "--set", ALIAS + "=P")
        check("reused_id_with_different_live_label_sends_no_input",
              observed.get("target", {}).get("semantic_label") == "Packaging"
              and run.get("status") == "blocked" and run.get("input_dispatched") is False
              and fixture.command("state") == baseline, (observed, run))
    finally:
        if recorder is not None and recorder.poll() is None:
            (work / "end-demonstration").touch()
            try:
                recorder.communicate(timeout=6)
            except subprocess.TimeoutExpired:
                recorder.kill()
                recorder.communicate(timeout=3)
        fixture.close()
    print(json.dumps({"passed": len(results), "total": len(results), "company_fx_tested": False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=ROOT / "skills/desktop-select")
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args()
    if os.name != "nt" or args.work_dir.exists() or not args.work_dir.is_absolute():
        parser.error("This Windows desktop test requires a new absolute work folder")
    args.work_dir.mkdir(parents=True)
    exercise(args.skill_dir.resolve(), args.work_dir)


if __name__ == "__main__":
    if sys.argv[1:2] == ["_gui"]:
        gui_driver(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4:])
    else:
        main()
