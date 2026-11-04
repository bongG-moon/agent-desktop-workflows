"""Fixed read-only agent-desktop observer. Raw UI content never goes to stdout."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

from local_core import (EngineError, _Child, _identity, foreground, process_path,
                        validate_local_directory)
from recognition import derive

VERSION = "0.2.0"
BINARY_SHA256 = "2c8e9a627d87649a4562bfe5678450d045b793ab6f3fdec8af05c9571f32d5ef"
BINARY = Path(__file__).resolve().parents[1] / "bin" / "agent-desktop.exe"
STAGES = ("window", "menu", "combo", "grid", "property")


def exe_name(value):
    if (not isinstance(value, str) or not value.lower().endswith(".exe") or
            len(value) > 200 or re.search(r'[<>:"/\\|?*\x00-\x1f]', value) or
            value.startswith(("-", ".")) or value.strip() != value):
        raise EngineError("invalid_executable_basename")
    return value


def verify_binary():
    validate_local_directory(BINARY.parent)
    if BINARY.is_symlink() or getattr(BINARY.lstat(), "st_file_attributes", 0) & 1024:
        raise EngineError("binary_reparse_rejected")
    with BINARY.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(stream.read()).hexdigest()
    if digest != BINARY_SHA256:
        raise EngineError("binary_hash_mismatch")
    return BINARY


def validate_command(arguments):
    if (len(arguments) == 5 and arguments[0:2] == ["list-windows", "--app"]
            and arguments[3:] == ["--timeout-ms", "10000"]):
        exe_name(arguments[2])
        return
    if (len(arguments) == 10 and arguments[0:2] == ["snapshot", "--window-id"]
            and re.fullmatch(r"w-[1-9][0-9]*", arguments[2])
            and arguments[3:5] == ["--surface", "window"]
            and arguments[5:] == ["--max-depth", "12", "--include-bounds", "--timeout-ms", "10000"]):
        return
    raise EngineError("command_not_allowed")


def child_environment(run):
    env = {}
    inherited = {k.upper(): v for k, v in os.environ.items()}
    # Windows system/library paths and the native interaction lock must stay local.
    for key in ("SYSTEMROOT", "WINDIR", "PROGRAMDATA"):
        if key in inherited:
            env[key] = str(validate_local_directory(inherited[key]))
    if "SYSTEMROOT" not in env:
        raise EngineError("local_system_directory_required")
    env["SYSTEMDRIVE"] = Path(env["SYSTEMROOT"]).drive
    for key, relative in (("TEMP", "temp"), ("TMP", "temp"), ("LOCALAPPDATA", "local"),
                          ("APPDATA", "roaming"), ("USERPROFILE", "profile")):
        directory = run / relative
        directory.mkdir(exist_ok=True)
        env[key] = str(directory)
    # agent-desktop creates the private leaf with its own ACL.
    env["AGENT_DESKTOP_HOME"] = str(run / "agent-state")
    env["RUST_LOG"] = "warn"
    return env


def validate_snapshot_identity(raw, pid, hwnd, name):
    """Reject contradictory target metadata before interpreting the payload."""
    if raw.get("ok") is not True:
        return
    data = raw.get("data")
    if not isinstance(data, dict) or data.get("identity_unproven") is True:
        raise EngineError("snapshot_identity_unproven")
    expected_id = "w-" + str(hwnd)
    window = data.get("window")
    if window is not None:
        if not isinstance(window, dict) or window.get("id") != expected_id:
            raise EngineError("snapshot_window_mismatch")
        if "pid" in window and window["pid"] != pid:
            raise EngineError("snapshot_process_mismatch")
    for key in ("window_id", "target_window_id"):
        if key in data and data[key] != expected_id:
            raise EngineError("snapshot_window_mismatch")
    for key in ("pid", "target_pid", "process_id"):
        if key in data and (type(data[key]) is not int or data[key] != pid):
            raise EngineError("snapshot_process_mismatch")
    if "app" in data and (not isinstance(data["app"], str) or data["app"].casefold() != name.casefold()):
        raise EngineError("snapshot_application_mismatch")


def _invoke(arguments, run):
    validate_command(arguments)
    binary = verify_binary()
    validate_local_directory(run)
    child = _Child([str(binary), *arguments], env=child_environment(run), cwd=run, timeout=15)
    try:
        return child.receive()
    finally:
        child.close()


def _write(run, name, value):
    validate_local_directory(run)
    with (run / name).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def new_run(output):
    root = validate_local_directory(output)
    run = root / uuid.uuid4().hex
    run.mkdir()
    validate_local_directory(run)
    return run


def inventory(name, run):
    name = exe_name(name)
    response = _invoke(["list-windows", "--app", name, "--timeout-ms", "10000"], run)
    if response.get("ok") is not True or not isinstance(response.get("data"), list):
        raise EngineError("window_inventory_unavailable")
    rows = []
    for row in response["data"]:
        if not isinstance(row, dict) or type(row.get("pid")) is not int:
            continue
        ident = row.get("id", "")
        if not isinstance(ident, str) or not re.fullmatch(r"w-[1-9][0-9]*", ident):
            continue
        pid, hwnd = row["pid"], int(ident[2:])
        try:
            image = process_path(pid)
            if Path(image).name.casefold() != name.casefold():
                continue
            identity = _identity(pid, hwnd, image)
        except EngineError:
            continue
        rows.append({"pid": pid, "hwnd": hwnd, "window_id": ident,
                     "exe": image, "title": row.get("title"), "identity": identity,
                     "upstream": row})
    return rows


def receipt(run, status, **counts):
    # Never return arguments, paths, labels, app names, IDs, or arbitrary errors.
    result = {"status": status, "receipt": run.name, "read_only": True,
              "input_dispatched": False, "model_calls": 0, "network_requests_by_wrapper": 0}
    for key in ("window_count", "scope_count", "observed_scope_count", "node_count"):
        if key in counts:
            result[key] = int(counts[key])
    if "foreground_unchanged" in counts:
        result["foreground_unchanged"] = counts["foreground_unchanged"] is True
    return result


def run_inventory(args):
    name = exe_name(args.exe_name)
    run = new_run(args.output)
    before = foreground()
    rows = inventory(name, run)
    _write(run, "inventory.json", {"windows": rows, "read_only": True})
    unchanged = before == foreground()
    status = ("observed" if rows else "no_matching_window") if unchanged else "interrupted"
    result = receipt(run, status, window_count=len(rows), foreground_unchanged=unchanged)
    _write(run, "receipt.json", result)
    return result


def run_observe(args):
    name = exe_name(args.exe_name)
    if args.stage not in STAGES:
        raise EngineError("invalid_stage")
    related = list(args.related_hwnd or [])
    if len(related) > 4:
        raise EngineError("too_many_related_windows")
    targets = [("target", args.hwnd)]
    if args.parent_hwnd:
        targets.append(("parent", args.parent_hwnd))
    targets.extend(("related_" + str(i + 1), hwnd) for i, hwnd in enumerate(related))
    if len({hwnd for _, hwnd in targets}) != len(targets):
        raise EngineError("duplicate_window_scope")
    image = process_path(args.pid)
    if Path(image).name.casefold() != name.casefold():
        raise EngineError("process_executable_mismatch")
    identities = {hwnd: _identity(args.pid, hwnd, image) for _, hwnd in targets}
    run = new_run(args.output)
    before = foreground()
    rows = inventory(name, run)
    # No unrelated process/window descriptions in this observation run.
    rows = [row for row in rows if row["pid"] == args.pid and row["hwnd"] in identities]
    _write(run, "inventory.json", {"windows": rows, "read_only": True})
    results = []
    for label, hwnd in targets:
        if identities[hwnd] != _identity(args.pid, hwnd, image):
            raise EngineError("target_identity_changed")
        matches = [row for row in rows if row["hwnd"] == hwnd and tuple(row["identity"]) == identities[hwnd]]
        if len(matches) != 1:
            results.append({"scope": label, "hwnd": hwnd, "status": "unavailable",
                            "reason": "target_not_in_upstream_window_inventory"})
            continue
        # Upstream menu surface searches the entire PID, including other business
        # windows. Only an explicitly selected window root is within this scope.
        surfaces = ["window"]
        for surface in surfaces:
            if identities[hwnd] != _identity(args.pid, hwnd, image):
                raise EngineError("target_identity_changed")
            raw = _invoke(["snapshot", "--window-id", matches[0]["window_id"], "--surface", surface,
                           "--max-depth", "12", "--include-bounds", "--timeout-ms", "10000"], run)
            if identities[hwnd] != _identity(args.pid, hwnd, image):
                raise EngineError("target_identity_changed")
            validate_snapshot_identity(raw, args.pid, hwnd, name)
            projection = derive(raw, args.stage)
            record = {"scope": label, "surface": surface, "hwnd": hwnd, "pid": args.pid,
                      "status": projection["status"], "raw": raw, "recognition": projection}
            _write(run, label + "-" + surface + ".json", record)
            results.append({"scope": label, "surface": surface, "hwnd": hwnd,
                            "status": projection["status"], "node_count": projection["node_count"]})
    for _, hwnd in targets:
        if identities[hwnd] != _identity(args.pid, hwnd, image):
            raise EngineError("target_identity_changed")
    unchanged = before == foreground()
    observed = sum(row["status"] == "observed" for row in results)
    status = "observed" if observed == len(results) else "partial" if observed else "unavailable"
    if not unchanged:
        status = "interrupted"
    _write(run, "observation.json", {"stage": args.stage, "scopes": results, "status": status,
                                     "foreground_unchanged": unchanged,
                                     "recognition_validated": False, "read_only": True})
    result = receipt(run, status, scope_count=len(results), observed_scope_count=observed,
                     node_count=sum(row.get("node_count", 0) for row in results),
                     foreground_unchanged=unchanged)
    _write(run, "receipt.json", result)
    return result


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise EngineError("invalid_arguments")


def main(argv=None):
    parser = SafeParser(description="Local read-only UI observation; raw content stays in the output directory.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    for command in ("inventory", "observe"):
        item = commands.add_parser(command)
        item.add_argument("--exe-name", required=True)
        item.add_argument("--output", required=True)
        if command == "observe":
            item.add_argument("--pid", type=int, required=True)
            item.add_argument("--hwnd", type=int, required=True)
            item.add_argument("--stage", choices=STAGES, default="window")
            item.add_argument("--parent-hwnd", type=int)
            item.add_argument("--related-hwnd", type=int, action="append")
    try:
        args = parser.parse_args(argv)
        result = run_inventory(args) if args.command == "inventory" else run_observe(args)
    except KeyboardInterrupt:
        result = {"status": "interrupted", "read_only": True, "input_dispatched": False,
                  "model_calls": 0, "network_requests_by_wrapper": 0}
    except EngineError:
        # Fixed public error. Detailed UI errors must not cross the local boundary.
        result = {"status": "blocked", "read_only": True, "input_dispatched": False,
                  "model_calls": 0, "network_requests_by_wrapper": 0}
    except Exception:
        # Do not leak a provider response, path, or app argument through a traceback.
        result = {"status": "blocked", "read_only": True, "input_dispatched": False,
                  "model_calls": 0, "network_requests_by_wrapper": 0}
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0 if result["status"] in {"observed", "no_matching_window", "partial"} else 2


if __name__ == "__main__":
    sys.exit(main())
