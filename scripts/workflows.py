"""Deterministic workflow replay with durable at-most-once action journals."""
from __future__ import annotations
import copy
import json
import threading
import time
import uuid

from storage import StoreError, exe_name, identifier, validate_workflow

DONE = {"verified", "executed", "skipped_confirmed"}
ATTEMPTED = {"started", "needs_review", "not_delivered"}


def _binding(value, workflow):
    if not isinstance(value, dict):
        raise StoreError("invalid_binding")
    result = {key: value.get(key) for key in ("pid", "hwnd", "exe_name")}
    for key in ("pid", "hwnd"):
        if type(result[key]) is not int or not 1 <= result[key] <= 0x7FFFFFFFFFFFFFFF:
            raise StoreError("invalid_binding")
    exe_name(result["exe_name"])
    if result["exe_name"].casefold() != workflow["target"]["exe_name"].casefold():
        raise StoreError("binding_application_mismatch")
    return result


def _parameters(workflow, supplied):
    values = dict(workflow["parameters"])
    if supplied is not None:
        if not isinstance(supplied, dict) or set(supplied) - set(values):
            raise StoreError("invalid_parameters")
        values.update(supplied)
    if any(not isinstance(value, str) or "\x00" in value or len(value.encode("utf-8")) > 10000 for value in values.values()):
        raise StoreError("invalid_parameters")
    return values


def _action_result(value):
    if not isinstance(value, dict) or type(value.get("input_dispatched")) not in (bool, type(None)):
        return {"status": "uncertain", "input_dispatched": None, "effect_verification": "unavailable"}
    result = {key: copy.deepcopy(value[key]) for key in ("status", "input_dispatched", "effect_verification", "receipt",
        "diagnostic", "delivery", "after_snapshot_available") if key in value}
    result.setdefault("input_dispatched", None)
    result.setdefault("effect_verification", "unavailable")
    if result.get("status") not in {"completed", "executed_unverified", "not_delivered", "uncertain", "blocked",
                                  "interrupted", "verification_failed"}:
        result.update(status="uncertain", input_dispatched=None)
    if result.get("status") == "completed" and result.get("input_dispatched") is False and result.get("effect_verification") != "matched":
        result.update(status="uncertain", input_dispatched=None)
    return result


class Runner:
    def __init__(self, store, adapter_factory, on_event=None):
        self.store = store
        self.adapter_factory = adapter_factory
        self.on_event = on_event
        self._cancel = threading.Event()

    def cancel(self):
        """Stop before the next action; an already-started action must still be observed."""
        self._cancel.set()

    def _publish(self, run):
        run["completed_steps"] = sum(entry["status"] in DONE for entry in run["steps"])
        run["task_verified"] = (run["status"] == "completed" and all(entry["status"] in {"verified", "skipped_confirmed"} for entry in run["steps"]))
        self.store._save_run(run)
        if self.on_event:
            event = {key: run[key] for key in ("id", "status", "current_step_id", "completed_steps", "total_steps", "task_verified")}
            try:
                self.on_event(event)
            except Exception:
                # A display failure cannot change dispatch receipts or cause replay.
                pass

    def _identity(self, adapter, source):
        identity_method = getattr(adapter, "identity", None)
        if identity_method is None:
            if "target_identity" in source:
                raise StoreError("target_identity_unavailable")
            return None  # Pure test adapters may model only the action boundary.
        identity = identity_method()
        if not isinstance(identity, dict) or set(identity) != {"process_started_utc_ticks", "main_window"}:
            raise StoreError("target_identity_unavailable")
        lifetime, window = identity["process_started_utc_ticks"], identity["main_window"]
        if (not isinstance(lifetime, str) or not lifetime or len(lifetime) > 80
                or not isinstance(window, dict) or set(window) != {"pid", "hwnd", "exe_name", "title", "class_name"}
                or any(window.get(key) != source["binding"][key] for key in ("pid", "hwnd", "exe_name"))
                or any(not isinstance(window[key], str) or "\x00" in window[key] or len(window[key]) > 8192 for key in ("title", "class_name"))):
            raise StoreError("target_identity_unavailable")
        saved = source.get("target_identity")
        if saved is not None and saved != identity:
            raise StoreError("target_identity_changed")
        if saved is None and any(entry["attempts"] for entry in source["steps"]):
            raise StoreError("prior_target_identity_missing")
        return copy.deepcopy(identity)

    def _open_adapter(self, run):
        adapter = self.adapter_factory(run["binding"], self.store.run_directory(run["id"]))
        identity = self._identity(adapter, run)
        if identity is not None and "target_identity" not in run:
            run["target_identity"] = identity
            self._publish(run)  # Identity is durable before preparation or any input.
        return adapter

    def _continuation(self, run):
        child_id = identifier(run["repair_continuation"])
        with self.store.lock("run-" + child_id):
            child = self.store._activate_reserved_run(child_id)
        if child.get("repair_fork_of") != run["id"]:
            raise StoreError("invalid_repair_continuation")
        return child_id

    def start(self, workflow_id, binding, parameters=None, *, expected_revision=None):
        self._cancel.clear()
        workflow = self.store.load(workflow_id)
        if expected_revision is not None and (type(expected_revision) is not int
                                               or workflow["revision"] != expected_revision):
            raise StoreError("workflow_revision_changed")
        binding = _binding(binding, workflow)
        values = _parameters(workflow, parameters)
        run_id = uuid.uuid4().hex
        with self.store.lock("target-" + str(binding["pid"]) + "-" + str(binding["hwnd"])):
            with self.store.lock("run-" + run_id):
                run = {"schema_version": 1, "id": run_id, "workflow_id": workflow["id"],
                    "workflow_revision": workflow["revision"], "name": workflow["name"], "workflow": workflow,
                    "parameters": values, "binding": binding, "status": "running", "current_step_id": None,
                    "completed_steps": 0, "total_steps": len(workflow["steps"]), "task_verified": False,
                    "created_at": time.time(), "steps": [{"id": step["id"], "status": "pending",
                        "input_dispatched": False, "attempts": 0} for step in workflow["steps"]]}
                self._publish(run)
                return self._drive(run)

    def resume(self, run_id, binding, confirm_step_id=None):
        self._cancel.clear()
        identifier(run_id)
        if confirm_step_id is not None:
            identifier(confirm_step_id)
        with self.store.lock("run-" + run_id):
            run = self.store.load_run(run_id)
            workflow = validate_workflow(run["workflow"])
            binding = _binding(binding, workflow)
            if run["binding"] != binding:
                raise StoreError("resume_binding_mismatch")
            _parameters(workflow, run["parameters"])
            if "repair_continuation" in run:
                return self.resume(self._continuation(run), binding, confirm_step_id=confirm_step_id)
            if run["status"] in {"completed", "completed_unverified"}:
                if confirm_step_id is not None:
                    raise StoreError("no_pending_confirmation")
                return run
            if confirm_step_id is not None:
                if run.get("diagnostic") == "completed_prefix_condition_changed":
                    # Confirming a later manual/opaque action could otherwise remove
                    # the failed checkpoint before it is read again. Require a plain
                    # resume with the condition restored first, even for CLI callers.
                    raise StoreError("completed_prefix_condition_changed")
                pending = next((entry for entry in run["steps"] if entry["status"] not in DONE), None)
                if pending is None or pending["id"] != confirm_step_id or pending["status"] not in ATTEMPTED:
                    raise StoreError("confirmation_step_mismatch")
                pending.update(status="skipped_confirmed", confirmation="user_confirmed_current_result",
                               confirmed_at=time.time())
                # Input delivery is unchanged: a human result confirmation is not a dispatch report.
            with self.store.lock("target-" + str(binding["pid"]) + "-" + str(binding["hwnd"])):
                run["status"] = "running"
                self._publish(run)
                return self._drive(run, recheck_prefix=True)

    def fork_repaired(self, run_id, binding):
        """Continue an explicitly repaired revision without replaying completed inputs."""
        self._cancel.clear()
        identifier(run_id)
        with self.store.lock("run-" + run_id):
            previous = self.store.load_run(run_id)
            binding = _binding(binding, previous["workflow"])
            if binding != previous["binding"]:
                raise StoreError("resume_binding_mismatch")
            if "repair_continuation" in previous:
                return self.resume(self._continuation(previous), binding)
            pending_index = next((i for i, entry in enumerate(previous["steps"]) if entry["status"] not in DONE), None)
            if pending_index is None:
                raise StoreError("no_pending_repair")
            pending = previous["steps"][pending_index]
            if pending["input_dispatched"] is not False:
                raise StoreError("uncertain_step_cannot_be_repaired")
            latest = self.store.load(previous["workflow_id"])
            if latest["revision"] <= previous["workflow_revision"]:
                raise StoreError("new_revision_required")
            if (latest["target"] != previous["workflow"]["target"]
                    or set(latest["parameters"]) != set(previous["parameters"])
                    or len(latest["steps"]) <= pending_index):
                raise StoreError("repair_scope_changed")
            def semantic(step):
                return {key: value for key, value in step.items() if key not in {"label", "recognition"}}
            for old, new in zip(previous["workflow"]["steps"][:pending_index], latest["steps"][:pending_index]):
                if semantic(old) != semantic(new):
                    raise StoreError("completed_prefix_changed")
            if latest["steps"][pending_index]["id"] != pending["id"]:
                raise StoreError("repair_step_identity_changed")
            if any(entry["attempts"] or entry["input_dispatched"] is not False for entry in previous["steps"][pending_index + 1:]):
                raise StoreError("later_step_already_attempted")
            with self.store.lock("target-" + str(binding["pid"]) + "-" + str(binding["hwnd"])):
                adapter = self.adapter_factory(binding, self.store.run_directory(run_id))
                identity = self._identity(adapter, previous)
                new_id = uuid.uuid4().hex
                run = {"schema_version": 1, "id": new_id, "workflow_id": latest["id"],
                    "workflow_revision": latest["revision"], "name": latest["name"], "workflow": latest,
                    "parameters": _parameters(latest, previous["parameters"]), "binding": binding,
                    "status": "running", "current_step_id": pending["id"], "completed_steps": pending_index,
                    "total_steps": len(latest["steps"]), "task_verified": False, "created_at": time.time(),
                    "repair_fork_of": run_id, "repair_prefix_pending": pending_index,
                    "steps": copy.deepcopy(previous["steps"][:pending_index]) +
                        [{"id": step["id"], "status": "pending", "input_dispatched": False, "attempts": 0}
                         for step in latest["steps"][pending_index:]]}
                if identity is not None:
                    run["target_identity"] = identity
                self.store._reserve_run(run)
                previous["repair_continuation"] = new_id
                self._publish(previous)  # Link is durable before the child can execute anything.
                with self.store.lock("run-" + new_id):
                    run = self.store._activate_reserved_run(new_id)
                    self._publish(run)
                    return self._drive(run)

    def _runtime_step(self, run, step, entry):
        runtime = copy.deepcopy(step)
        if runtime["target"]["kind"] != "manual":
            runtime["target"].setdefault("window", {key: run["workflow"]["target"][key] for key in ("title", "class_name")})
        value = run["parameters"][step["parameter"]] if "parameter" in step else step.get("value")
        if "parameter" in step and runtime["expect"]["kind"] == "value" and "target" not in runtime["expect"]:
            runtime["expect"]["value"] = value
        if "prepared" in entry:
            runtime["_prepared"] = copy.deepcopy(entry["prepared"])
        return runtime, value

    def _check(self, adapter, step, value):
        if step["expect"]["kind"] in {"none", "manual"}:
            return {"matched": None}
        try:
            result = adapter.check(step, value)
            if not isinstance(result, dict) or type(result.get("matched")) not in (bool, type(None)):
                return {"matched": None, "diagnostic": "invalid_check_response"}
            return {key: copy.deepcopy(result[key]) for key in ("matched", "diagnostic") if key in result}
        except (Exception, KeyboardInterrupt):
            return {"matched": None, "diagnostic": "check_unavailable"}

    def _review(self, run, entry, reason):
        entry.update(status="needs_review", reason=reason)
        run["status"] = "needs_review"
        self._publish(run)
        return run

    def _condition_key(self, run, target, kind):
        """Only merge declarations with the same explicit window and locator."""
        if not isinstance(target, dict) or not target.get("locator"):
            return None
        window = target.get("window") or {key: run["workflow"]["target"][key]
                                         for key in ("title", "class_name")}
        return kind, json.dumps({"window": window, "locator": target["locator"]}, sort_keys=True)

    def _recheck_prefix(self, run, adapter):
        end = run.get("repair_prefix_pending")
        completed_end = next((i for i, entry in enumerate(run["steps"]) if entry["status"] not in DONE),
                             len(run["steps"]))
        if end is None:
            end = completed_end
        if type(end) is not int or not 0 <= end <= len(run["steps"]):
            raise StoreError("invalid_repair_prefix")
        end = max(end, completed_end)
        if any(entry["status"] not in DONE and not ("repair_prefix_pending" in run
                and entry["status"] == "needs_review"
                and entry.get("reason") == "completed_prefix_condition_changed") for entry in run["steps"][:end]):
            raise StoreError("invalid_repair_prefix")
        checkpoints = {}
        writes = {"set-value": "value", "type": "value", "select": "value", "clear": "value",
                  "check": "checked", "uncheck": "checked"}

        def invalidate(step):
            if step["action"] == "wait":
                return
            property_kind = writes.get(step["action"])
            if property_kind:
                checkpoints.pop(self._condition_key(run, step["target"], property_kind), None)
            else:
                # A submitted button, navigation or human action can consume/reset
                # old fields. Its explicit completion condition establishes new state.
                checkpoints.clear()

        for step, entry in zip(run["workflow"]["steps"][:end], run["steps"][:end]):
            invalidate(step)
            kind = step["expect"]["kind"]
            if kind not in {"value", "checked"}:
                continue
            runtime, value = self._runtime_step(run, step, entry)
            target = runtime["expect"].get("target", runtime["target"])
            key = self._condition_key(run, target, kind)
            if key is not None:
                checkpoints[key] = runtime, value, entry

        if end < len(run["steps"]):
            pending = run["steps"][end]
            if pending["attempts"] and pending["input_dispatched"] is not False:
                # An uncertain/already delivered action may have superseded an older
                # condition. Only observe that pending action; never replay its prefix.
                invalidate(run["workflow"]["steps"][end])
        for runtime, value, entry in checkpoints.values():
            if self._cancel.is_set():
                run["status"] = "cancelled"
                self._publish(run)
                return False
            checked = self._check(adapter, runtime, value)
            entry["resume_recheck"] = checked
            if checked["matched"] is not True:
                # Preserve the completed action's receipt and status. A changed
                # checkpoint is not a new attempt and cannot be confirmed as one.
                entry["reason"] = "completed_prefix_condition_changed"
                run.update(status="needs_review", diagnostic="completed_prefix_condition_changed",
                           current_step_id=entry["id"])
                self._publish(run)
                return False
            if entry.get("reason") == "completed_prefix_condition_changed":
                if entry["status"] == "needs_review" and "repair_prefix_pending" in run:
                    entry.update(status="skipped_confirmed" if entry.get("confirmation") else "verified")
                entry.pop("reason")
        for entry in run["steps"][:end]:
            if entry.get("reason") == "completed_prefix_condition_changed":
                # Compatibility with 0.1.1 repair journals, which changed a
                # completed prefix entry into needs_review under this marker.
                if entry["status"] == "needs_review" and "repair_prefix_pending" in run:
                    entry.update(status="skipped_confirmed" if entry.get("confirmation") else "verified")
                entry.pop("reason")
        if "repair_prefix_pending" in run:
            del run["repair_prefix_pending"]
        if run.get("diagnostic") == "completed_prefix_condition_changed":
            run.pop("diagnostic")
        self._publish(run)
        return True

    def _drive(self, run, *, recheck_prefix=False):
        try:
            adapter = self._open_adapter(run)
        except (Exception, KeyboardInterrupt) as error:
            run.update(status="needs_review", diagnostic=str(error) if isinstance(error, StoreError) else "adapter_unavailable")
            self._publish(run)
            return run
        if (recheck_prefix or "repair_prefix_pending" in run) and not self._recheck_prefix(run, adapter):
            return run
        for step, entry in zip(run["workflow"]["steps"], run["steps"]):
            if entry["status"] in DONE:
                continue
            if self._cancel.is_set():
                run["status"] = "cancelled"
                self._publish(run)
                return run
            run["current_step_id"] = step["id"]
            runtime, value = self._runtime_step(run, step, entry)
            if entry["status"] in ATTEMPTED:
                # A prior process may have died after dispatch. Only read or ask about this step.
                result = self._check(adapter, runtime, value)
                entry["check"] = result
                if result["matched"] is True:
                    entry.update(status="verified", verification="read_after_prior_attempt")
                    self._publish(run)
                    continue
                return self._review(run, entry, "prior_attempt_requires_review")
            if entry["status"] not in {"pending", "prepared"}:
                raise StoreError("invalid_run_step_state")
            if step["action"] == "manual" or step["target"]["kind"] == "manual" and step["action"] != "wait":
                return self._review(run, entry, "manual_step")
            if step["target"]["kind"] == "region" and step["target"].get("reviewed") is not True:
                return self._review(run, entry, "region_requires_review")
            if "prepared" not in entry or step["action"] != "wait":
                try:
                    prepare = getattr(adapter, "prepare", None)
                    prepared = prepare(runtime, value) if prepare else {}
                    if not isinstance(prepared, dict) or len(json.dumps(prepared, allow_nan=False).encode()) > 1024 * 1024:
                        raise ValueError()
                    entry.update(status="prepared", prepared=prepared)
                    self._publish(run)
                    runtime, value = self._runtime_step(run, step, entry)
                except (Exception, KeyboardInterrupt):
                    # Unattempted input always gets a current baseline on entry to this
                    # drive. A prior prepared-then-cancelled baseline cannot prove a later
                    # input's result. Read-only waits keep their original observation period;
                    # started/uncertain actions take the readback-only branch above.
                    entry.update(reason="preparation_unavailable")
                    run["status"] = "needs_review"
                    self._publish(run)
                    return run
            if step["action"] == "wait":
                end = time.monotonic() + step.get("timeout_seconds", 30)
                while True:
                    checked = self._check(adapter, runtime, value)
                    entry["check"] = checked
                    if checked["matched"] is True:
                        entry.update(status="verified", verification="wait_condition")
                        self._publish(run)
                        break
                    if self._cancel.wait(0.25):
                        run["status"] = "cancelled"
                        self._publish(run)
                        return run
                    if time.monotonic() >= end:
                        return self._review(run, entry, "wait_condition_unverified")
                continue
            if self._cancel.is_set():
                run["status"] = "cancelled"
                self._publish(run)
                return run
            entry.update(status="started", input_dispatched=None, attempts=entry["attempts"] + 1,
                         started_at=time.time())
            self._publish(run)  # Must finish durably before invoking any mutating adapter call.
            try:
                result = _action_result(adapter.execute(runtime, value))
            except (Exception, KeyboardInterrupt):
                result = {"status": "uncertain", "input_dispatched": None,
                          "effect_verification": "unavailable", "diagnostic": "execution_interrupted"}
            entry.update(result=result, input_dispatched=result["input_dispatched"])
            self._publish(run)  # The dispatch result survives a later failed or interrupted readback.
            if (result["input_dispatched"] is True and result.get("status") == "uncertain"
                    and result.get("delivery") in {"delivered_verified", "delivered_unverified"}
                    and result.get("diagnostic") == "snapshot_unavailable"
                    and result.get("after_snapshot_available") is False
                    and step["expect"]["kind"] in {"file", "value", "checked", "exists"}):
                # A menu can close after a known-delivered export and make its own
                # after-snapshot unavailable. Read the declared independent result
                # once with the original baseline; never send the action again or
                # rewrite the uncertain engine receipt as a successful delivery.
                checked = self._check(adapter, runtime, value)
                entry["check"] = checked
                if checked["matched"] is True:
                    entry.update(status="verified", verification="independent_postcondition_after_snapshot_unavailable")
                    self._publish(run)
                    continue
                return self._review(run, entry, "postcondition_unverified")
            if result["input_dispatched"] is None or result.get("status") in {"uncertain", "interrupted"}:
                return self._review(run, entry, "delivery_uncertain")
            if result.get("status") in {"blocked", "not_delivered", "verification_failed"}:
                return self._review(run, entry, "action_not_completed")
            kind = step["expect"]["kind"]
            if kind == "none":
                if result["input_dispatched"] is True or result.get("status") == "completed":
                    entry.update(status="executed", verification="delivery_only")
                    self._publish(run)
                    continue
                return self._review(run, entry, "delivery_unverified")
            if kind == "manual":
                return self._review(run, entry, "manual_result_confirmation")
            # Adapter's fresh control readback may already prove a value condition. Other kinds
            # (file, existence, query markers) need their own independent postcondition check.
            expected = runtime["expect"]
            equivalent_value = (kind == "value" and step["action"] in {"set-value", "type", "select"}
                                and value is not None and expected.get("value", value) == value)
            equivalent_checked = (kind == "checked" and step["action"] in {"check", "uncheck"}
                                  and type(expected.get("value")) is bool
                                  and expected["value"] is (step["action"] == "check"))
            if (equivalent_value or equivalent_checked) and "target" not in expected and result.get("status") == "completed" and result.get("effect_verification") == "matched":
                checked = {"matched": True, "diagnostic": "adapter_fresh_control_readback"}
            else:
                checked = self._check(adapter, runtime, value)
            entry["check"] = checked
            if checked["matched"] is True:
                entry.update(status="verified", verification="postcondition")
                self._publish(run)
            else:
                return self._review(run, entry, "postcondition_unverified")
        run["current_step_id"] = None
        run["status"] = "completed_unverified" if any(entry["status"] == "executed" for entry in run["steps"]) else "completed"
        self._publish(run)
        return run
