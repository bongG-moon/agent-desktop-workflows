"""Actual Windows tests confined to this package's synthetic NativeFixture.

The recorder observes only its fixture. Physical input and temporary recording
hooks are exercised, and the fixture is closed in finally. No model/network calls.
"""
from __future__ import annotations
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path, timeout=3):
    def reject_nonfinite(value):
        raise ValueError("Non-finite number is not valid JSON: " + value)
    deadline = time.monotonic() + timeout
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_nonfinite)
        except (OSError, json.JSONDecodeError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(.03)


class Fixture:
    def __init__(self, exe, *args, ready_timeout=15):
        self.proc = subprocess.Popen([str(exe), *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
        self.lines = queue.Queue()
        def reader():
            for line in self.proc.stdout:
                self.lines.put(json.loads(line))
        threading.Thread(target=reader, daemon=True).start()
        try:
            self.ready = self.lines.get(timeout=ready_timeout)
            assert self.ready["kind"] == "ready" and self.ready["pid"] == self.proc.pid, 'fixture_startup_identity_mismatch'
        except BaseException as error:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(timeout=5)
            if isinstance(error, queue.Empty):
                raise RuntimeError('fixture_startup_timeout') from error
            raise
    def command(self, text):
        self.proc.stdin.write(text + "\n"); self.proc.stdin.flush()
        if text != "quit":
            return self.lines.get(timeout=10)
    def close(self):
        if self.proc.poll() is None:
            try:
                self.command("quit"); self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill(); self.proc.wait(timeout=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--product-dir", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    if os.name != "nt" or not args.work_dir.is_absolute():
        parser.error("Windows and an absolute local work directory are required")
    # Native UIA bounds are physical coordinates. Match the helper on high-DPI
    # desktops before SetCursorPos prepares a passive recording observation.
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    root = args.product_dir.resolve(strict=True); work = args.work_dir
    work.mkdir(parents=True, exist_ok=True)
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    exe = work / "NativeFixture.exe"
    subprocess.run([str(compiler), "/nologo", "/target:exe", "/out:" + str(exe),
        "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll", "/r:System.Web.Extensions.dll",
        str(root / "tests/fixtures/NativeFixture.cs")], capture_output=True, check=True)
    helper = [sys.executable, '-I', '-X', 'utf8', '-B', str(root / 'scripts/py_helper.py')]
    results = []; sequence = 0
    def call(command, request, timeout=20):
        nonlocal sequence
        sequence += 1; req = work / (str(sequence) + "-request.json"); res = work / (str(sequence) + "-response.json")
        save(req, request)
        process = subprocess.run([*helper, command, "--request", str(req), "--response", str(res)],
            capture_output=True, timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
        assert not process.stdout and not process.stderr
        return read_json(res)
    def record(name, passed, **details):
        results.append(dict(case=name, passed=bool(passed), **details))
        print(json.dumps({"case": name, "passed": bool(passed)}), flush=True)
    fixture = Fixture(exe); recorder = None
    try:
        binding = {"pid": fixture.proc.pid, "hwnd": fixture.ready["hwnd"], "exe_name": "NativeFixture.exe"}
        inspected = call("inspect", binding); assert inspected["ok"]
        data = inspected["data"]; binding["process_started_utc_ticks"] = data["process_started_utc_ticks"]
        region = next(n for n in data["nodes"] if n["automation_id"] == "ResultRegion")
        notes = next(n for n in data["nodes"] if n["automation_id"] == "NotesEditor")
        check = next(n for n in data["nodes"] if n["automation_id"] == "EnableOption")
        protected = next(n for n in data["nodes"] if n["automation_id"] == "ProtectedEditor")
        hidden = next(n for n in data["nodes"] if n["name"] == "Closed submenu item")
        record("empty_uia_bounds_are_valid_json", hidden["bounds_valid"] is False and hidden["offscreen"] is True
            and all(v == 0 for v in hidden["bounds"].values()))
        hidden_action = call("point", dict(binding, locator=hidden["locator"], point={"x_ratio": .5, "y_ratio": .5}, action="click"))
        record("empty_uia_bounds_block_input", hidden_action.get("input_dispatched") is False
            and hidden_action.get("diagnostic") in {"target_bounds_unavailable", "target_not_actionable"})
        record("structural_region_observed", region["role"] == "group" and region["name"] == "Result region")
        record("explicit_unchecked_state", check["checked"] is False)
        record("protected_value_withheld", protected["secure"] is True and protected["value"] is None)
        request = dict(binding, locator=region["locator"], point={"x_ratio": .4, "y_ratio": .4}, action="right-click")
        activated = call("activate", binding); assert activated.get("foreground_verified")
        action = call("point", request); state = fixture.command("state")
        record("no_ref_region_right_click", action.get("input_dispatched") is True and state["context_count"] == 1, response=action)
        fixture.command("close-menu"); fixture.command("move"); fixture.command("resize")
        call("activate", binding); action = call("point", request); state = fixture.command("state")
        record("region_recomputed_after_move_resize", action.get("input_dispatched") is True and state["context_count"] == 2)
        fixture.command("close-menu")
        bad = call("point", dict(request, process_started_utc_ticks="1"))
        record("changed_lifetime_rejected", bad.get("input_dispatched") is False and bad.get("diagnostic") == "target_lifetime_changed")
        root_node = next(n for n in data["nodes"] if n["role"] == "window")
        bad = call("point", dict(request, locator=root_node["locator"]))
        record("window_root_not_substituted", bad.get("input_dispatched") is False and bad.get("diagnostic") == "window_root_point_rejected")
        fixture.command("sibling")
        bad = call("point", request)
        record("same_pid_other_window_blocks_input", bad.get("input_dispatched") is False)
        call("activate", binding); fixture.command("reset")
        recording_dir = work / "recording"; recording_dir.mkdir(exist_ok=True)
        record_req = dict(binding, output_dir=str(recording_dir), progress_path=str(recording_dir / "progress.json"),
                          draft_path=str(recording_dir / "draft.json"), stop_path=str(recording_dir / "stop"),
                          name="Native recording acceptance", auto_start=True)
        req = recording_dir / "request.json"; res = recording_dir / "response.json"; save(req, record_req)
        recorder = subprocess.Popen([*helper, "record", "--request", str(req), "--response", str(res)], creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.time() + 10
        while time.time() < deadline:
            if (recording_dir / "progress.json").exists():
                progress = read_json(recording_dir / "progress.json")
                if progress["state"] == "recording": break
            time.sleep(.1)
        else: raise RuntimeError("recorder_not_ready")
        # Only fixture-owned controls are targeted. Move first so the passive
        # recorder can cache pre-click accessibility without blocking hooks.
        current = call("inspect", binding)["data"]
        notes = next(n for n in current["nodes"] if n["automation_id"] == "NotesEditor")
        r = notes["bounds"]; user.SetCursorPos(round(r["x"] + r["width"] * .5), round(r["y"] + r["height"] * .5)); time.sleep(.7)
        assert call("point", dict(binding, locator=notes["locator"], point={"x_ratio": .5, "y_ratio": .5}, action="click")).get("input_dispatched") is True
        time.sleep(.4); fixture.command("set-notes"); time.sleep(1.2)
        region = next(n for n in current["nodes"] if n["automation_id"] == "ResultRegion")
        r = region["bounds"]; user.SetCursorPos(round(r["x"] + r["width"] * .4), round(r["y"] + r["height"] * .4)); time.sleep(.7)
        save(recording_dir / "before-region.json", read_json(recording_dir / "progress.json"))
        assert call("point", dict(binding, locator=region["locator"], point={"x_ratio": .4, "y_ratio": .4}, action="right-click")).get("input_dispatched") is True
        time.sleep(.8); fixture.command("close-menu")
        # Unsupported wheel activity must be preserved as a manual review step.
        user.mouse_event(0x0800, 0, 0, 120, 0); time.sleep(.7)
        live_progress = read_json(recording_dir / "progress.json")
        record("live_unrecognized_step_notice", live_progress["warning_count"] > 0 and "last_step" in live_progress)
        save(recording_dir / "stop", {})
        recorder.wait(timeout=8); recorder = None
        final = read_json(res); draft = read_json(recording_dir / "draft.json")
        steps = draft["steps"]
        record("recorded_final_value", any(s["action"] == "set-value" and s.get("value") == "recorded final" for s in steps))
        record("recorded_ref_less_region", any(s["action"] == "right-click" and s["target"]["kind"] == "region"
            and s["target"]["locator"].get("automation_id") == "ResultRegion" for s in steps))
        record("unsupported_step_preserved", any(s["action"] == "manual" and s["recognition"].get("reason") == "scroll_needs_review" for s in steps))
        record("recording_original_events_preserved", Path(final["recording_path"]).is_file())
        record("recording_hooks_end_with_process", final["status"] == "recorded")
    finally:
        if recorder and recorder.poll() is None:
            recorder.kill(); recorder.wait(timeout=3)
        fixture.close()
    summary = {"synthetic_only": True, "actual_fx_tested": False, "passed": sum(r["passed"] for r in results), "total": len(results), "results": results}
    save(work / "summary.json", summary)
    print(json.dumps({k:v for k,v in summary.items() if k != "results"}), flush=True)
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
