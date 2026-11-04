"""One observed window, one explicit action, then local readback. No network client."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

import observe
from local_core import EngineError, _Child, _identity, foreground, process_path, validate_local_directory
from recognition import derive

VERSION = "0.2.0"
SESSION_SECONDS = 120
MAX_FILE_BYTES = 16 * 1024 * 1024
ACTIONS = ("click", "double-click", "right-click", "set-value", "type", "select",
           "expand", "collapse", "check", "uncheck", "focus", "clear", "scroll")
VALUE_ACTIONS = {"set-value", "type", "select"}
HEADED_REQUIRED = {"double-click", "right-click", "type", "focus"}
REF = re.compile(r"@(s[a-z0-9]{1,64}):e([1-9][0-9]{0,8})")
DIAGNOSTICS = {"invalid_arguments", "invalid_action", "headed_required", "invalid_value",
    "invalid_ref", "invalid_session", "expired_session", "session_consumed", "snapshot_changed",
    "target_identity_changed", "target_not_listed", "snapshot_unavailable", "ref_not_in_snapshot",
    "ref_scope_mismatch", "secure_target", "ref_preflight_failed", "binary_hash_mismatch",
    "window_process_mismatch", "process_executable_mismatch", "different_windows_session",
    "reparse_directory_rejected", "reparse_file_rejected", "local_absolute_directory_required",
    "local_fixed_drive_required", "existing_output_directory_required", "windows_required"}


def _read(path):
    path = Path(path)
    validate_local_directory(path.parent)
    stat = path.lstat()
    if getattr(stat, "st_file_attributes", 0) & 1024 or path.is_symlink() or not path.is_file():
        raise EngineError("reparse_file_rejected")
    if stat.st_size > MAX_FILE_BYTES:
        raise EngineError("invalid_session")
    content = path.read_bytes()
    value = json.loads(content.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(value, dict):
        raise EngineError("invalid_session")
    return value, hashlib.sha256(content).hexdigest()


def _write(run, name, value):
    """Exclusive durable writes preserve evidence even if the next command is interrupted."""
    validate_local_directory(run)
    with (run / name).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def _ref(value):
    if not isinstance(value, str) or not REF.fullmatch(value):
        raise EngineError("invalid_ref")
    return REF.fullmatch(value)


def command(args):
    action = args.action
    if action not in ACTIONS:
        raise EngineError("invalid_action")
    _ref(args.ref)
    headed = bool(args.headed)
    if action in HEADED_REQUIRED and not headed:
        raise EngineError("headed_required")
    value = getattr(args, "value", None)
    if action in VALUE_ACTIONS:
        if (not isinstance(value, str) or len(value.encode("utf-8")) > 10000
                or "\x00" in value or (action == "select" and (not value or value.startswith("-")))):
            raise EngineError("invalid_value")
    elif value is not None:
        raise EngineError("invalid_value")
    argv = (["--headed"] if headed else []) + [action, args.ref]
    if action in VALUE_ACTIONS:
        # Timeout precedes the positional value: option-looking text is never reparsed as a flag.
        argv += ["--timeout-ms", "5000", "--", value]
    elif action == "scroll":
        direction, amount = getattr(args, "direction", None), getattr(args, "amount", None)
        if direction not in {"up", "down", "left", "right"} or type(amount) is not int or not 1 <= amount <= 10:
            raise EngineError("invalid_arguments")
        argv += ["--direction", direction, "--amount", str(amount), "--timeout-ms", "5000"]
    else:
        argv += ["--timeout-ms", "5000"]
    if action != "scroll" and (getattr(args, "direction", None) is not None or getattr(args, "amount", None) is not None):
        raise EngineError("invalid_arguments")
    return argv


def _call(arguments, run):
    # This private call receives only fixed arrays from command(), snapshot(), or get-states.
    binary = observe.verify_binary()
    validate_local_directory(run)
    child = _Child([str(binary), *arguments], env=_environment(run), cwd=run, timeout=15)
    try:
        return child.receive()
    finally:
        child.close()


def _environment(run):
    env = observe.child_environment(run)
    # A persistent session is writable by the agent; reject redirected state/temp folders.
    for key in ("TEMP", "TMP", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "AGENT_DESKTOP_HOME"):
        path = Path(env[key])
        validate_local_directory(path if path.exists() else path.parent)
    return env


def snapshot(session, run):
    raw = observe._invoke(["snapshot", "--window-id", "w-" + str(session["hwnd"]),
        "--surface", "window", "--max-depth", "12", "--include-bounds", "--timeout-ms", "10000"], run)
    observe.validate_snapshot_identity(raw, session["pid"], session["hwnd"], session["exe_name"])
    if derive(raw)["status"] != "observed":
        raise EngineError("snapshot_unavailable")
    return raw


def _same_identity(session):
    if tuple(session["identity"]) != _identity(session["pid"], session["hwnd"], session["image"]):
        raise EngineError("target_identity_changed")


def _map_path(run, snapshot_id):
    if not re.fullmatch(r"s[a-z0-9]{1,64}", snapshot_id or ""):
        raise EngineError("invalid_session")
    return run / "agent-state" / "snapshots" / snapshot_id / "refmap.json"


def _node_and_map(raw, ref, refmap, session):
    match = _ref(ref)
    if raw.get("data", {}).get("snapshot_id") != match[1]:
        raise EngineError("ref_not_in_snapshot")
    nodes = [n for n in derive(raw)["nodes"] if n.get("ref") == ref]
    if len(nodes) != 1:
        raise EngineError("ref_not_in_snapshot")
    entry = refmap.get("inner", {}).get("@e" + match[2])
    if (not isinstance(entry, dict) or entry.get("pid") != session["pid"]
            or entry.get("source_window_id") != "w-" + str(session["hwnd"])
            or entry.get("source_surface", "window") != "window"
            or not isinstance(entry.get("source_app"), str)
            or entry["source_app"].casefold() != session["exe_name"].casefold()
            or entry.get("role") != nodes[0]["role"] or entry.get("name") != nodes[0]["name"]):
        raise EngineError("ref_scope_mismatch")
    if _secure(nodes[0]) or _secure(entry):
        raise EngineError("secure_target")
    return nodes[0]


def _secure(node):
    states = node.get("states", [])
    tokens = {str(s).lower() for s in states} if isinstance(states, (dict, list)) else set()
    return bool(tokens & {"secure", "password", "protected"}) or node.get("role") == "password"


def receipt(run, status, *, input_dispatched=False, delivery="not_delivered", read_only=False, diagnostic=None):
    result = {"version": VERSION, "status": status, "read_only": read_only,
        "input_dispatched": input_dispatched, "delivery": delivery,
        "retry": "unsafe" if input_dispatched is not False else "inspect_again",
        "business_commit": "not_verified", "model_calls": 0, "network_requests_by_wrapper": 0}
    if run is not None:
        result["receipt"] = run.name
    if diagnostic:
        result["diagnostic"] = diagnostic if diagnostic in DIAGNOSTICS else "local_operation_failed"
    return result


def run_inspect(args):
    name = observe.exe_name(args.exe_name)
    image = process_path(args.pid)
    if Path(image).name.casefold() != name.casefold():
        raise EngineError("process_executable_mismatch")
    identity = _identity(args.pid, args.hwnd, image)
    run = observe.new_run(args.output)
    session = {"version": VERSION, "exe_name": name, "pid": args.pid, "hwnd": args.hwnd,
        "image": image, "identity": identity, "created_at": time.time(), "lifetime_seconds": SESSION_SECONDS}
    before_foreground = foreground()
    rows = observe.inventory(name, run)
    if not any(r["pid"] == args.pid and r["hwnd"] == args.hwnd and tuple(r["identity"]) == identity for r in rows):
        raise EngineError("target_not_listed")
    _same_identity(session)
    raw = snapshot(session, run)
    _same_identity(session)
    if before_foreground != foreground():
        raise EngineError("target_identity_changed")
    projection = derive(raw)
    _write(run, "before.json", raw)
    _write(run, "inspection.json", projection)
    snapshot_id = raw.get("data", {}).get("snapshot_id")
    refmap, map_sha = _read(_map_path(run, snapshot_id))
    refs = [n["ref"] for n in projection["nodes"] if n.get("ref")]
    # Do not issue a consumable session unless every exposed ref is bound to this window.
    for ref in refs:
        try:
            _node_and_map(raw, ref, refmap, session)
        except EngineError as exc:
            if str(exc) != "secure_target":
                raise
    if not refs:
        raise EngineError("snapshot_unavailable")
    session.update({"snapshot_id": snapshot_id, "snapshot_sha256": _read(run / "before.json")[1],
                    "refmap_sha256": map_sha, "created_at": time.time()})
    _write(run, "session.json", session)
    result = receipt(run, "ready", read_only=True)
    result["node_count"] = projection["node_count"]
    result["expires_in_seconds"] = SESSION_SECONDS
    _write(run, "inspection-receipt.json", result)
    return result


def _load_session(run, ref):
    session, _ = _read(run / "session.json")
    if (session.get("version") != VERSION or type(session.get("created_at")) not in (int, float)
            or not 0 <= time.time() - session["created_at"] <= SESSION_SECONDS
            or session.get("lifetime_seconds") != SESSION_SECONDS):
        raise EngineError("expired_session")
    observe.exe_name(session.get("exe_name"))
    _same_identity(session)
    raw, raw_sha = _read(run / "before.json")
    refmap, map_sha = _read(_map_path(run, session.get("snapshot_id")))
    if raw_sha != session.get("snapshot_sha256") or map_sha != session.get("refmap_sha256"):
        raise EngineError("snapshot_changed")
    observe.validate_snapshot_identity(raw, session["pid"], session["hwnd"], session["exe_name"])
    node = _node_and_map(raw, ref, refmap, session)
    return session, raw, node


def _delivery(raw, action, headed):
    """Do not equate a failed select/focus preparation with no side effects."""
    data = raw.get("data") if raw.get("ok") is True else raw.get("error")
    data = data if isinstance(data, dict) else {}
    disposition = data.get("disposition", {})
    value = disposition.get("delivery") if isinstance(disposition, dict) else None
    if value in {"delivered_verified", "delivered_unverified"}:
        return True, value
    if (value == "not_delivered" and disposition.get("retry") == "safe"
            and action != "select" and not headed):
        # Nested action reports may contradict the top-level envelope.
        pending = [data]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                if item.get("outcome") in {"attempted", "succeeded"} and item.get("mechanism") in {"semantic_api", "physical_synthetic"}:
                    return None, "possibly_dispatched"
                if isinstance(item.get("disposition"), dict) and item["disposition"].get("delivery") in {"delivered_verified", "delivered_unverified", "delivery_uncertain"}:
                    return None, "possibly_dispatched"
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        return False, "not_delivered"
    return None, "possibly_dispatched"


def verify_effect(before_node, after, action, value):
    """Readback of one uniquely matched control; application save/query success is separate."""
    nodes = derive(after)["nodes"]
    keys = ("role", "name", "native_id")
    matches = [node for node in nodes if all(node.get(k) == before_node.get(k) for k in keys)]
    result = {"status": "unavailable", "scope": "control_state", "business_commit": "not_verified"}
    if len(matches) != 1:
        return result
    node = matches[0]
    if not before_node.get("native_id") and node["path"] != before_node["path"]:
        return result
    result["node"] = node
    observed = node.get("value")
    expected = value if action in {"set-value", "select"} else "" if action == "clear" else None
    if expected is not None and observed is not None:
        result.update(status="matched" if observed == expected else "mismatch", scope="control_value")
    elif action == "select" and node.get("selected") is True and node.get("name") == value:
        result["status"] = "matched"
    elif action in {"check", "uncheck", "expand", "collapse", "focus"}:
        positive = {"check": "checked", "uncheck": "unchecked", "expand": "expanded", "collapse": "collapsed", "focus": "focused"}[action]
        opposite = {"check": "unchecked", "uncheck": "checked", "expand": "collapsed", "collapse": "expanded"}.get(action)
        states = node.get("states", [])
        if isinstance(states, list):
            if positive in states:
                result["status"] = "matched"
            elif opposite and opposite in states:
                result["status"] = "mismatch"
        elif isinstance(states, dict):
            key = {"uncheck": "checked", "collapse": "expanded"}.get(action, positive)
            desired = action not in {"uncheck", "collapse"}
            if type(states.get(key)) is bool:
                result["status"] = "matched" if states[key] == desired else "mismatch"
    return result


def read_missing_value(verification, after, session, run, action, value):
    """An omitted snapshot value is unknown; only a fresh scoped read can prove empty."""
    node = verification.get("node")
    if (action not in {"set-value", "select", "clear"} or not isinstance(node, dict)
            or node.get("value") is not None or not node.get("ref")):
        return verification
    ref = node["ref"]
    snapshot_id = _ref(ref)[1]
    refmap, _ = _read(_map_path(run, snapshot_id))
    _node_and_map(after, ref, refmap, session)
    _same_identity(session)
    raw = _call(["get", ref, "--property", "value"], run)
    _same_identity(session)
    _write(run, "after-value.json", raw)
    data = raw.get("data")
    if (raw.get("ok") is not True or not isinstance(data, dict)
            or data.get("ref") != ref or data.get("property") != "value"
            or not isinstance(data.get("value"), str)):
        return verification
    expected = "" if action == "clear" else value
    return dict(verification, status="matched" if data["value"] == expected else "mismatch",
                scope="control_value", node=dict(node, value=data["value"]), evidence="fresh_get_value")


def run_act(args):
    run = None
    attempted = False
    after_available = False
    dispatched, delivery = False, "not_delivered"
    try:
        argv = command(args)
        run = validate_local_directory(args.session)
        if not re.fullmatch(r"[a-f0-9]{32}", run.name):
            raise EngineError("invalid_session")
        session, before, node = _load_session(run, args.ref)
        # Atomic, permanent consumption serializes concurrent calls and survives crashes.
        try:
            _write(run, "consumed.json", {"consumed_at": time.time(), "ref": args.ref,
                "action": args.action, "headed": bool(args.headed), "one_action_only": True})
        except FileExistsError:
            raise EngineError("session_consumed") from None
        live = _call(["get", args.ref, "--property", "states"], run)
        _write(run, "preflight.json", live)
        if live.get("ok") is not True or live.get("data", {}).get("ref") != args.ref:
            raise EngineError("ref_preflight_failed")
        if _secure({"states": live.get("data", {}).get("value", [])}):
            raise EngineError("secure_target")
        _same_identity(session)
        # Validate executable/environment before recording the potential mutation boundary.
        observe.verify_binary()
        _environment(run)
        if not 0 <= time.time() - session["created_at"] <= SESSION_SECONDS:
            raise EngineError("expired_session")
        _write(run, "dispatch-started.json", {"started_at": time.time(), "action": args.action,
            "input_dispatched": None, "delivery": "possibly_dispatched", "retry": "unsafe"})
        attempted = True
        dispatched, delivery = None, "possibly_dispatched"
        raw = _call(argv, run)
        dispatched, delivery = _delivery(raw, args.action, args.headed)
        _write(run, "action.json", raw)
        # Persist the receipt before any readback that may hang or be interrupted.
        _write(run, "dispatch-receipt.json", receipt(run, "action_returned", input_dispatched=dispatched, delivery=delivery))
        _same_identity(session)
        after = snapshot(session, run)
        _same_identity(session)
        _write(run, "after.json", after)
        after_available = True
        verification = verify_effect(node, after, args.action, getattr(args, "value", None))
        verification = read_missing_value(verification, after, session, run, args.action, getattr(args, "value", None))
        _write(run, "verification.json", verification)
        if raw.get("ok") is not True:
            status = "not_delivered" if dispatched is False else "uncertain"
        elif dispatched is None:
            # Matching UI state does not repair a missing/unknown action-delivery report.
            status = "uncertain"
        elif verification["status"] == "matched":
            status = "completed"
        elif verification["status"] == "mismatch":
            status = "verification_failed"
        else:
            status = "executed_unverified" if dispatched is True else "not_delivered" if dispatched is False else "uncertain"
        result = receipt(run, status, input_dispatched=dispatched, delivery=delivery)
        result["effect_verification"] = verification["status"]
        result["after_snapshot_available"] = True
    except (Exception, KeyboardInterrupt) as exc:
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "uncertain" if attempted else "blocked"
        result = receipt(run, status, input_dispatched=dispatched, delivery=delivery,
            diagnostic=str(exc) if isinstance(exc, EngineError) else None)
        result["after_snapshot_available"] = after_available
    if run is not None:
        try:
            _write(run, "action-receipt.json", result)
        except (Exception, KeyboardInterrupt):
            pass
    return result


def main(argv=None):
    parser = observe.SafeParser(description="Local scoped action and readback; raw UI evidence remains in local files.")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=observe.SafeParser)
    inspect = sub.add_parser("inspect")
    for key in ("exe-name", "output"):
        inspect.add_argument("--" + key, required=True)
    for key in ("pid", "hwnd"):
        inspect.add_argument("--" + key, required=True, type=int)
    act = sub.add_parser("act")
    act.add_argument("--session", required=True)
    act.add_argument("--ref", required=True)
    act.add_argument("--action", required=True, choices=ACTIONS)
    act.add_argument("--value")
    act.add_argument("--headed", action="store_true")
    act.add_argument("--direction", choices=("up", "down", "left", "right"))
    act.add_argument("--amount", type=int)
    try:
        args = parser.parse_args(argv)
        if args.command == "act" and args.action == "scroll":
            args.direction = args.direction or "down"
            args.amount = 3 if args.amount is None else args.amount
        result = run_inspect(args) if args.command == "inspect" else run_act(args)
    except (Exception, KeyboardInterrupt) as exc:
        result = receipt(None, "interrupted" if isinstance(exc, KeyboardInterrupt) else "blocked",
            read_only=True, diagnostic=str(exc) if isinstance(exc, EngineError) else None)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0 if result["status"] in {"ready", "completed", "executed_unverified"} else 2


if __name__ == "__main__":
    sys.exit(main())
