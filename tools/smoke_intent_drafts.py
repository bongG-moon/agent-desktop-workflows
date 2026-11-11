"""Actual Windows recording acceptance for action-derived natural-language drafts.

Use the existing synthetic WPF business window and its independent state channel.
Only Tk controls are driven programmatically; recording uses real scoped Windows
mouse events, UIA observations and production HookPump/worker/persistence code.
The fixture's actual user clicks may change its values and run its synthetic query.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time
import traceback

from smoke_native import Fixture


ROOT = Path(__file__).resolve().parents[1]
TASK = "Recorded action draft acceptance"
MES_ALIAS = "검토용 MES 별칭"
FAMILY_TEXT = "실행 시 받은 {ChosenFamily}로 FAMILY를 설정하고 화면의 현재값은 무시해 주세요.\n내가 작성한 문장을 보존하세요."


def compile_fixture(work):
    """Reuse the existing provider/business fixture, with fully visible options.

    The old query/export fixture has a caption wider than its compact combo;
    its N option is partially clipped. This development-only source copy just
    shortens that caption's layout allocation. No provider, values or handlers
    are modified, and no source/binary from this copy is part of the package.
    """
    source = ROOT / "tests/fixtures"
    combo_text = (source / "ComboEvidenceFixture.cs").read_text(encoding="utf8")
    old = "caption=new TextBlock { Width=185,"
    assert combo_text.count(old) == 1
    combo_copy = work / "VisibleComboEvidenceFixture.cs"
    combo_copy.write_text(combo_text.replace(old, "caption=new TextBlock { Width=120,"), encoding="utf8")
    net = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319"
    refs = [net / "WPF" / (name + ".dll") for name in (
        "PresentationFramework", "PresentationCore", "WindowsBase", "UIAutomationTypes", "UIAutomationProvider")]
    refs += [net / (name + ".dll") for name in ("System.Web.Extensions", "System.Xaml", "System.IO.Compression")]
    exe = work / "DesktopContextFixture.exe"
    subprocess.run([str(net / "csc.exe"), "/nologo", "/target:exe", "/main:DesktopContextFixture",
                    "/out:" + str(exe), *["/r:" + str(ref) for ref in refs], str(combo_copy),
                    str(source / "DesktopContextFixture.cs")], check=True)
    return exe


def gui_driver(tool, folder, mode, argv):
    import tkinter as tk
    original_loop = tk.Misc.mainloop

    def mainloop(root, *args):
        stage = "start" if mode == "record" else "reopen"
        snapshots = {}

        def walk(parent):
            for child in parent.winfo_children():
                yield child
                yield from walk(child)

        def widget(name):
            return next(w for w in walk(root) if w.winfo_name() == name)

        def press(caption):
            next(w for w in walk(root) if w.winfo_class() in ("Button", "TButton")
                 and str(w.cget("text")) == caption).invoke()

        def select(index):
            tree = widget("steps")
            iid = tree.get_children()[index]
            tree.selection_set(iid)
            tree.focus(iid)
            tree.event_generate("<<TreeviewSelect>>")
            root.update_idletasks()

        def form():
            return {"text": widget("instruction").get("1.0", "end-1c"),
                    "parameters": widget("parameters").get(),
                    "alias": widget("alias").get(),
                    "count": len(widget("steps").get_children())}

        def save_snapshot(name):
            snapshots[name] = form()
            (folder / "ui-snapshots.json").write_text(json.dumps(snapshots, ensure_ascii=False, indent=2), encoding="utf8")

        def reached(name, count):
            return (folder / name).exists() and len(widget("steps").get_children()) >= count

        def tick():
            nonlocal stage
            try:
                if stage == "start":
                    press("시작")
                    root.iconify()
                    (folder / "ready").touch()
                    stage = "mes-location"
                elif stage == "mes-location" and reached("mes-location-clicked", 1):
                    select(0)
                    current = form()
                    if not current["text"]:
                        root.after(100, tick)
                        return
                    save_snapshot("mes-location")
                    # Alias-only edits must survive a later automatic text update.
                    widget("alias").delete(0, "end")
                    widget("alias").insert(0, MES_ALIAS)
                    (folder / "mes-location-reviewed").touch()
                    stage = "mes-option"
                elif stage == "mes-option" and reached("mes-option-clicked", 1):
                    # Do not touch the text: the same row/form must update from
                    # the witnessed option click, while still automatically drafted.
                    current = form()
                    if '"N"' not in current["text"]:
                        root.after(100, tick)
                        return
                    assert current["count"] == 1 and not current["parameters"], current
                    assert current["alias"] == MES_ALIAS, current
                    save_snapshot("mes-option")
                    (folder / "mes-option-reviewed").touch()
                    stage = "family-location"
                elif stage == "family-location" and reached("family-location-clicked", 2):
                    select(1)
                    current = form()
                    if not current["text"]:
                        root.after(100, tick)
                        return
                    assert "Hybrid" not in current["text"], current
                    save_snapshot("family-location")
                    # Deliberately leave these edits unapplied. Later option
                    # action draft must not overwrite either dirty form buffer.
                    widget("instruction").delete("1.0", "end")
                    widget("instruction").insert("1.0", FAMILY_TEXT)
                    widget("parameters").delete(0, "end")
                    widget("parameters").insert(0, "ChosenFamily")
                    (folder / "family-custom-written").touch()
                    stage = "family-followup"
                elif stage == "family-followup" and reached("family-followup-clicked", 2):
                    current = form()
                    assert current["text"] == FAMILY_TEXT and current["parameters"] == "ChosenFamily", current
                    assert current["count"] == 2, current
                    save_snapshot("family-after-followup")
                    (folder / "family-custom-preserved").touch()
                    stage = "button"
                elif stage == "button" and reached("query-clicked", 3):
                    select(2)
                    current = form()
                    if not current["text"]:
                        root.after(100, tick)
                        return
                    assert "버튼" in current["text"] and "눌러" in current["text"], current
                    save_snapshot("query")
                    (folder / "query-reviewed").touch()
                    stage = "rightclick"
                elif stage == "rightclick" and reached("grid-right-clicked", 4):
                    select(3)
                    current = form()
                    if "우클릭" not in current["text"]:
                        root.after(100, tick)
                        return
                    save_snapshot("rightclick")
                    press("마치고 검토")
                    press("전체 저장")
                    return
                elif stage == "reopen":
                    expected = json.loads((folder.parent / "record-gui/ui-snapshots.json").read_text(encoding="utf8"))
                    for number, key in enumerate(("mes-option", "family-after-followup", "query", "rightclick")):
                        select(number)
                        current = form()
                        assert current["text"] == expected[key]["text"], (key, current, expected[key])
                        assert current["parameters"] == expected[key]["parameters"], (key, current, expected[key])
                        assert current["alias"] == expected[key]["alias"], (key, current, expected[key])
                        save_snapshot(key)
                    press("전체 저장")
                    return
                root.after(100, tick)
            except BaseException:
                (folder / "gui-error.txt").write_text(traceback.format_exc(), encoding="utf8")
                root.destroy()

        root.after(200, tick)
        root.after(60000, lambda: root.destroy() if root.winfo_exists() else None)
        return original_loop(root, *args)

    tk.Misc.mainloop = mainloop
    sys.path.insert(0, str(tool.parent))
    sys.argv = [str(tool), *argv]
    runpy.run_path(str(tool), run_name="__main__")


def exercise(skill, work, exe, expected_version):
    sys.path.insert(0, str(skill / "scripts"))
    import desktop_select as app
    import learned_tasks as tasks
    import row_action
    from provider_visibility import visible

    app.bridge.set_dpi_awareness()
    fixture = Fixture(exe, str(work / "unused-export.xlsx"), ready_timeout=25)
    tool = skill / "scripts/desktop_select.py"
    env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(work / "tasks"))
    target = ["--exe", exe.name, "--window", fixture.ready["title"], "--hwnd", str(fixture.ready["hwnd"])]
    binding = app.bridge.Binding(app.pick_window(app.windows(exe.name), fixture.ready["title"], fixture.ready["hwnd"]))
    results = []
    gui = None

    def check(name, condition, detail=None):
        print(json.dumps({"case": name, "passed": bool(condition)}), flush=True)
        if not condition:
            raise AssertionError((name, detail))
        results.append(name)

    def cli(command, *extra):
        proc = subprocess.run([sys.executable, "-X", "utf8", "-B", str(tool), command, *extra],
                              cwd=work, env=env, capture_output=True, encoding="utf8", timeout=50)
        try:
            return json.loads(proc.stdout)
        except ValueError as error:
            raise AssertionError((command, proc.stdout, proc.stderr)) from error

    def wait_marker(name, seconds=12):
        path = work / "record-gui" / name
        deadline = time.monotonic() + seconds
        while not path.exists():
            error = path.parent / "gui-error.txt"
            if error.exists():
                raise AssertionError(error.read_text(encoding="utf8"))
            if gui.poll() is not None:
                raise AssertionError(("gui_exited", gui.communicate()))
            if time.monotonic() >= deadline:
                raise AssertionError(("gui_draft_not_ready", name))
            time.sleep(.08)

    def mark(name):
        (work / "record-gui" / name).touch()

    def click(*, identity=None, name=None, role=None, combo=None, right=False, preferred=(.5, .5)):
        binding.check()
        if not binding.contains_window(app.bridge.foreground()):
            row_action.foreground(binding)
        with app.bridge.UIA() as uia:
            found = []
            for hwnd in binding.scoped_windows():
                scope_binding = app.bridge.Binding(dict(pid=binding.pid, hwnd=hwnd,
                    exe_name=binding.exe, process_started_utc_ticks=binding.created))
                nodes, elements = app.observe(scope_binding, uia)
                for node, element in zip(nodes, elements):
                    if (identity is not None and node.get("automation_id") != identity
                            or name is not None and node.get("name") != name
                            or role is not None and node.get("role") != role):
                        continue
                    if not visible(binding, uia, element) or element.property(30010) is not True or uia.secure(element):
                        continue
                    if combo is not None:
                        owner = uia.related_element(element, 30080)
                        if owner is None or owner.property(30011) != combo:
                            continue
                    if not any(uia.same(element, old) for old in found):
                        found.append(element)
            assert len(found) == 1, ("fixture_target_not_unique", identity, name, len(found))
            element = found[0]
            box = uia.bounds(element)
            def hit_matches(x, y):
                if not binding.contains_window(app.bridge.window_from_point(x, y)):
                    return False
                actual = uia.from_point(x, y)
                for _ in range(16):
                    if actual is None or uia.pid(actual) != binding.pid:
                        return False
                    if uia.same(actual, element):
                        return True
                    actual = uia.parent(actual)
                return False
            # Existing WPF fixture children can be partially clipped by their
            # parent. Probe only this observed control's own rectangle, then
            # send one click at a point that really hits the same UIA element.
            points = [(round(box["x"]+box["width"]*rx), round(box["y"]+box["height"]*ry))
                      for rx, ry in (preferred, (.25, .5), (.75, .5), (.5, .25), (.5, .75))]
            matching = next(((x, y) for x, y in points if hit_matches(x, y)), None)
            assert matching is not None, "fixture_point_identity_unavailable"
            x, y = matching
            assert app.bridge.user32.SetCursorPos(x, y)
            time.sleep(.65)
            assert hit_matches(x, y), "fixture_point_identity_changed"
            assert binding.contains_window(app.bridge.foreground())
            assert binding.contains_window(app.bridge.window_from_point(x, y))
            assert app.bridge._send_point(x, y, "right-click" if right else "click")["input_dispatched"] is True
        time.sleep(.65)

    def start_gui(mode):
        folder = work / (mode + "-gui")
        folder.mkdir()
        return subprocess.Popen([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                                 "_gui", str(tool), str(folder), mode, mode, *target, "--task", TASK],
                                cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf8")

    try:
        doctor = cli("doctor")
        check("tested_package_identifies_expected_version", doctor.get("version") == expected_version
              and Path(doctor.get("skill_path", "")).resolve() == skill.resolve(), doctor)
        initial = cli("select", *target, "--target", "Family", "--value", "Hybrid")
        assert initial.get("status") == "verified", initial
        assert fixture.command("state")["family"] == "Hybrid"
        gui = start_gui("record")
        wait_marker("ready")
        # Point at the passive caption, not the visible Y/N option buttons
        # that occupy the other part of this deliberately custom combo.
        click(identity="conditionState", role="combobox", preferred=(.15, .5))
        mark("mes-location-clicked")
        wait_marker("mes-location-reviewed")
        click(identity="conditionStateOptionN", role="listitem", combo="conditionState")
        mark("mes-option-clicked")
        wait_marker("mes-option-reviewed")
        check("actual_option_click_changes_fixture_to_n", fixture.command("state")["status"] == "N")
        click(identity="conditionFamily", role="combobox")
        mark("family-location-clicked")
        wait_marker("family-custom-written")
        # The native WPF popup has a hit-test peer without SelectionContainer,
        # so selecting its different non-hit peer would be an invalid test.
        # A real right click on this proven combo supplies a different late
        # draft, exercising preservation of the user's pending form edits.
        click(identity="conditionFamily", role="combobox", right=True)
        mark("family-followup-clicked")
        wait_marker("family-custom-preserved")
        check("family_current_hybrid_remains_only_observed_state", fixture.command("state")["family"] == "Hybrid")
        with app.bridge.UIA() as uia:
            nodes, elements = app.observe(binding, uia)
            family, = [element for node, element in zip(nodes, elements)
                       if node.get("automation_id") == "conditionFamily"]
            opened = family.property(30070) == 1
        if opened:
            click(identity="conditionFamily", role="combobox")
        click(identity="QueryCommand", role="button")
        mark("query-clicked")
        wait_marker("query-reviewed")
        deadline = time.monotonic() + 4
        actual = fixture.command("state")
        while not actual["ready"] and time.monotonic() < deadline:
            time.sleep(.1)
            actual = fixture.command("state")
        assert actual["ready"] and actual["queries"] == 1, actual
        click(identity="ResultSurface", role="table", right=True)
        mark("grid-right-clicked")
        stdout, stderr = gui.communicate(timeout=20)
        error = work / "record-gui/gui-error.txt"
        if error.exists():
            raise AssertionError(error.read_text(encoding="utf8"))
        reply = json.loads(stdout)
        check("record_cli_saves_automatically_drafted_actions", gui.returncode == 0
              and reply.get("status") == "recorded", (reply, stderr))
        plan = tasks.load_task(TASK, work / "tasks")
        ui = json.loads((work / "record-gui/ui-snapshots.json").read_text(encoding="utf8"))
        check("location_only_combo_draft_requests_input_not_current_value", "{" in ui["mes-location"]["text"]
              and bool(ui["mes-location"]["parameters"])
              and "Hybrid" not in ui["family-location"]["text"] and bool(ui["family-location"]["parameters"]), ui)
        check("witnessed_option_updates_same_draft_to_fixed_n", ui["mes-option"]["count"] == 1
              and '"N"' in ui["mes-option"]["text"] and not ui["mes-option"]["parameters"], ui)
        check("unapplied_alias_survives_same_row_automatic_update", ui["mes-option"]["alias"] == MES_ALIAS, ui)
        check("actual_family_followup_right_click_preserves_custom_instruction", ui["family-after-followup"]["text"] == FAMILY_TEXT
              and ui["family-after-followup"]["parameters"] == "ChosenFamily", ui)
        steps = plan["steps"]
        check("saved_steps_preserve_auto_and_user_intents", len(steps) == 4
              and steps[0]["anchor"]["automation_id"] == "conditionState"
              and '"N"' in steps[0]["instruction"]["text"] and steps[0]["instruction"]["parameters"] == []
              and steps[1]["instruction"] == {"mode": "natural", "text": FAMILY_TEXT, "parameters": ["ChosenFamily"]}
              and "Hybrid" not in json.dumps(plan), plan)
        check("button_draft_uses_displayed_name", "Search" in steps[2]["instruction"]["text"]
              and "버튼" in steps[2]["instruction"]["text"], steps[2])
        check("rightclick_draft_preserves_result_region", steps[3]["anchor"]["automation_id"] == "ResultSurface"
              and "우클릭" in steps[3]["instruction"]["text"] and len(steps[3]["anchor"]["point"]) == 2, steps[3])
        actual = fixture.command("state")
        check("only_demonstrated_query_and_rightclick_occur", actual["queries"] == actual["right_clicks"]
              == actual["menus"] == 1 and actual["leaves"] == 0 and actual["writes"] == 0, actual)
        gui = start_gui("edit")
        stdout, stderr = gui.communicate(timeout=20)
        error = work / "edit-gui/gui-error.txt"
        if error.exists():
            raise AssertionError(error.read_text(encoding="utf8"))
        reply = json.loads(stdout)
        check("reopened_gui_retains_all_drafts_and_custom_parameters", gui.returncode == 0
              and reply.get("status") == "recorded" and tasks.load_task(TASK, work / "tasks") == plan, (reply, stderr))
    finally:
        if gui is not None and gui.poll() is None:
            gui.kill()
            gui.communicate(timeout=3)
        fixture.close()
    summary = {"passed": len(results), "total": len(results), "company_fx_tested": False}
    (work / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf8")
    print(json.dumps(summary), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=ROOT / "skills/desktop-select")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--expected-version", default="0.5.1")
    parser.add_argument("--prepare-only", action="store_true", help="Compile only; do not open a GUI or dispatch input")
    args = parser.parse_args()
    if os.name != "nt" or not args.work_dir.is_absolute() or args.work_dir.exists():
        parser.error("This Windows test requires a new absolute work directory")
    args.work_dir.mkdir(parents=True)
    exe = compile_fixture(args.work_dir)
    if args.prepare_only:
        print(str(exe))
        return
    exercise(args.skill_dir.resolve(), args.work_dir, exe, args.expected_version)


if __name__ == "__main__":
    if sys.argv[1:2] == ["_gui"]:
        gui_driver(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[5:])
    else:
        main()
