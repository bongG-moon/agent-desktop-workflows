"""Private local storage for independent workflows, recordings, and execution journals."""
from __future__ import annotations
from contextlib import contextmanager
import copy
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid

ENGINE = Path(__file__).resolve().parents[1] / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))
from local_core import validate_local_directory as _validate_existing_directory

MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_STEPS = 500
ID = re.compile(r"[a-f0-9]{32}")
PARAMETER = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
ACTIONS = {"click", "double-click", "right-click", "set-value", "type", "select", "expand",
           "collapse", "check", "uncheck", "focus", "clear", "scroll", "wait", "manual"}


class StoreError(RuntimeError):
    """A fixed diagnostic code, never arbitrary file content or provider messages."""


def _fail(code):
    raise StoreError(code)


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        _fail("invalid_identifier")
    return value


def _text(value, maximum=1024, *, empty=True):
    if (not isinstance(value, str) or len(value.encode("utf-8")) > maximum
            or "\x00" in value or (not empty and not value.strip())):
        _fail("invalid_text")
    return value


def exe_name(value):
    _text(value, 200, empty=False)
    if (not value.lower().endswith(".exe") or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
            or value.startswith(("-", ".")) or value.strip() != value):
        _fail("invalid_executable")
    return value


def _keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        _fail("invalid_schema")


def _number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        _fail("invalid_number")


def _locator(value, *, ancestor=False):
    _keys(value, {"automation_id", "name", "role", "ancestors"} if not ancestor else {"automation_id", "name", "role"})
    for key in ("automation_id", "name", "role"):
        if key in value:
            _text(value[key], 1024, empty=False)
    if not value.get("automation_id") and not value.get("name"):
        _fail("missing_target_locator")
    ancestors = value.get("ancestors", [])
    if not isinstance(ancestors, list) or len(ancestors) > 16:
        _fail("invalid_ancestors")
    for item in ancestors:
        _locator(item, ancestor=True)


def _target(value):
    _keys(value, {"kind", "locator", "window", "point", "recorded_bounds", "reviewed"}, {"kind"})
    if not isinstance(value["kind"], str) or value["kind"] not in {"element", "region", "manual"}:
        _fail("invalid_target_kind")
    if value["kind"] != "manual":
        _locator(value.get("locator"))
    elif "locator" in value and value["locator"]:
        _locator(value["locator"])
    if "window" in value:
        _keys(value["window"], {"title", "class_name"})
        for item in value["window"].values():
            _text(item, 2048)
    if "reviewed" in value and type(value["reviewed"]) is not bool:
        _fail("invalid_review_flag")
    if value["kind"] == "region":
        _keys(value.get("point"), {"x_ratio", "y_ratio"}, {"x_ratio", "y_ratio"})
        for coordinate in value["point"].values():
            _number(coordinate, 0, 1)
        _keys(value.get("recorded_bounds"), {"x", "y", "width", "height"}, {"x", "y", "width", "height"})
        for key, number in value["recorded_bounds"].items():
            _number(number, 0.01 if key in {"width", "height"} else -1000000, 1000000)
    elif "point" in value or "recorded_bounds" in value:
        _fail("coordinates_require_region")


def validate_workflow(workflow):
    """Validate without evaluating expressions, regexes, shell commands, or templates."""
    value = copy.deepcopy(workflow)
    _keys(value, {"schema_version", "id", "name", "revision", "target", "parameters", "steps",
                  "recording_id", "created_at", "updated_at"},
          {"schema_version", "id", "name", "revision", "target", "parameters", "steps"})
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        _fail("unsupported_schema")
    identifier(value["id"])
    _text(value["name"], 300, empty=False)
    if type(value["revision"]) is not int or not 1 <= value["revision"] <= 999999:
        _fail("invalid_revision")
    _keys(value["target"], {"exe_name", "title", "class_name"}, {"exe_name", "title", "class_name"})
    exe_name(value["target"]["exe_name"])
    _text(value["target"]["title"], 2048)
    _text(value["target"]["class_name"], 1024)
    if not isinstance(value["parameters"], dict) or len(value["parameters"]) > 100:
        _fail("invalid_parameters")
    for key, default in value["parameters"].items():
        if not isinstance(key, str) or not PARAMETER.fullmatch(key):
            _fail("invalid_parameter_name")
        _text(default, 10000)
    if not isinstance(value["steps"], list) or not 1 <= len(value["steps"]) <= MAX_STEPS:
        _fail("invalid_step_count")
    seen = set()
    for step in value["steps"]:
        _keys(step, {"id", "label", "action", "target", "value", "parameter", "headed", "expect",
                     "recognition", "direction", "amount", "timeout_seconds", "selection"},
              {"id", "label", "action", "target", "expect", "recognition"})
        identifier(step["id"])
        if step["id"] in seen:
            _fail("duplicate_step_id")
        seen.add(step["id"])
        _text(step["label"], 500, empty=False)
        if not isinstance(step["action"], str) or step["action"] not in ACTIONS:
            _fail("unsupported_action")
        _target(step["target"])
        if step["target"]["kind"] == "region" and step["action"] not in {"click", "right-click", "double-click"}:
            _fail("unsupported_region_action")
        if step["target"]["kind"] == "manual" and step["action"] not in {"manual", "wait"}:
            _fail("manual_target_requires_manual_action")
        if "parameter" in step:
            if not isinstance(step["parameter"], str) or step["parameter"] not in value["parameters"]:
                _fail("unknown_parameter")
        if "value" in step:
            _text(step["value"], 10000)
        if step["action"] in {"set-value", "type", "select"} and not ("value" in step or "parameter" in step):
            _fail("missing_action_value")
        if "headed" in step and type(step["headed"]) is not bool:
            _fail("invalid_headed")
        if "timeout_seconds" in step:
            _number(step["timeout_seconds"], 0.1, 300)
        if "direction" in step and (not isinstance(step["direction"], str) or step["direction"] not in {"up", "down", "left", "right"}):
            _fail("invalid_scroll")
        if "amount" in step and (type(step["amount"]) is not int or not 1 <= step["amount"] <= 10):
            _fail("invalid_scroll")
        expect = step["expect"]
        _keys(expect, {"kind", "value", "path", "target"}, {"kind"})
        if not isinstance(expect["kind"], str) or expect["kind"] not in {"value", "checked", "manual", "none", "file", "exists"}:
            _fail("invalid_expectation")
        if step["action"] == "wait" and expect["kind"] == "none":
            _fail("wait_condition_required")
        if "value" in expect:
            if expect["kind"] == "checked":
                if type(expect["value"]) is not bool:
                    _fail("invalid_checked_expectation")
            else:
                _text(expect["value"], 10000)
        if "path" in expect:
            _text(expect["path"], 32767, empty=False)
            path = Path(expect["path"])
            if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
                _fail("local_expectation_path_required")
        if expect["kind"] == "file" and "path" not in expect:
            _fail("missing_expectation_path")
        if "target" in expect:
            _target(expect["target"])
        recognition = step["recognition"]
        _keys(recognition, {"target", "action", "result", "reason"}, {"target", "action", "result"})
        for item in recognition.values():
            _text(item, 1000)
        if "selection" in step:
            # Recording evidence describes the original user's choice. It is
            # never a replay receipt or permission to skip result verification.
            selection = step["selection"]
            _keys(selection, {"source", "value", "result", "observed_value"},
                  {"source", "value", "result"})
            if (selection["source"] != "option_click" or not isinstance(selection["result"], str)
                    or selection["result"] not in {"matched", "mismatch", "unavailable"}):
                _fail("invalid_selection_evidence")
            _text(selection["value"], 10000, empty=False)
            if step["action"] != "set-value" or step["target"]["kind"] != "element":
                _fail("selection_requires_value_action")
            if selection["result"] == "unavailable":
                if "observed_value" in selection:
                    _fail("invalid_selection_evidence")
            else:
                _text(selection.get("observed_value"), 10000, empty=False)
                if (selection["observed_value"] == selection["value"]) != (selection["result"] == "matched"):
                    _fail("invalid_selection_evidence")
    if "recording_id" in value:
        identifier(value["recording_id"])
    for key in ("created_at", "updated_at"):
        if key in value:
            _number(value[key], 0, 100000000000)
    return value


def _directory(path, create=False):
    path = Path(path)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        _fail("local_absolute_directory_required")
    path = Path(os.path.abspath(path))
    try:
        if create and not path.exists():
            missing, ancestor = [], path
            while not ancestor.exists():
                missing.append(ancestor)
                ancestor = ancestor.parent
            _validate_existing_directory(ancestor)
            for item in reversed(missing):
                item.mkdir(exist_ok=True)
                _validate_existing_directory(item)
        return _validate_existing_directory(path)
    except StoreError:
        raise
    except Exception:
        _fail("unsafe_local_directory")


def _regular(path, *, missing=False):
    _directory(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return
        _fail("local_file_not_found")
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 1024 or not path.is_file():
        _fail("unsafe_local_file")
    if info.st_size > MAX_JSON_BYTES:
        _fail("local_file_too_large")
    return info


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _fail("duplicate_json_key")
        result[key] = value
    return result


def read_json(path):
    path = Path(path)
    _regular(path)
    try:
        data = path.read_bytes()
        if len(data) > MAX_JSON_BYTES:
            _fail("local_file_too_large")
        value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: _fail("invalid_json_number"))
        if not isinstance(value, dict):
            _fail("invalid_json_object")
        return value
    except StoreError:
        raise
    except Exception:
        _fail("invalid_json_file")


def _write(path, value, *, exclusive=False):
    _directory(path.parent)
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    if len(data) > MAX_JSON_BYTES:
        _fail("local_file_too_large")
    if exclusive:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return
    _regular(path, missing=True)
    temporary = path.parent / ("." + uuid.uuid4().hex + ".tmp")
    created = None
    try:
        with temporary.open("xb") as stream:
            created = os.fstat(stream.fileno())
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # A Windows reader can briefly deny replacement of the run journal.
        # Retry only the rename of these already flushed bytes under the lock.
        for attempt in range(6):
            if not os.path.samestat(created, _regular(temporary)):
                _fail("temporary_file_changed")
            _regular(path, missing=True)
            try:
                os.replace(temporary, path)
                return
            except OSError as error:
                if attempt == 5 or getattr(error, "winerror", None) not in (5, 32, 33):
                    raise
                time.sleep(0.025 * 2 ** attempt)
    finally:
        if created is not None:
            remaining = _regular(temporary, missing=True)
            if remaining is not None and os.path.samestat(created, remaining):
                temporary.unlink()


class Store:
    def __init__(self, root=None):
        if root is None:
            base = os.environ.get("LOCALAPPDATA")
            if not base:
                _fail("local_appdata_required")
            root = Path(base) / "AgentDesktopWorkflows"
        self.root = _directory(root, create=True)
        for name in ("workflows", "recordings", "runs", "locks"):
            _directory(self.root / name, create=True)

    @contextmanager
    def lock(self, name):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", name):
            _fail("invalid_lock")
        path = self.root / "locks" / (name + ".lock")
        _regular(path, missing=True)
        stream = path.open("a+b")
        locked = False
        try:
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _fail("resource_busy")
            locked = True
            yield
        finally:
            if locked:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def load(self, workflow_id, revision=None):
        folder = _directory(self.root / "workflows" / identifier(workflow_id))
        if revision is None:
            revisions = [int(p.stem) for p in folder.iterdir() if re.fullmatch(r"[0-9]{6}\.json", p.name)]
            if not revisions:
                _fail("workflow_not_found")
            revision = max(revisions)
        if type(revision) is not int or not 1 <= revision <= 999999:
            _fail("invalid_revision")
        value = validate_workflow(read_json(folder / (str(revision).zfill(6) + ".json")))
        if value["id"] != workflow_id or value["revision"] != revision:
            _fail("workflow_identity_mismatch")
        return value

    def save(self, workflow, expected_revision=None):
        value = copy.deepcopy(workflow)
        value.setdefault("id", uuid.uuid4().hex)
        value.setdefault("schema_version", 1)
        value.setdefault("revision", 1)
        value.setdefault("parameters", {})
        value = validate_workflow(value)
        folder = self.root / "workflows" / value["id"]
        with self.lock("workflow-" + value["id"]):
            if folder.exists():
                latest = self.load(value["id"])
                required = value["revision"] if expected_revision is None else expected_revision
                if type(required) is not int or latest["revision"] != required:
                    _fail("revision_conflict")
                value["revision"] = latest["revision"] + 1
                value["created_at"] = latest["created_at"]
            else:
                if expected_revision not in (None, 0) or value["revision"] != 1:
                    _fail("revision_conflict")
                _directory(folder, create=True)
                value["created_at"] = time.time()
            if "recording_id" in value:
                _regular(self.root / "recordings" / (value["recording_id"] + ".json"))
            value["updated_at"] = time.time()
            value = validate_workflow(value)
            _write(folder / (str(value["revision"]).zfill(6) + ".json"), value, exclusive=True)
        return value

    def import_recording(self, path):
        original = read_json(Path(path))
        value = copy.deepcopy(original.get("workflow", original))
        if not isinstance(value, dict):
            _fail("invalid_recording")
        value.pop("id", None)
        value.pop("recording_id", None)
        value.update(id=uuid.uuid4().hex, schema_version=1, revision=1)
        value.setdefault("parameters", {})
        value = validate_workflow(value)
        recording_id = uuid.uuid4().hex
        _write(self.root / "recordings" / (recording_id + ".json"), original, exclusive=True)
        value["recording_id"] = recording_id
        return self.save(value)

    def list_workflows(self):
        folder = _directory(self.root / "workflows")
        values = [self.load(p.name) for p in folder.iterdir() if ID.fullmatch(p.name)]
        return sorted(values, key=lambda item: item.get("updated_at", 0), reverse=True)

    def run_directory(self, run_id, create=False):
        return _directory(self.root / "runs" / identifier(run_id), create=create)

    def load_run(self, run_id):
        value = read_json(self.run_directory(run_id) / "run.json")
        if value.get("id") != run_id or value.get("schema_version") != 1:
            _fail("invalid_run")
        workflow = validate_workflow(value.get("workflow"))
        if workflow["id"] != value.get("workflow_id") or workflow["revision"] != value.get("workflow_revision"):
            _fail("run_workflow_mismatch")
        if (not isinstance(value.get("steps"), list) or len(value["steps"]) != len(workflow["steps"])
                or any(not isinstance(s, dict) for s in value["steps"])
                or [s.get("id") for s in value["steps"]] != [s["id"] for s in workflow["steps"]]):
            _fail("invalid_run_steps")
        done = {"verified", "executed", "skipped_confirmed"}
        for step in value["steps"]:
            if (not isinstance(step.get("status"), str) or step.get("status") not in done | {"pending", "prepared", "started", "needs_review", "not_delivered"}
                    or type(step.get("input_dispatched")) not in (bool, type(None))
                    or type(step.get("attempts")) is not int or not 0 <= step["attempts"] <= 1):
                _fail("invalid_run_steps")
            if step["status"] in {"pending", "prepared"} and (step["attempts"] != 0 or step["input_dispatched"] is not False):
                _fail("inconsistent_pending_run_step")
            if step["status"] == "started" and step["attempts"] != 1:
                _fail("inconsistent_started_run_step")
        if not isinstance(value.get("status"), str) or value.get("status") not in {"running", "needs_review", "cancelled", "completed", "completed_unverified"}:
            _fail("invalid_run_status")
        if value["status"] in {"completed", "completed_unverified"} and not all(s["status"] in done for s in value["steps"]):
            _fail("inconsistent_completed_run")
        if value.get("current_step_id") is not None and value["current_step_id"] not in {s["id"] for s in value["steps"]}:
            _fail("invalid_current_step")
        return value

    def _save_run(self, run):
        run["updated_at"] = time.time()
        _write(self.run_directory(run["id"], create=True) / "run.json", run)

    def _reserve_run(self, run):
        # A repair link is written only after this immutable, never-executed seed exists.
        folder = self.run_directory(run["id"], create=True)
        _write(folder / "initial.json", run, exclusive=True)

    def _activate_reserved_run(self, run_id):
        folder = self.run_directory(run_id)
        journal, activated = folder / "run.json", folder / "activated.json"
        if not journal.exists():
            if activated.exists():
                _fail("reserved_run_journal_missing")
            seed = read_json(folder / "initial.json")
            if seed.get("id") != run_id:
                _fail("invalid_reserved_run")
            self._save_run(seed)
        run = self.load_run(run_id)
        if not activated.exists():
            _write(activated, {"run_id": run_id, "activated_at": time.time()}, exclusive=True)
        else:
            if read_json(activated).get("run_id") != run_id:
                _fail("invalid_reserved_run")
        return run

    def list_runs(self, workflow_id=None):
        if workflow_id is not None:
            identifier(workflow_id)
        values = []
        for path in _directory(self.root / "runs").iterdir():
            if not ID.fullmatch(path.name):
                continue
            _directory(path)
            if not (path / "run.json").exists():
                continue  # Reserved repair seeds are reached through their parent run.
            run = self.load_run(path.name)
            if workflow_id is not None and run["workflow_id"] != workflow_id:
                continue
            values.append({key: run.get(key) for key in ("id", "workflow_id", "workflow_revision", "name", "status",
                "current_step_id", "completed_steps", "total_steps", "created_at", "updated_at", "task_verified")})
        return sorted(values, key=lambda item: item.get("updated_at", 0), reverse=True)
