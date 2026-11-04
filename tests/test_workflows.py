import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import storage
from storage import Store, StoreError, validate_workflow
from workflows import Runner

BINDING = {"pid": 100, "hwnd": 200, "exe_name": "Fixture.exe"}


def step(action="set-value", expect="value", **changes):
    result = {"id": uuid.uuid4().hex, "label": "Private control", "action": action,
        "target": {"kind": "element", "locator": {"automation_id": "Field", "role": "combobox"}},
        "expect": expect if isinstance(expect, dict) else {"kind": expect}, "recognition": {"target": "observed", "action": "supported", "result": "pending"}}
    if action in {"set-value", "type", "select"}:
        result["value"] = "N"
    result.update(changes)
    return result


def workflow(steps=None):
    return {"schema_version": 1, "id": uuid.uuid4().hex, "name": "Private workflow", "revision": 1,
        "target": {"exe_name": "Fixture.exe", "title": "Private title", "class_name": "FixtureClass"},
        "parameters": {}, "steps": steps or [step()]}


def result(status="completed", dispatched=True, effect="matched"):
    return {"status": status, "input_dispatched": dispatched, "effect_verification": effect}


class Adapter:
    def __init__(self, results=None, checks=None):
        self.results = list(results or [result()])
        self.checks = list(checks or [{"matched": True}])
        self.executions = []
        self.reads = []
        self.preparations = []
        self.before_execute = None

    def prepare(self, target, value):
        self.preparations.append((copy.deepcopy(target), value))
        return {"baseline": "pre-action", "file_existed": False}

    def execute(self, target, value):
        if self.before_execute:
            self.before_execute(target, value)
        self.executions.append((copy.deepcopy(target), value))
        outcome = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def check(self, target, value):
        self.reads.append((copy.deepcopy(target), value))
        outcome = self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class IdentityAdapter(Adapter):
    lifetime = "original-process"

    def identity(self):
        return {"process_started_utc_ticks": self.lifetime,
                "main_window": dict(BINDING, title="Private title", class_name="FixtureClass")}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve()
        def existing(path):
            path = Path(path)
            if not path.is_dir() or any(p.is_symlink() for p in [path, *path.parents]):
                raise ValueError()
            return path
        self.local = patch.object(storage, "_validate_existing_directory", existing)
        self.local.start()
        self.store = Store(self.folder / "local-store")

    def tearDown(self):
        self.local.stop()
        self.temp.cleanup()

    def save(self, value=None):
        return self.store.save(value or workflow())

    def runner(self, adapter, events=None):
        return Runner(self.store, lambda binding, directory: adapter, events)

    def test_revision_save_preserves_original_and_conflicts_stale_editor(self):
        first = self.save()
        changed = copy.deepcopy(first)
        changed["steps"][0]["value"] = "Y"
        second = self.store.save(changed, expected_revision=1)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(self.store.load(first["id"], 1)["steps"][0]["value"], "N")
        self.assertEqual(self.store.load(first["id"])["steps"][0]["value"], "Y")
        with self.assertRaisesRegex(StoreError, "revision_conflict"):
            self.store.save(changed, expected_revision=1)

    def test_import_preserves_raw_recording_and_assigns_new_workflow_identity(self):
        original = {"workflow": workflow(), "events": [{"action": "user_click", "timestamp": 123}]}
        path = self.folder / "recording.json"
        path.write_text(json.dumps(original))
        imported = self.store.import_recording(path)
        self.assertNotEqual(imported["id"], original["workflow"]["id"])
        receipt = self.store.root / "recordings" / (imported["recording_id"] + ".json")
        self.assertEqual(json.loads(receipt.read_text()), original)
        imported["steps"][0]["value"] = "edited"
        self.store.save(imported)
        self.assertEqual(json.loads(receipt.read_text()), original)

    def test_unknown_action_and_script_fields_rejected(self):
        for edit in ({"action": "shell"}, {"command": "arbitrary.exe"}, {"action": "mouse-click"}):
            value = workflow()
            value["steps"][0].update(edit)
            with self.subTest(edit=edit), self.assertRaises(StoreError):
                self.store.save(value)

    def test_duplicate_steps_unknown_parameters_and_bad_checked_rejected(self):
        one = step()
        candidates = [workflow([one, copy.deepcopy(one)]), workflow([step(parameter="missing")]),
                      workflow([step(action="check", expect={"kind": "checked", "value": "false"})])]
        for value in candidates:
            with self.assertRaises(StoreError):
                validate_workflow(value)

    def test_region_requires_observed_locator_point_and_bounds(self):
        target = {"kind": "region", "locator": {"automation_id": "Grid"},
                  "point": {"x_ratio": 0.4, "y_ratio": 0.6},
                  "recorded_bounds": {"x": 10, "y": 30, "width": 500, "height": 200}}
        value = workflow([step("right-click", "none", target=target, headed=True)])
        self.store.save(value)
        for mutate in (lambda t: t["point"].update(x_ratio=1.1), lambda t: t.update(locator={"role": "group"}),
                       lambda t: t.pop("recorded_bounds")):
            changed = copy.deepcopy(value)
            mutate(changed["steps"][0]["target"])
            with self.assertRaises(StoreError):
                validate_workflow(changed)

    def test_region_requires_reviewed_revision_before_dispatch(self):
        target = {"kind": "region", "locator": {"automation_id": "Grid"},
                  "point": {"x_ratio": 0.4, "y_ratio": 0.6},
                  "recorded_bounds": {"x": 10, "y": 30, "width": 500, "height": 200}}
        saved = self.save(workflow([step("right-click", "none", target=target, headed=True)]))
        adapter = Adapter([result("executed_unverified", True, "unavailable")])
        runner = self.runner(adapter)
        original = runner.start(saved["id"], BINDING)
        self.assertEqual(original["steps"][0]["reason"], "region_requires_review")
        self.assertEqual(adapter.executions, [])
        self.assertIs(original["steps"][0]["input_dispatched"], False)
        saved["steps"][0]["target"]["reviewed"] = True
        self.store.save(saved)
        repaired = runner.fork_repaired(original["id"], BINDING)
        self.assertEqual(repaired["status"], "completed_unverified")
        self.assertEqual(len(adapter.executions), 1)

    def test_distinct_expected_value_or_checked_state_needs_own_readback(self):
        for instruction in (step(expect={"kind": "value", "value": "different"}),
                            step(expect={"kind": "value", "value": "N", "target": {"kind": "element", "locator": {"name": "Other field"}}}),
                            step("check", {"kind": "checked", "value": False})):
            with self.subTest(action=instruction["action"]):
                saved = self.save(workflow([instruction]))
                adapter = Adapter(checks=[{"matched": False}])
                run = self.runner(adapter).start(saved["id"], BINDING)
                self.assertEqual(run["status"], "needs_review")
                self.assertEqual(len(adapter.reads), 1)

    def test_network_relative_storage_and_path_traversal_rejected(self):
        for path in ("relative", r"\\server\share\store", "//server/share/store"):
            with self.assertRaises(StoreError):
                Store(path)
        for identifier in ("../private", "a" * 31, "Z" * 32):
            with self.assertRaises(StoreError):
                self.store.load(identifier)

    def test_duplicate_json_keys_and_nan_rejected(self):
        path = self.folder / "bad.json"
        for text in ('{"a":1,"a":2}', '{"a":NaN}'):
            path.write_text(text)
            with self.assertRaises(StoreError):
                storage.read_json(path)

    def test_parameters_are_resolved_inside_runner_without_model_ref_copying(self):
        value = workflow([step(parameter="status", expect={"kind": "value", "value": "N"})])
        value["parameters"] = {"status": "N"}
        saved = self.save(value)
        adapter = Adapter()
        run = self.runner(adapter).start(saved["id"], BINDING, {"status": "Y"})
        self.assertEqual(run["status"], "completed")
        self.assertEqual(adapter.executions[0][1], "Y")
        self.assertEqual(adapter.executions[0][0]["expect"]["value"], "Y")

    def test_parameter_does_not_overwrite_other_control_completion_value(self):
        value = workflow([step(parameter="status", expect={"kind": "value", "value": "Complete",
            "target": {"kind": "element", "locator": {"name": "Result status"}}})])
        value["parameters"] = {"status": "N"}
        saved = self.save(value)
        adapter = Adapter()
        run = self.runner(adapter).start(saved["id"], BINDING, {"status": "Y"})
        self.assertEqual(run["status"], "completed")
        self.assertEqual(adapter.reads[0][0]["expect"]["value"], "Complete")
        self.assertEqual(adapter.executions[0][1], "Y")

    def test_identity_is_durable_before_dispatch_and_changed_process_blocks_resume(self):
        saved = self.save(workflow([step(), step("click", "none")]))
        adapter = IdentityAdapter()
        stopped = []
        def progress(event):
            if event["completed_steps"] == 1 and not stopped:
                stopped.append(True)
                runner.cancel()
        def before_execute(_, value):
            run_id = self.store.list_runs()[0]["id"]
            self.assertEqual(self.store.load_run(run_id)["target_identity"], adapter.identity())
        adapter.before_execute = before_execute
        runner = self.runner(adapter, progress)
        original = runner.start(saved["id"], BINDING)
        self.assertEqual(original["status"], "cancelled")
        adapter.lifetime = "replacement-process"
        resumed = runner.resume(original["id"], BINDING)
        self.assertEqual(resumed["status"], "needs_review")
        self.assertEqual(resumed["diagnostic"], "target_identity_changed")
        self.assertEqual(len(adapter.executions), 1)
        adapter.lifetime = "original-process"
        continued = runner.resume(original["id"], BINDING)
        self.assertEqual(continued["status"], "completed_unverified")
        self.assertEqual(len(adapter.executions), 2)

    def test_attempted_legacy_run_without_identity_cannot_rebaseline(self):
        saved = self.save()
        original = self.runner(Adapter([result("uncertain", None, "unavailable")])).start(saved["id"], BINDING)
        adapter = IdentityAdapter()
        resumed = self.runner(adapter).resume(original["id"], BINDING)
        self.assertEqual(resumed["diagnostic"], "prior_target_identity_missing")
        self.assertEqual(adapter.executions, [])
        self.assertNotIn("target_identity", resumed)

    def test_repair_rejects_changed_process_before_creating_continuation(self):
        saved = self.save()
        adapter = IdentityAdapter([result("blocked", False, "unavailable")])
        original = self.runner(adapter).start(saved["id"], BINDING)
        saved["steps"][0]["label"] = "fixed"
        self.store.save(saved)
        adapter.lifetime = "replacement-process"
        with self.assertRaisesRegex(StoreError, "target_identity_changed"):
            self.runner(adapter).fork_repaired(original["id"], BINDING)
        self.assertNotIn("repair_continuation", self.store.load_run(original["id"]))
        self.assertEqual(adapter.executions[0][0]["target"]["window"]["title"], "Private title")

    def test_unknown_parameter_and_wrong_executable_stop_before_adapter(self):
        saved = self.save()
        adapter = Adapter()
        with self.assertRaises(StoreError):
            self.runner(adapter).start(saved["id"], BINDING, {"unrecognized": "N"})
        with self.assertRaises(StoreError):
            self.runner(adapter).start(saved["id"], dict(BINDING, exe_name="Other.exe"))
        self.assertEqual(adapter.executions, [])

    def test_journal_is_started_before_dispatch_and_contains_baseline(self):
        saved = self.save()
        adapter = Adapter()
        def verify(target, value):
            runs = self.store.list_runs()
            live = self.store.load_run(runs[0]["id"])
            self.assertEqual(live["steps"][0]["status"], "started")
            self.assertIsNone(live["steps"][0]["input_dispatched"])
            self.assertEqual(target["_prepared"]["baseline"], "pre-action")
        adapter.before_execute = verify
        run = self.runner(adapter).start(saved["id"], BINDING)
        self.assertTrue(run["task_verified"])

    def test_click_delivery_only_continues_without_business_success(self):
        saved = self.save(workflow([step("click", "none"), step("click", "none")]))
        adapter = Adapter([result("executed_unverified", True, "unavailable")])
        run = self.runner(adapter).start(saved["id"], BINDING)
        self.assertEqual(len(adapter.executions), 2)
        self.assertEqual(run["status"], "completed_unverified")
        self.assertFalse(run["task_verified"])
        self.assertTrue(all(entry["status"] == "executed" for entry in run["steps"]))

    def test_manual_result_pauses_and_confirm_resumes_without_clicking_again(self):
        saved = self.save(workflow([step("click", "manual"), step()]))
        adapter = Adapter([result("executed_unverified", True, "unavailable"), result()])
        runner = self.runner(adapter)
        first = runner.start(saved["id"], BINDING)
        self.assertEqual(first["status"], "needs_review")
        again = runner.resume(first["id"], BINDING)
        self.assertEqual(again["status"], "needs_review")
        self.assertEqual(len(adapter.executions), 1)
        final = runner.resume(first["id"], BINDING, confirm_step_id=saved["steps"][0]["id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(len(adapter.executions), 2)
        self.assertEqual(final["steps"][0]["confirmation"], "user_confirmed_current_result")

    def test_manual_step_never_calls_mutating_adapter(self):
        saved = self.save(workflow([step("manual", "manual", target={"kind": "manual"})]))
        adapter = Adapter()
        first = self.runner(adapter).start(saved["id"], BINDING)
        self.assertEqual(adapter.executions, [])
        final = self.runner(adapter).resume(first["id"], BINDING, saved["steps"][0]["id"])
        self.assertEqual(final["status"], "completed")
        self.assertFalse(final["steps"][0]["input_dispatched"])

    def test_unknown_delivery_stops_and_resume_checks_without_resending(self):
        saved = self.save()
        adapter = Adapter([result("uncertain", None, "unavailable")], [{"matched": False}, {"matched": True}])
        runner = self.runner(adapter)
        first = runner.start(saved["id"], BINDING)
        self.assertEqual(first["status"], "needs_review")
        runner.resume(first["id"], BINDING)
        final = runner.resume(first["id"], BINDING)
        self.assertEqual(len(adapter.executions), 1)
        self.assertEqual(final["status"], "completed")
        self.assertIsNone(final["steps"][0]["input_dispatched"])

    def test_process_crash_leaves_started_record_and_resume_does_not_dispatch(self):
        class Crash(BaseException):
            pass
        saved = self.save()
        adapter = Adapter([Crash()], [{"matched": None}])
        with self.assertRaises(Crash):
            self.runner(adapter).start(saved["id"], BINDING)
        run_id = self.store.list_runs()[0]["id"]
        self.assertEqual(self.store.load_run(run_id)["steps"][0]["status"], "started")
        resumed = self.runner(adapter).resume(run_id, BINDING)
        self.assertEqual(resumed["status"], "needs_review")
        self.assertEqual(len(adapter.executions), 1)

    def test_keyboard_interrupt_after_start_is_uncertain_and_journaled(self):
        saved = self.save()
        adapter = Adapter([KeyboardInterrupt()])
        run = self.runner(adapter).start(saved["id"], BINDING)
        self.assertEqual(run["status"], "needs_review")
        self.assertIsNone(self.store.load_run(run["id"])["steps"][0]["input_dispatched"])

    def test_bad_success_response_cannot_claim_completion(self):
        saved = self.save()
        for response in ({"status": "completed"}, {}, {"status": "completed", "input_dispatched": False}):
            with self.subTest(response=response):
                run = self.runner(Adapter([response])).start(saved["id"], BINDING)
                self.assertEqual(run["status"], "needs_review")
                self.assertFalse(run["task_verified"])

    def test_verification_failure_never_retries_input(self):
        saved = self.save()
        adapter = Adapter([result("verification_failed", True, "mismatch")], [{"matched": False}])
        run = self.runner(adapter).start(saved["id"], BINDING)
        self.runner(adapter).resume(run["id"], BINDING)
        self.assertEqual(len(adapter.executions), 1)

    def test_postcondition_exception_preserves_dispatch_before_pause(self):
        saved = self.save(workflow([step("click", "exists")]))
        adapter = Adapter([result("executed_unverified", True, "unavailable")], [RuntimeError("private")])
        run = self.runner(adapter).start(saved["id"], BINDING)
        stored = self.store.load_run(run["id"])
        self.assertTrue(stored["steps"][0]["input_dispatched"])
        self.assertEqual(stored["steps"][0]["result"]["status"], "executed_unverified")
        self.assertNotIn("private", json.dumps(stored))

    def test_prepared_file_baseline_survives_new_adapter_on_resume(self):
        saved = self.save(workflow([step("click", expect={"kind": "file", "path": str(self.folder / "out.xlsx")})]))
        first_adapter = Adapter([result("executed_unverified", True, "unavailable")], [{"matched": None}])
        run = self.runner(first_adapter).start(saved["id"], BINDING)
        later = Adapter(checks=[{"matched": True}])
        done = self.runner(later).resume(run["id"], BINDING)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(later.reads[0][0]["_prepared"]["baseline"], "pre-action")
        self.assertEqual(later.executions, [])

    def test_cancel_between_actions_and_resume_does_not_repeat_prefix(self):
        saved = self.save(workflow([step(), step()]))
        adapter = Adapter()
        runner = self.runner(adapter)
        adapter.before_execute = lambda *_: runner.cancel()
        run = runner.start(saved["id"], BINDING)
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(len(adapter.executions), 1)
        adapter.before_execute = None
        done = runner.resume(run["id"], BINDING)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(len(adapter.executions), 2)

    def test_new_revision_does_not_change_existing_run_snapshot(self):
        saved = self.save(workflow([step("manual", "manual", target={"kind": "manual"})]))
        run = self.runner(Adapter()).start(saved["id"], BINDING)
        saved["steps"][0]["label"] = "new label"
        self.store.save(saved)
        original = self.store.load_run(run["id"])
        self.assertEqual(original["workflow_revision"], 1)
        self.assertEqual(original["workflow"]["steps"][0]["label"], "Private control")

    def test_confirmation_must_identify_current_pending_step(self):
        saved = self.save(workflow([step("manual", "manual", target={"kind": "manual"}), step()]))
        runner = self.runner(Adapter())
        run = runner.start(saved["id"], BINDING)
        with self.assertRaisesRegex(StoreError, "confirmation_step_mismatch"):
            runner.resume(run["id"], BINDING, saved["steps"][1]["id"])

    def test_resume_cannot_switch_to_another_same_executable_window(self):
        saved = self.save()
        runner = self.runner(Adapter([result("uncertain", None, "unavailable")]))
        run = runner.start(saved["id"], BINDING)
        with self.assertRaisesRegex(StoreError, "resume_binding_mismatch"):
            runner.resume(run["id"], dict(BINDING, hwnd=201))

    def test_target_lock_blocks_concurrent_run_and_releases_afterward(self):
        saved = self.save()
        with self.store.lock("target-100-200"):
            with self.assertRaisesRegex(StoreError, "resource_busy"):
                self.runner(Adapter()).start(saved["id"], BINDING)
        self.assertEqual(self.runner(Adapter()).start(saved["id"], BINDING)["status"], "completed")

    def test_process_death_releases_os_lock_without_live_lock_takeover(self):
        ready = self.folder / "child-ready"
        script = """
import pathlib, sys
sys.path.insert(0, sys.argv[1])
import storage
# Only the OS lock is under test; directory policy has separate tests.
storage._validate_existing_directory = lambda path: pathlib.Path(path)
store = storage.Store(pathlib.Path(sys.argv[2]))
with store.lock('target-100-200'):
    pathlib.Path(sys.argv[3]).write_text('ready')
    sys.stdin.read()
"""
        process = subprocess.Popen([sys.executable, "-B", "-c", script,
            str(Path(storage.__file__).parent), str(self.store.root), str(ready)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "lock child failed to become ready")
            with self.assertRaisesRegex(StoreError, "resource_busy"):
                with self.store.lock("target-100-200"):
                    self.fail("live child lock was taken over")
            # kill does not run Python finally blocks: the operating system must release it.
            process.kill()
            process.wait(timeout=10)
            with self.store.lock("target-100-200"):
                self.assertTrue((self.store.root / "locks" / "target-100-200.lock").exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_progress_is_bounded_summary_without_business_text(self):
        saved = self.save()
        events = []
        self.runner(Adapter(), events.append).start(saved["id"], BINDING)
        self.assertTrue(events)
        self.assertNotIn("Private", json.dumps(events))
        self.assertNotIn("parameters", json.dumps(events))

    def test_display_exception_does_not_repeat_or_interrupt_execution(self):
        saved = self.save()
        adapter = Adapter()
        def bad_display(_):
            raise RuntimeError("broken display")
        run = self.runner(adapter, bad_display).start(saved["id"], BINDING)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(adapter.executions), 1)

    def test_wait_uses_reads_only(self):
        saved = self.save(workflow([step("wait", "exists", timeout_seconds=1)]))
        adapter = Adapter(checks=[{"matched": False}, {"matched": True}])
        run = self.runner(adapter).start(saved["id"], BINDING)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(adapter.executions, [])
        self.assertEqual(len(adapter.reads), 2)

    def test_file_wait_captures_baseline_once_and_keeps_it_after_cancel(self):
        saved = self.save(workflow([step("wait", {"kind": "file", "path": str(self.folder / "export.xlsx")}, timeout_seconds=1)]))
        adapter = Adapter(checks=[{"matched": False}])
        runner = self.runner(adapter)
        original_check = adapter.check
        def cancel_after_check(target, value):
            checked = original_check(target, value)
            runner.cancel()
            return checked
        adapter.check = cancel_after_check
        run = runner.start(saved["id"], BINDING)
        self.assertEqual(run["status"], "cancelled")
        baseline = run["steps"][0]["prepared"]
        self.assertEqual(adapter.reads[0][0]["_prepared"], baseline)
        self.assertEqual(run["steps"][0]["attempts"], 0)
        self.assertIs(run["steps"][0]["input_dispatched"], False)
        resumed_adapter = Adapter(checks=[{"matched": True}])
        resumed = self.runner(resumed_adapter).resume(run["id"], BINDING)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed_adapter.reads[0][0]["_prepared"], baseline)
        self.assertEqual(resumed_adapter.preparations, [])
        self.assertEqual(adapter.executions + resumed_adapter.executions, [])

    def test_completed_run_resume_is_read_only(self):
        saved = self.save()
        adapter = Adapter()
        runner = self.runner(adapter)
        run = runner.start(saved["id"], BINDING)
        runner.resume(run["id"], BINDING)
        self.assertEqual(len(adapter.executions), 1)

    def test_repair_fork_runs_only_known_unsent_pending_step(self):
        saved = self.save(workflow([step(), step("click", "none")]))
        adapter = Adapter([result(), result("blocked", False, "unavailable")])
        original = self.runner(adapter).start(saved["id"], BINDING)
        saved["steps"][1]["target"]["locator"]["automation_id"] = "CorrectButton"
        latest = self.store.save(saved)
        repaired_adapter = Adapter([result("executed_unverified", True, "unavailable")], [{"matched": True}])
        repaired = self.runner(repaired_adapter).fork_repaired(original["id"], BINDING)
        self.assertNotEqual(repaired["id"], original["id"])
        self.assertEqual(repaired["repair_fork_of"], original["id"])
        self.assertEqual(repaired["workflow_revision"], latest["revision"])
        self.assertEqual(len(repaired_adapter.executions), 1)
        self.assertEqual(repaired_adapter.executions[0][0]["target"]["locator"]["automation_id"], "CorrectButton")
        self.assertEqual(self.store.load_run(original["id"])["status"], "needs_review")

    def test_repair_request_retry_reuses_child_without_second_dispatch(self):
        saved = self.save()
        original = self.runner(Adapter([result("blocked", False, "unavailable")])).start(saved["id"], BINDING)
        saved["steps"][0]["target"]["locator"]["automation_id"] = "Fixed"
        self.store.save(saved)
        adapter = Adapter()
        runner = self.runner(adapter)
        first = runner.fork_repaired(original["id"], BINDING)
        again = runner.fork_repaired(original["id"], BINDING)
        self.assertEqual(again["id"], first["id"])
        resumed_parent = runner.resume(original["id"], BINDING)
        self.assertEqual(resumed_parent["id"], first["id"])
        self.assertEqual(len(adapter.executions), 1)

    def test_repair_reservation_survives_crash_before_child_journal(self):
        class Crash(BaseException):
            pass
        saved = self.save()
        original = self.runner(Adapter([result("blocked", False, "unavailable")])).start(saved["id"], BINDING)
        saved["steps"][0]["label"] = "fixed"
        self.store.save(saved)
        adapter = Adapter()
        runner = self.runner(adapter)
        with patch.object(self.store, "_activate_reserved_run", side_effect=Crash), self.assertRaises(Crash):
            runner.fork_repaired(original["id"], BINDING)
        reserved_id = self.store.load_run(original["id"])["repair_continuation"]
        self.assertFalse((self.store.run_directory(reserved_id) / "run.json").exists())
        self.assertEqual([r["id"] for r in self.store.list_runs()], [original["id"]])
        self.assertEqual(adapter.executions, [])
        recovered = runner.fork_repaired(original["id"], BINDING)
        self.assertEqual(recovered["id"], reserved_id)
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(len(adapter.executions), 1)

    def test_uncertain_repair_retry_only_reads_and_missing_used_journal_does_not_reset(self):
        saved = self.save()
        original = self.runner(Adapter([result("blocked", False, "unavailable")])).start(saved["id"], BINDING)
        saved["steps"][0]["label"] = "fixed"
        self.store.save(saved)
        adapter = Adapter([result("uncertain", None, "unavailable")], [{"matched": False}])
        runner = self.runner(adapter)
        first = runner.fork_repaired(original["id"], BINDING)
        again = runner.fork_repaired(original["id"], BINDING)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(adapter.executions), 1)
        self.assertEqual(again["status"], "needs_review")
        (self.store.run_directory(first["id"]) / "run.json").unlink()
        with self.assertRaisesRegex(StoreError, "reserved_run_journal_missing"):
            runner.fork_repaired(original["id"], BINDING)
        self.assertEqual(len(adapter.executions), 1)

    def test_repair_fork_rejects_uncertain_delivery(self):
        saved = self.save()
        original = self.runner(Adapter([result("uncertain", None, "unavailable")])).start(saved["id"], BINDING)
        saved["steps"][0]["label"] = "repaired"
        self.store.save(saved)
        adapter = Adapter()
        with self.assertRaisesRegex(StoreError, "uncertain_step_cannot_be_repaired"):
            self.runner(adapter).fork_repaired(original["id"], BINDING)
        self.assertEqual(adapter.executions, [])

    def test_repair_fork_rejects_completed_prefix_changes(self):
        saved = self.save(workflow([step(), step("manual", "manual", target={"kind": "manual"})]))
        original = self.runner(Adapter()).start(saved["id"], BINDING)
        saved["steps"][0]["value"] = "different"
        self.store.save(saved)
        with self.assertRaisesRegex(StoreError, "completed_prefix_changed"):
            self.runner(Adapter()).fork_repaired(original["id"], BINDING)

    def test_repair_fork_rechecks_prior_conditions_without_resetting_them(self):
        saved = self.save(workflow([step(), step("manual", "manual", target={"kind": "manual"})]))
        original = self.runner(Adapter()).start(saved["id"], BINDING)
        saved["steps"][1]["label"] = "reviewed pending step"
        self.store.save(saved)
        adapter = Adapter(checks=[{"matched": False}])
        repaired = self.runner(adapter).fork_repaired(original["id"], BINDING)
        self.assertEqual(repaired["status"], "needs_review")
        self.assertEqual(repaired["steps"][0]["reason"], "completed_prefix_condition_changed")
        self.assertEqual(adapter.executions, [])

    def test_inconsistent_completed_journal_rejected(self):
        saved = self.save(workflow([step("manual", "manual", target={"kind": "manual"})]))
        run = self.runner(Adapter()).start(saved["id"], BINDING)
        path = self.store.run_directory(run["id"]) / "run.json"
        altered = json.loads(path.read_text())
        altered["status"] = "completed"
        path.write_text(json.dumps(altered))
        with self.assertRaisesRegex(StoreError, "inconsistent_completed_run"):
            self.store.load_run(run["id"])

    def test_malformed_and_started_reset_journal_rejected(self):
        saved = self.save()
        run = self.runner(Adapter([result("uncertain", None, "unavailable")])).start(saved["id"], BINDING)
        path = self.store.run_directory(run["id"]) / "run.json"
        original = json.loads(path.read_text())
        for entry in (None, dict(original["steps"][0], status="pending")):
            altered = copy.deepcopy(original)
            altered["steps"][0] = entry
            path.write_text(json.dumps(altered))
            with self.assertRaises(StoreError):
                self.store.load_run(run["id"])


if __name__ == "__main__":
    unittest.main()
