"""Test the desktop-select CLI against a separate local WPF app on Windows.

The fixture has two windows in one process, two buttons with the same name,
and two different combo providers. State and per-button counters come from the
app's stdin channel, independently of the accessibility interface under test.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from smoke_native import Fixture


ROOT = Path(__file__).resolve().parents[1]


def compile_fixture(directory):
    framework = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319"
    references = [framework / "WPF" / name for name in (
        "PresentationFramework.dll", "PresentationCore.dll", "WindowsBase.dll",
        "UIAutomationTypes.dll", "UIAutomationProvider.dll")]
    references += [framework / "System.Xaml.dll", framework / "System.Web.Extensions.dll"]
    exe = directory / ("DesktopSelectFixture_" + uuid.uuid4().hex[:10] + ".exe")
    sources = ROOT / "tests/fixtures"
    subprocess.run([str(framework / "csc.exe"), "/nologo", "/target:exe", "/main:DesktopSelectFixture",
                    "/out:" + str(exe), *["/r:" + str(path) for path in references],
                    str(sources / "ComboEvidenceFixture.cs"), str(sources / "DesktopSelectFixture.cs")],
                   check=True)
    return exe


def exercise(skill, exe, work):
    tool = skill / "scripts/desktop_select.py"
    if not tool.is_file():
        raise FileNotFoundError(tool)
    fixture = Fixture(exe)
    title = fixture.ready["title"]
    results = []

    def cli(command, *args):
        argv = [sys.executable, "-X", "utf8", "-B", str(tool), command, "--exe", exe.name]
        if command != "windows":
            argv.extend(("--window", title))
        proc = subprocess.run([*argv, *args], cwd=work, capture_output=True,
                              encoding="utf-8", timeout=60)
        try:
            return json.loads(proc.stdout)
        except ValueError as error:
            raise AssertionError(f"{command} did not return JSON: {proc.stdout} {proc.stderr}") from error

    def check(name, passed, reply=None):
        results.append({"case": name, "passed": bool(passed)})
        print(json.dumps(results[-1]), flush=True)
        if not passed and reply is not None:
            # Only output from the synthetic process is printed here.
            print(json.dumps(reply, ensure_ascii=True)[:2500], flush=True)

    def state():
        return fixture.command("state")

    def signature(model):
        return tuple(model[key] for key in ("status", "status_writes", "family", "inner_queries",
                                             "outer_queries", "sibling_queries"))

    try:
        start = signature(state())
        reply = cli("windows")
        listed = json.dumps(reply)
        check("both_titles_discoverable", title in listed and fixture.ready["sibling_title"] in listed, reply)
        reply = cli("inspect")
        fields = json.dumps(reply)
        check("inspect_reads_target_window_fields", "MES Setup" in fields and "Family" in fields, reply)
        reply = cli("inspect", "--target", "Family", "--role", "combobox")
        check("virtual_combo_is_resolved_while_collapsed", reply.get("status") == "resolved" and
              reply.get("input_dispatched") is False, reply)
        check("read_only_commands_do_not_act", signature(state()) == start)

        reply = cli("open", "--target", "Search", "--role", "button")
        check("duplicate_command_blocked", reply.get("status") == "blocked" and signature(state()) == start, reply)
        fixture.command("duplicate-title")
        try:
            reply = cli("open", "--target", "Search", "--role", "button", "--within", "workPanel")
            check("same_process_same_title_blocks_dispatch",
                  reply.get("status") == "blocked" and signature(state()) == start, reply)
            reply = cli("open", "--target", "Setup screens", "--role", "treeitem", "--expand",
                        "--hwnd", str(fixture.ready["hwnd"]))
            check("explicit_hwnd_expands_only_the_target_tree", reply.get("status") == "verified" and
                  state()["expanded"] is True, reply)
        finally:
            fixture.command("restore-title")

        reply = cli("select", "--target", "MES Setup", "--value", "N", "--within", "workPanel")
        actual = state()
        check("custom_blank_parent_named_field_verified", reply.get("status") == "verified" and
              actual["status"] == "N" and actual["family"] == "Standard", reply)
        before = signature(actual)
        reply = cli("select", "--target", "conditionState", "--value", "N")
        check("already_selected_by_id_sends_no_second_input", reply.get("status") == "verified" and
              reply.get("input_dispatched") is False and signature(state()) == before, reply)
        reply = cli("select", "--target", "MES Setup", "--value", "Hybrid")
        check("option_from_other_combo_is_rejected", reply.get("status") == "blocked" and
              reply.get("input_dispatched") is False and signature(state()) == before, reply)
        reply = cli("select", "--target", "Family", "--value", "Hybrid", "--within", "workPanel")
        actual = state()
        check("second_combo_changes_only_its_selection", reply.get("status") == "verified" and
              actual["family"] == "Hybrid" and actual["status"] == "N", reply)
        before = signature(actual)
        reply = cli("select", "--target", "missing field", "--value", "N")
        check("unknown_field_preserves_both_values",
              reply.get("status") == "blocked" and signature(state()) == before, reply)

        reply = cli("open", "--target", "Search", "--role", "button", "--within", "workPanel")
        deadline = time.monotonic() + 2
        model = state()
        while not model["inner_queries"] and time.monotonic() < deadline:
            time.sleep(.05)
            model = state()
        check("only_scoped_inner_command_runs_once", reply.get("status") == "delivered_unverified"
              and (model["inner_queries"], model["outer_queries"], model["sibling_queries"]) == (1, 0, 0), reply)

        reply = cli("open", "--target", "Export", "--role", "menuitem", "--expand")
        check("menu_expansion_opens_app_submenu", reply.get("status") == "verified" and
              state()["submenu_open"] is True, reply)
        discovery = cli("inspect")
        locations = [()] + [("--popup", str(p["hwnd"])) for p in discovery.get("owned_popups", [])]
        found = []
        for location in locations:
            observed = cli("inspect", "--target", "Active sheet", "--role", "menuitem", *location)
            if observed.get("status") == "resolved":
                found.append(location)
        # A WPF popup can expose the same leaf in the main tree and its owned
        # popup tree. Pick one known location and invoke the leaf only once.
        if found:
            reply = cli("open", "--target", "Active sheet", "--role", "menuitem", *found[-1])
        else:
            reply = discovery
        check("fresh_leaf_invocation_reaches_only_active_sheet", reply.get("status") == "delivered_unverified"
              and state()["sheet_commands"] == 1, reply)
    finally:
        fixture.close()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=ROOT / "skills/desktop-select",
                        help="Path to the source or unpacked desktop-select skill")
    parser.add_argument("--compile-only", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This test needs the Windows desktop")
    with tempfile.TemporaryDirectory(prefix="desktop-select-smoke-") as directory:
        work = Path(directory)
        exe = compile_fixture(work)
        if args.compile_only:
            print("Fixture compiled")
            return
        results = exercise(args.skill_dir.resolve(), exe, work)
    passed = sum(case["passed"] for case in results)
    print(json.dumps({"passed": passed, "total": len(results), "company_fx_tested": False}))
    raise SystemExit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
