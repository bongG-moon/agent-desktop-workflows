"""Local Tk workspace for recording, reviewing and replaying desktop workflows."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import uuid

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "Agent Desktop Workflows"
VERSION = "0.1.4"
ACTION_LABELS = {"click": "클릭", "double-click": "두 번 클릭", "right-click": "우클릭",
    "set-value": "값 설정", "type": "입력", "select": "항목 선택", "expand": "펼치기",
    "collapse": "접기", "check": "체크", "uncheck": "체크 해제", "focus": "포커스",
    "clear": "비우기", "scroll": "스크롤", "wait": "기다리기", "manual": "직접 수행"}
EXPECT_LABELS = {"value": "현재 값 일치", "checked": "체크 상태", "exists": "대상 표시",
    "file": "새 출력 파일", "manual": "사람이 결과 확인", "none": "전달 확인 후 계속"}
STATE_LABELS = {"running": "실행 중", "completed": "완료 조건 확인", "completed_unverified": "실행 종료 / 일부 결과 미확인",
    "needs_review": "확인 필요", "cancelled": "일시정지 / 중단", "ready": "녹화 준비",
    "recording": "녹화 중", "paused": "녹화 일시정지", "outside_target": "대상 창 밖 / 기록 제외",
    "review": "녹화 검토", "failed": "중단", "verified": "조건 확인", "executed": "전달 확인",
    "prepared": "준비", "started": "실행 시작", "pending": "대기", "not_delivered": "전달 안 됨",
    "skipped_confirmed": "직접 확인", "uncertain": "전달 여부 미확인"}


def target_text(target):
    locator = target.get("locator", {})
    parts = [locator.get("name"), locator.get("automation_id"), locator.get("role")]
    identity = " / ".join(str(v) for v in parts if v)
    return identity or ("직접 처리" if target.get("kind") == "manual" else "대상 지정 필요")


def recognition_text(step):
    rec = step.get("recognition", {})
    kind = step.get("target", {}).get("kind")
    if kind == "element" and rec.get("reason") == "combo_value_unobserved":
        return "요소 확인 / 선택값 보완 필요"
    if kind == "manual" or step.get("action") == "manual":
        return "보완 필요"
    if kind == "region":
        return "영역 확인됨 / 재생 시 재확인" if step.get("target", {}).get("reviewed") else "영역 위치 검토 필요"
    observed = str(rec.get("target", "")).casefold()
    return "미확인" if observed in {"", "unavailable", "unknown", "missing", "unrecognized", "not_found", "manual"} else "요소 후보"


def _parameter_value(step, workflow):
    name = step.get("parameter")
    return workflow.get("parameters", {}).get(name, "") if name else step.get("value", "")


def _safe_reason(error):
    # UI is local, but do not dump raw provider traces into a message box.
    name = str(error)
    descriptions = {
        "uncertain_step_cannot_be_repaired": "이미 전달됐을 수 있는 단계는 수정본으로 반복할 수 없습니다. 실제 결과를 확인한 뒤 현재 단계 확인으로 이어가세요.",
        "new_revision_required": "먼저 선택한 단계를 보완해 새 수정본으로 저장하세요.",
        "binding_application_mismatch": "녹화한 앱과 현재 선택한 앱이 다릅니다. 대상 업무 창을 다시 선택하세요.",
        "resume_binding_mismatch": "이 실행이 시작된 창과 현재 창이 다릅니다. 원래 업무 창을 선택하세요.",
        "revision_conflict": "다른 수정본이 먼저 저장됐습니다. 작업을 다시 선택해 최신 수정본을 불러오세요.",
        "workflow_revision_changed": "화면을 연 뒤 작업 수정본이 바뀌었습니다. 최신 수정본을 불러와 내용을 확인한 뒤 실행하세요.",
        "recording_cancelled": "녹화를 취소했습니다. 기존 작업은 유지됩니다.",
        "region_requires_review": "영역 대상의 검토가 필요합니다. 단계 수정에서 기록한 대상과 위치를 확인해 저장한 뒤 수정본으로 이어가세요.",
        "manual_step": "직접 수행하도록 기록한 단계입니다. 실제 작업을 수행한 뒤 현재 단계 확인으로 이어가세요.",
        "manual_result_confirmation": "입력 후 실제 결과를 직접 확인하도록 설정한 단계입니다.",
        "delivery_uncertain": "입력이 전달됐는지 확인되지 않습니다. 자동 반복하지 않고 실제 화면 확인을 기다립니다.",
        "postcondition_unverified": "설정한 완료 조건이 아직 확인되지 않습니다. 실제 결과를 확인하세요.",
        "wait_condition_unverified": "기다리던 조건이 확인되지 않습니다. 현재 화면과 완료 조건을 확인하세요.",
        "prior_attempt_requires_review": "이전 실행 시도가 있어 자동 반복할 수 없습니다. 저장된 기록과 현재 상태를 확인하세요.",
        "completed_prefix_changed": "이미 마친 앞부분의 정의가 바뀌어 수정본으로 이어갈 수 없습니다.",
        "completed_prefix_condition_changed": "이어하기에 필요한 현재 조건이 달라졌습니다. 해당 조건을 확인하고 원래 상태로 복원한 뒤 이어하기를 다시 선택하세요. 완료 확인으로 건너뛸 수 없습니다.",
        "repair_scope_changed": "입력 변수나 작업 범위가 달라져 이전 실행을 이어갈 수 없습니다.",
        "repair_step_identity_changed": "중단된 단계의 ID가 달라져 이전 실행을 이어갈 수 없습니다.",
        "target_identity_changed": "대상 프로그램이나 창의 식별 정보가 달라졌습니다. 이전 실행의 입력은 이어 보내지 않습니다.",
        "target_identity_missing": "이전 실행에 대상 식별 근거가 없어 자동으로 이어갈 수 없습니다.",
        "selection_action_not_supported": "현재 콤보는 항목 선택 동작을 지원하지 않습니다. 실제 지원 동작을 확인해 이 단계를 보완하세요.",
        "combo_value_unobserved": "콤보 대상은 찾았지만 실제 선택값을 읽지 못했습니다. 단계 수정에서 실제 값이나 입력 변수와 기본값을 지정하세요. 같은 클릭을 다시 녹화하는 것만으로 해결되지 않을 수 있습니다.",
        "empty_combo_value_requires_review": "콤보 설정값이 비어 있어 입력을 보내지 않았습니다. 단계 수정에서 실제 값 또는 입력 변수와 기본값을 지정하고 새 수정본으로 저장하세요.",
        "combo_value_unavailable": "콤보의 실제 선택값을 확인할 근거가 부족합니다. 빈 값 비교로 성공 처리하지 않습니다. 현재 화면을 확인하세요.",
        "combo_value_conflict": "콤보에서 읽은 선택값 근거가 서로 다릅니다. 현재 화면과 선택 항목을 확인하세요.",
        "combo_value_mismatch": "입력은 전달됐지만 실제 콤보 선택값이 요청한 값과 다릅니다. 자동 반복하지 않고 현재 화면 확인을 기다립니다.",
        "combo_value_observed": "콤보의 실제 선택값 근거가 요청한 값과 일치합니다.",
        "combo_readback_interrupted": "입력 전달 후 선택값 확인이 중단됐습니다. 현재 화면을 확인하고 이어갈지 결정하세요. 입력을 자동 반복하지 않습니다.",
        "combo_parent_value_unavailable": "값 쓰기는 전달됐지만 부모 콤보의 값만으로는 결과를 확인할 수 없습니다.",
    }
    if name in descriptions:
        return descriptions[name]
    return name if name and len(name) < 180 and "\n" not in name else type(error).__name__


class DemoStore:
    """In-memory preview only; no desktop commands or persistent writes."""
    def __init__(self):
        self.root = ROOT / ".build" / "ui-preview"
        self.workflow = {"schema_version": 1, "id": "d" * 32, "name": "Device 조회 후 Excel 내보내기 / 화면 예시",
            "revision": 1, "target": {"exe_name": "DemoApp.exe", "title": "Device Setup — 합성 화면", "class_name": "Demo"},
            "parameters": {"Family": "Hybrid", "Status": "N"}, "steps": []}
        definitions = [("Family 조건 설정", "set-value", "Family", "element", "value", "Family"),
            ("Status 조건 설정", "set-value", "Status", "element", "value", "Status"),
            ("조회 요청", "click", "조회", "element", "none", None),
            ("조회 완료 확인", "manual", "결과 표", "manual", "manual", None),
            ("결과 표 우클릭", "right-click", "결과 표", "region", "none", None),
            ("Export to Excel 선택", "click", "Export to Excel", "element", "manual", None)]
        for i, (label, action, name, kind, expect, parameter) in enumerate(definitions):
            step = {"id": f"{i + 1:032x}", "label": label, "action": action,
                "target": {"kind": kind, "locator": {"name": name, "role": "combobox" if parameter else "button"}},
                "expect": {"kind": expect}, "recognition": {"target": "observed" if kind != "manual" else "unavailable",
                    "action": "candidate", "result": "pending", "reason": "완료 표식이 없어 직접 확인이 필요합니다." if kind == "manual" else ""}}
            if parameter:
                step["parameter"] = parameter
            if kind == "region":
                step["target"]["point"] = {"x_ratio": 0.4, "y_ratio": 0.4}
            self.workflow["steps"].append(step)

    def list_workflows(self):
        return [copy.deepcopy(self.workflow)]

    def load(self, _id):
        return copy.deepcopy(self.workflow)

    def list_runs(self, workflow_id=None):
        return []


class WorkflowApp(tk.Tk):
    def __init__(self, store_path=None, *, demo=False):
        super().__init__()
        self.title(f"{APP_NAME} {VERSION}")
        self.geometry("1190x790")
        self.minsize(1000, 680)
        self.configure(bg="#edf3f7")
        self.demo = demo
        if demo:
            self.store = DemoStore()
        else:
            from storage import Store
            self.store = Store(Path(store_path) if store_path else None)
        self.events = queue.Queue()
        self.workflow = None
        self.workflows = []
        self.windows = []
        self.busy = False
        self.runner = None
        self.run = None
        self.history_view = False
        self.stop_event = threading.Event()
        self.param_vars = {}
        self.status = tk.StringVar(value="작업을 한 번 보여주고, 다음부터 이름과 조건으로 실행하세요.")
        self.target_var = tk.StringVar()
        self.workflow_title = tk.StringVar(value="저장한 작업을 선택하세요")
        self.workflow_meta = tk.StringVar(value="녹화 원본과 수정 이력은 별도로 보관합니다.")
        self._build_style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(60, self._drain)
        self.refresh_workflows()
        if demo:
            self.windows = [{"pid": 1000, "hwnd": 2000, "exe_name": "DemoApp.exe", "title": "Device Setup — 합성 화면", "class_name": "Demo"}]
            self._show_windows(self.windows)
            self.status.set("화면 예시입니다. 실제 앱을 조작하거나 작업을 저장하지 않습니다.")
        else:
            self.after(150, self.refresh_windows)

    def _build_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#edf3f7")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("TLabel", background="#edf3f7", foreground="#20394d", font=("맑은 고딕", 10))
        style.configure("Card.TLabel", background="#ffffff")
        style.configure("Title.TLabel", font=("맑은 고딕", 18, "bold"), background="#113a4e", foreground="#ffffff")
        style.configure("Sub.TLabel", font=("맑은 고딕", 10), background="#113a4e", foreground="#cde1ec")
        style.configure("Section.TLabel", font=("맑은 고딕", 12, "bold"), background="#ffffff")
        style.configure("TButton", font=("맑은 고딕", 10), padding=(12, 8), borderwidth=0)
        style.configure("Accent.TButton", background="#087d78", foreground="white", font=("맑은 고딕", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#086b67"), ("disabled", "#b7caca")])
        style.configure("Treeview", font=("맑은 고딕", 10), rowheight=35, fieldbackground="#ffffff", borderwidth=0)
        style.configure("Treeview.Heading", font=("맑은 고딕", 9, "bold"), background="#e5edf2", padding=(5, 9))
        style.map("Treeview", background=[("selected", "#d8efe9")], foreground=[("selected", "#153d43")])
        style.configure("TCombobox", padding=6)

    def _build(self):
        header = tk.Frame(self, bg="#113a4e", padx=25, pady=17)
        header.pack(fill="x")
        ttk.Label(header, text="Agent Desktop Workflows", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="녹화 → 인식 상태 확인 → 필요한 단계만 보완 → 작업 이름으로 실행", style="Sub.TLabel").pack(anchor="w", pady=(5, 0))
        top = ttk.Frame(self, padding=(20, 14, 20, 10))
        top.pack(fill="x")
        ttk.Label(top, text="대상 업무 창", font=("맑은 고딕", 10, "bold")).pack(side="left", padx=(0, 12))
        self.window_combo = ttk.Combobox(top, textvariable=self.target_var, state="readonly")
        self.window_combo.pack(side="left", fill="x", expand=True)
        self.refresh_button = ttk.Button(top, text="창 새로고침", command=self.refresh_windows)
        self.refresh_button.pack(side="left", padx=(8, 0))
        body = ttk.Frame(self, padding=(20, 0, 20, 12))
        body.pack(fill="both", expand=True)
        sidebar = ttk.Frame(body, style="Card.TFrame", padding=14, width=245)
        sidebar.pack(side="left", fill="y", padx=(0, 14))
        sidebar.pack_propagate(False)
        ttk.Label(sidebar, text="내 작업", style="Section.TLabel").pack(anchor="w", pady=(0, 12))
        self.workflow_list = ttk.Treeview(sidebar, show="tree", selectmode="browse", height=3)
        self.workflow_list.pack(fill="both", expand=True)
        self.workflow_list.bind("<<TreeviewSelect>>", lambda _e: self._select_workflow())
        self.workflow_list.bind("<Button-1>", lambda _e: "break" if self.busy else None)
        self.workflow_list.bind("<Key>", lambda _e: "break" if self.busy else None)
        self.record_button = ttk.Button(sidebar, text="＋ 새 작업 녹화", style="Accent.TButton", command=self.record)
        self.record_button.pack(fill="x", pady=(13, 6))
        self.import_button = ttk.Button(sidebar, text="녹화 파일 가져오기", command=self.import_file)
        self.import_button.pack(fill="x", pady=4)
        ttk.Button(sidebar, text="사용 가이드", command=self.open_guide).pack(fill="x", pady=4)
        ttk.Label(sidebar, text="이 제품 전용 로컬 저장소\n기존 CUA 설정과 연결하지 않습니다.", style="Card.TLabel", foreground="#617889", wraplength=210).pack(anchor="w", pady=(14, 0))
        main = ttk.Frame(body, style="Card.TFrame", padding=17)
        main.pack(side="left", fill="both", expand=True)
        heading = ttk.Frame(main, style="Card.TFrame")
        heading.pack(fill="x")
        ttk.Label(heading, textvariable=self.workflow_title, style="Section.TLabel").pack(side="left")
        self.rename_button = ttk.Button(heading, text="이름 변경", command=self.rename)
        self.rename_button.pack(side="right")
        self.latest_button = ttk.Button(heading, text="최신 수정본 보기", command=self.show_latest)
        self.latest_button.pack(side="right", padx=(0, 6))
        ttk.Label(main, textvariable=self.workflow_meta, style="Card.TLabel", foreground="#617889").pack(anchor="w", pady=(4, 10))
        self.parameter_frame = ttk.Frame(main, style="Card.TFrame")
        self.parameter_frame.pack(fill="x", pady=(0, 10))
        table_frame = ttk.Frame(main, style="Card.TFrame")
        table_frame.pack(fill="both", expand=True)
        columns = ("number", "step", "action", "recognition", "completion")
        self.steps = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse", height=2)
        titles = {"number": "단계", "step": "작업", "action": "동작", "recognition": "대상 인식", "completion": "결과 확인"}
        widths = {"number": 43, "step": 210, "action": 85, "recognition": 170, "completion": 150}
        for name in columns:
            self.steps.heading(name, text=titles[name])
            self.steps.column(name, width=widths[name], minwidth=widths[name] if name == "number" else 65, stretch=name != "number")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.steps.yview)
        self.steps.configure(yscrollcommand=scrollbar.set)
        self.steps.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.steps.tag_configure("manual", background="#fff3db")
        self.steps.tag_configure("region", background="#eff5ff")
        self.steps.tag_configure("verified", background="#e7f5ee")
        self.steps.bind("<<TreeviewSelect>>", lambda _e: self.show_step_detail())
        self.steps.bind("<Double-1>", lambda _e: self.edit_step())
        detail_frame = ttk.Frame(main, style="Card.TFrame", padding=(0, 10, 0, 0))
        detail_frame.pack(fill="x")
        self.detail = tk.Text(detail_frame, height=4, wrap="word", bg="#f3f7fa", fg="#344e62", font=("맑은 고딕", 9), relief="flat", padx=12, pady=10)
        self.detail.pack(fill="x")
        self._detail("단계를 선택하면 대상, 지원 동작과 완료 조건을 확인할 수 있습니다.\n녹화 중의 요소 후보 표시는 재생 성공을 뜻하지 않습니다.")
        controls = ttk.Frame(main, style="Card.TFrame", padding=(0, 10, 0, 0))
        controls.pack(fill="x")
        self.edit_button = ttk.Button(controls, text="단계 수정", command=self.edit_step)
        self.edit_button.pack(side="left")
        self.repair_button = ttk.Button(controls, text="이 단계 다시 녹화", command=lambda: self.record(repair=True))
        self.repair_button.pack(side="left", padx=6)
        self.move_up_button = ttk.Button(controls, text="↑", width=3, command=lambda: self.move_step(-1))
        self.move_up_button.pack(side="left")
        self.move_down_button = ttk.Button(controls, text="↓", width=3, command=lambda: self.move_step(1))
        self.move_down_button.pack(side="left", padx=3)
        self.delete_button = ttk.Button(controls, text="단계 제외", command=self.delete_step)
        self.delete_button.pack(side="left", padx=3)
        actions = ttk.Frame(main, style="Card.TFrame", padding=(0, 14, 0, 0))
        actions.pack(fill="x")
        self.run_button = ttk.Button(actions, text="작업 실행", style="Accent.TButton", command=self.run_workflow)
        self.run_button.pack(side="left")
        self.pause_button = ttk.Button(actions, text="일시정지 / 중단", command=self.cancel, state="disabled")
        self.pause_button.pack(side="left", padx=7)
        self.resume_button = ttk.Button(actions, text="실행 기록 / 이어하기", command=self.choose_run)
        self.resume_button.pack(side="left")
        recovery = ttk.Frame(main, style="Card.TFrame", padding=(0, 6, 0, 0))
        recovery.pack(fill="x")
        self.repair_resume_button = ttk.Button(recovery, text="수정본으로 이어하기", command=self.resume_repaired)
        self.repair_resume_button.pack(side="left")
        self.confirm_button = ttk.Button(recovery, text="현재 단계 직접 확인 후 계속", command=self.confirm_pending)
        self.confirm_button.pack(side="right")
        footer = ttk.Frame(self, padding=(22, 9, 22, 13))
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status, wraplength=1120).pack(anchor="w")
        self.edit_controls = [self.record_button, self.import_button, self.rename_button, self.edit_button,
            self.repair_button, self.move_up_button, self.move_down_button, self.delete_button, self.run_button,
            self.resume_button, self.repair_resume_button, self.confirm_button, self.refresh_button, self.latest_button]
        self._set_busy(False)

    def _detail(self, text):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def _set_busy(self, busy):
        self.busy = busy
        for control in getattr(self, "edit_controls", []):
            control.configure(state="disabled" if busy or self.demo else "normal")
        if self.history_view:
            self.run_button.configure(state="disabled")
        if not self.workflow:
            self.latest_button.configure(state="disabled")
        if self.run and self.run.get("diagnostic") == "completed_prefix_condition_changed":
            self.confirm_button.configure(state="disabled")
        self.pause_button.configure(state="normal" if busy and not self.demo else "disabled")
        self.window_combo.configure(state="disabled" if busy else "readonly")

    def _background(self, function, done):
        if self.busy:
            return
        self._set_busy(True)
        self.stop_event.clear()
        def worker():
            try:
                result = function()
            except Exception as exc:
                self.events.put(("error", _safe_reason(exc)))
            else:
                self.events.put(("done", (done, result)))
        threading.Thread(target=worker, daemon=True).start()

    def _drain(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "error":
                    self._set_busy(False)
                    self.status.set("작업을 중단했습니다: " + value)
                    messagebox.showerror(APP_NAME, "작업을 완료하지 못했습니다.\n" + value, parent=self)
                elif kind == "done":
                    self._set_busy(False)
                    callback, result = value
                    try:
                        callback(result)
                    except Exception as exc:
                        self.status.set("결과를 불러오지 못했습니다: " + _safe_reason(exc))
                        messagebox.showerror(APP_NAME, _safe_reason(exc), parent=self)
                elif kind == "progress":
                    self._progress(value)
        except queue.Empty:
            pass
        self.after(70, self._drain)

    def refresh_windows(self):
        if self.demo or self.busy:
            return
        from desktop import inventory
        self.status.set("열려 있는 업무 창을 확인합니다…")
        self._background(lambda: inventory(self.store.root / "observations"), self._show_windows)

    def _show_windows(self, windows):
        self.windows = windows
        values = [f"{w.get('title') or '(제목 없음)'}  |  {w['exe_name']}  |  PID {w['pid']} / 창 {w['hwnd']}" for w in windows]
        self.window_combo["values"] = values
        if values:
            old = self.target_var.get()
            self.window_combo.current(values.index(old) if old in values else 0)
        else:
            self.target_var.set("")
        if not self.demo:
            self.status.set(f"{len(windows)}개 창을 찾았습니다. 실제 작업할 업무 창을 선택하세요.")

    def binding(self):
        index = self.window_combo.current()
        if index < 0 or index >= len(self.windows):
            messagebox.showinfo(APP_NAME, "먼저 대상 업무 창을 선택하세요.", parent=self)
            return None
        return {key: self.windows[index][key] for key in ("pid", "hwnd", "exe_name")}

    def refresh_workflows(self, selected_id=None):
        self.workflows = self.store.list_workflows()
        self.workflow_list.delete(*self.workflow_list.get_children())
        for workflow in self.workflows:
            self.workflow_list.insert("", "end", iid=workflow["id"], text=workflow["name"])
        if self.workflows:
            available = {w["id"] for w in self.workflows}
            selected_id = selected_id if selected_id in available else self.workflows[0]["id"]
            self.workflow_list.selection_set(selected_id)
            self.workflow_list.focus(selected_id)
            self._select_workflow(force=True)

    def _select_workflow(self, force=False):
        if self.busy and not force:
            return
        ids = self.workflow_list.selection()
        if not ids:
            return
        workflow = self.store.load(ids[0])
        # Tk queues selection notifications. A second notification must not erase
        # a pending run just restored after saving a repair revision.
        if not force and self.workflow and workflow["id"] == self.workflow["id"] and workflow["revision"] == self.workflow["revision"]:
            return
        self.workflow = workflow
        self.run = None
        self.history_view = False
        self.workflow_title.set(self.workflow["name"])
        count = len(self.workflow["steps"])
        self.workflow_meta.set(f"수정본 {self.workflow['revision']}  /  {count}단계  /  {self.workflow['target']['exe_name']}")
        self._render_parameters(self.workflow.get("parameters", {}))
        self._render_steps()
        self._set_busy(self.busy)

    def _render_parameters(self, values, *, readonly=False):
        for child in self.parameter_frame.winfo_children():
            child.destroy()
        self.param_vars = {}
        for index, (name, value) in enumerate(values.items()):
            ttk.Label(self.parameter_frame, text=name, style="Card.TLabel").grid(row=index // 3, column=(index % 3) * 2, sticky="w", padx=(0, 6), pady=4)
            variable = tk.StringVar(value=str(value))
            self.param_vars[name] = variable
            ttk.Entry(self.parameter_frame, textvariable=variable, width=17,
                      state="readonly" if readonly else "normal").grid(row=index // 3, column=(index % 3) * 2 + 1, padx=(0, 17), pady=4, sticky="ew")

    def show_latest(self):
        if self.demo or self.busy or not self.workflow:
            return
        self.refresh_workflows(self.workflow["id"])
        self.status.set("최신 수정본을 불러왔습니다. 단계와 입력 조건을 확인한 뒤 작업 실행을 선택하세요.")

    def _render_steps(self, selected_id=None):
        self.steps.delete(*self.steps.get_children())
        if not self.workflow:
            return
        runtime = {s["id"]: s for s in self.run.get("steps", [])} if self.run else {}
        for index, step in enumerate(self.workflow["steps"]):
            result = runtime.get(step["id"], {})
            kind = step.get("target", {}).get("kind", "manual")
            completion = STATE_LABELS.get(result.get("status"), EXPECT_LABELS.get(step.get("expect", {}).get("kind"), "미설정"))
            tags = ("verified",) if result.get("status") == "verified" else (kind,)
            self.steps.insert("", "end", iid=step["id"], values=(index + 1, step["label"], ACTION_LABELS.get(step["action"], step["action"]), recognition_text(step), completion), tags=tags)
        if selected_id and self.steps.exists(selected_id):
            self.steps.selection_set(selected_id)
            self.steps.see(selected_id)

    def selected_step(self):
        ids = self.steps.selection()
        if not self.workflow or not ids:
            return None
        return next((s for s in self.workflow["steps"] if s["id"] == ids[0]), None)

    def show_step_detail(self):
        step = self.selected_step()
        if not step:
            return
        rec = step.get("recognition", {})
        lines = [f"대상: {target_text(step.get('target', {}))}",
            f"인식: {rec.get('target', '미확인')}  /  조작: {rec.get('action', '미확인')}  /  결과: {rec.get('result', '미확인')}"]
        if step.get("parameter"):
            lines.append(f"입력 변수: {step['parameter']} (현재 {_parameter_value(step, self.workflow)})")
        elif "value" in step:
            lines.append("설정값: " + (json.dumps(step["value"], ensure_ascii=False) if step["value"] != "" else "빈 문자열 (값 없음)"))
        if rec.get("reason"):
            lines.append("보완 안내: " + _safe_reason(rec["reason"]))
        if self.run:
            result = next((s for s in self.run.get("steps", []) if s["id"] == step["id"]), {})
            if result.get("reason"):
                lines.append("실행 상태: " + _safe_reason(result["reason"]))
            if result.get("result", {}).get("diagnostic"):
                lines.append("실행 근거: " + _safe_reason(result["result"]["diagnostic"]))
        self._detail("\n".join(lines))

    def record(self, repair=False):
        if self.demo or self.busy:
            return
        binding = self.binding()
        if not binding:
            return
        original = copy.deepcopy(self.workflow) if repair else None
        selected = self.selected_step() if repair else None
        if repair and not selected:
            messagebox.showinfo(APP_NAME, "다시 녹화할 단계를 먼저 선택하세요.", parent=self)
            return
        from desktop import record
        folder = self.store.root / "recording-sessions" / uuid.uuid4().hex
        folder.mkdir(parents=True, exist_ok=False)
        self.status.set("별도 녹화창을 준비합니다. 녹화창에서 시작을 누르고 업무를 직접 수행하세요.")
        def capture():
            return record(binding, folder, on_progress=lambda event: self.events.put(("progress", event)), stop_event=self.stop_event)
        def done(path):
            if not path:
                self.status.set("녹화를 취소했습니다. 저장된 작업은 변경하지 않았습니다.")
                return
            imported = self.store.import_recording(Path(path))
            if repair:
                self._replace_from_recording(original, selected, imported)
            else:
                self.refresh_workflows(imported["id"])
                self.status.set("녹화가 저장됐습니다. 보완 표시와 완료 조건을 확인한 뒤 실행하세요.")
        self._background(capture, done)

    def _replace_from_recording(self, original, selected, captured):
        choices = captured.get("steps", [])
        if not choices:
            self.status.set("보완 녹화에 단계가 없습니다. 기존 단계는 유지했습니다.")
            return
        dialog = tk.Toplevel(self)
        dialog.title("대체할 녹화 단계 선택")
        dialog.transient(self)
        dialog.geometry("630x330")
        ttk.Label(dialog, text="방금 녹화한 동작 중 기존 단계를 대체할 하나를 선택하세요.", padding=15).pack(anchor="w")
        chooser = tk.Listbox(dialog, font=("맑은 고딕", 11), activestyle="none")
        chooser.pack(fill="both", expand=True, padx=15)
        for index, step in enumerate(choices):
            chooser.insert("end", f"{index + 1}. {step['label']} — {ACTION_LABELS.get(step['action'], step['action'])} / {recognition_text(step)}")
        chooser.selection_set(0)
        def commit():
            pick = chooser.curselection()
            if not pick:
                return
            replacement = copy.deepcopy(choices[pick[0]])
            replacement["id"] = selected["id"]
            replacement["label"] = selected["label"]
            replacement["expect"] = copy.deepcopy(selected.get("expect", {"kind": "manual"}))
            if selected.get("parameter") and replacement.get("action") in {"set-value", "select", "type"}:
                replacement["parameter"] = selected["parameter"]
            updated = copy.deepcopy(original)
            updated["steps"] = [replacement if s["id"] == selected["id"] else s for s in updated["steps"]]
            if captured.get("recording_id"):
                updated["recording_id"] = captured["recording_id"]
            try:
                saved = self.store.save(updated, expected_revision=original["revision"])
            except Exception as exc:
                messagebox.showerror(APP_NAME, _safe_reason(exc), parent=dialog)
                return
            dialog.destroy()
            pending_run = self.run
            self.refresh_workflows(saved["id"])
            if pending_run and pending_run.get("workflow_id") == saved["id"]:
                self.run = pending_run
            self.steps.selection_set(selected["id"])
            self.status.set("선택 단계만 새 수정본으로 저장했습니다. 원본 녹화와 이전 수정본은 보존됩니다.")
        ttk.Button(dialog, text="선택한 동작으로 이 단계 교체", style="Accent.TButton", command=commit).pack(pady=14)
        dialog.grab_set()

    def _progress(self, event):
        state = event.get("state", event.get("status", "running"))
        last = event.get("last_step") or {}
        if last:
            rec = last.get("recognition", {})
            label = last.get("label", ACTION_LABELS.get(last.get("action"), "동작"))
            self.status.set(f"{STATE_LABELS.get(state, state)} / {event.get('event_count', 0)}단계 / {label} / 보완 {event.get('warning_count', 0)}건")
            self._detail(f"방금 기록: {label}\n대상: {rec.get('target', '미확인')} / 조작: {rec.get('action', '미확인')} / 결과: {rec.get('result', '미확인')}\n{rec.get('reason', '')}\n이 표시는 재생 준비 상태이며, 사용자가 수행한 동작의 성공 여부를 대신하지 않습니다.")
        else:
            self.status.set(STATE_LABELS.get(state, state) + " / " + str(event.get("message", event.get("reason", "현재 단계의 조건을 확인합니다."))))
        step_id = event.get("step_id", event.get("current_step_id"))
        if step_id and self.steps.exists(step_id):
            self.steps.selection_set(step_id)
            self.steps.see(step_id)

    def import_file(self):
        if self.demo or self.busy:
            return
        path = filedialog.askopenfilename(parent=self, title="이 제품의 녹화/작업 JSON 선택", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            saved = self.store.import_recording(Path(path))
        except Exception as exc:
            messagebox.showerror(APP_NAME, _safe_reason(exc), parent=self)
            return
        self.refresh_workflows(saved["id"])
        self.status.set("원본을 보관하고 작업을 가져왔습니다. CUA 녹화 파일의 자동 호환을 보장하지 않습니다.")

    def rename(self):
        if self.demo or self.busy or not self.workflow:
            return
        name = simpledialog.askstring(APP_NAME, "작업 이름", initialvalue=self.workflow["name"], parent=self)
        if name and name.strip():
            updated = copy.deepcopy(self.workflow)
            updated["name"] = name.strip()
            self._save_revision(updated)

    def _save_revision(self, updated, selected_id=None):
        pending_run = self.run
        try:
            saved = self.store.save(updated, expected_revision=self.workflow["revision"])
        except Exception as exc:
            messagebox.showerror(APP_NAME, _safe_reason(exc), parent=self)
            return False
        self.refresh_workflows(saved["id"])
        if pending_run and pending_run.get("workflow_id") == saved["id"]:
            self.run = pending_run
        self._set_busy(self.busy)
        if selected_id and self.steps.exists(selected_id):
            self.steps.selection_set(selected_id)
        self.status.set(f"수정본 {saved['revision']}을 저장했습니다. 이전 기록은 유지되며 진행 중 실행에 소급 적용되지 않습니다.")
        return True

    def move_step(self, offset):
        if self.demo or self.busy:
            return
        selected = self.selected_step()
        if not selected:
            return
        updated = copy.deepcopy(self.workflow)
        index = next(i for i, s in enumerate(updated["steps"]) if s["id"] == selected["id"])
        other = index + offset
        if 0 <= other < len(updated["steps"]):
            updated["steps"][index], updated["steps"][other] = updated["steps"][other], updated["steps"][index]
            self._save_revision(updated, selected["id"])

    def delete_step(self):
        if self.demo or self.busy:
            return
        selected = self.selected_step()
        if not selected:
            return
        if not messagebox.askyesno(APP_NAME, "새 수정본에서 이 단계를 제외할까요? 원본과 이전 수정본은 유지됩니다.", parent=self):
            return
        updated = copy.deepcopy(self.workflow)
        updated["steps"] = [s for s in updated["steps"] if s["id"] != selected["id"]]
        self._save_revision(updated)

    def edit_step(self):
        if self.demo or self.busy:
            return
        selected = self.selected_step()
        if not selected:
            return
        editor = StepEditor(self, selected, self.workflow)
        self.wait_window(editor)
        if editor.result is not None:
            step, parameters = editor.result
            updated = copy.deepcopy(self.workflow)
            updated["steps"] = [step if s["id"] == selected["id"] else s for s in updated["steps"]]
            updated["parameters"] = parameters
            self._save_revision(updated, selected["id"])

    def run_workflow(self):
        if self.demo or self.busy or not self.workflow:
            return
        if self.history_view:
            self.status.set("실행 기록을 보고 있습니다. 새 실행은 [최신 수정본 보기]에서 내용과 입력 조건을 확인한 뒤 시작하세요.")
            return
        binding = self.binding()
        if not binding:
            return
        from desktop import DesktopAdapter
        from workflows import Runner
        self.runner = Runner(self.store, DesktopAdapter, on_event=lambda event: self.events.put(("progress", event)))
        parameters = {name: variable.get() for name, variable in self.param_vars.items()}
        self.status.set("현재 요소를 찾고 단계별로 실행합니다. 물리 입력 단계에서는 마우스와 키보드를 함께 사용하지 마세요.")
        workflow_id = self.workflow["id"]
        expected_revision = self.workflow["revision"]
        self._background(lambda: self.runner.start(workflow_id, binding, parameters,
                         expected_revision=expected_revision), self._run_done)

    def _run_done(self, run):
        self.run = run
        self.history_view = True
        self.workflow = copy.deepcopy(run.get("workflow", self.workflow))
        if self.workflow:
            self.workflow_title.set(self.workflow["name"])
            self.workflow_meta.set(f"실행 당시 수정본 {self.workflow['revision']}  /  {len(self.workflow['steps'])}단계  /  {self.workflow['target']['exe_name']}")
            self._render_parameters(run.get("parameters", self.workflow.get("parameters", {})), readonly=True)
        self._render_steps(run.get("current_step_id"))
        self._set_busy(self.busy)
        label = STATE_LABELS.get(run.get("status"), run.get("status", "실행 기록 저장"))
        self.status.set(label + " / 실행 기록 " + run.get("id", "")[:8])
        self.show_step_detail()
        if run.get("status") == "needs_review":
            current = next((s for s in run.get("steps", []) if s["id"] == run.get("current_step_id")), {})
            code = run.get("diagnostic") or current.get("result", {}).get("diagnostic") or current.get("reason")
            if code == "action_not_completed" and current.get("result", {}).get("diagnostic"):
                code = current["result"]["diagnostic"]
            reason = _safe_reason(code) if code else "현재 단계의 대상과 결과를 확인하세요."
            if code == "completed_prefix_condition_changed":
                self._detail(reason)
            else:
                delivery = ("현재 단계에는 입력을 보내지 않았습니다." if current.get("result", {}).get("input_dispatched") is False
                            else "이미 전달됐을 수 있는 입력은 자동 반복하지 않습니다.")
                self._detail("현재 단계에 확인이 필요합니다. " + delivery + "\n" + reason + "\n실제 결과를 확인한 단계만 [현재 단계 직접 확인 후 계속]을 선택하세요.")

    def cancel(self):
        self.stop_event.set()
        if self.runner:
            self.runner.cancel()
        self.status.set("중단을 요청했습니다. 이미 전달된 입력은 취소되지 않으며 결과를 확인한 뒤 이어갈 수 있습니다.")

    def choose_run(self):
        if self.demo or self.busy or not self.workflow:
            return
        runs = self.store.list_runs(self.workflow["id"])
        if not runs:
            messagebox.showinfo(APP_NAME, "이 작업의 실행 기록이 없습니다.", parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title("실행 기록 선택")
        dialog.transient(self)
        dialog.geometry("690x340")
        ttk.Label(dialog, text="실행은 시작 당시 수정본으로 이어집니다. 이미 전달된 동작은 반복하지 않습니다.", padding=14).pack(anchor="w")
        listing = tk.Listbox(dialog, font=("맑은 고딕", 10), activestyle="none")
        listing.pack(fill="both", expand=True, padx=14)
        for run in runs:
            listing.insert("end", f"{run['id'][:8]}  {STATE_LABELS.get(run['status'], run['status'])}  /  {run.get('completed_steps', 0)}/{run.get('total_steps', 0)}단계")
        listing.selection_set(0)
        def choose(resume):
            selected = listing.curselection()
            if not selected:
                return
            self.run = self.store.load_run(runs[selected[0]]["id"])
            self._run_done(self.run)
            dialog.destroy()
            if resume:
                self._resume()
        buttons = ttk.Frame(dialog, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="기록 보기", command=lambda: choose(False)).pack(side="left")
        ttk.Button(buttons, text="상태 확인하며 이어하기", style="Accent.TButton", command=lambda: choose(True)).pack(side="right")
        dialog.grab_set()

    def _resume(self, confirmed=None):
        if not self.run or self.busy or self.demo:
            return
        binding = self.binding()
        if not binding:
            return
        from desktop import DesktopAdapter
        from workflows import Runner
        self.runner = Runner(self.store, DesktopAdapter, on_event=lambda event: self.events.put(("progress", event)))
        run_id = self.run["id"]
        self._background(lambda: self.runner.resume(run_id, binding, confirm_step_id=confirmed), self._run_done)

    def confirm_pending(self):
        if self.demo or self.busy or not self.run:
            return
        if self.run.get("diagnostic") == "completed_prefix_condition_changed":
            messagebox.showinfo(APP_NAME, _safe_reason("completed_prefix_condition_changed"), parent=self)
            return
        step_id = self.run.get("current_step_id")
        if self.run.get("status") != "needs_review" or not step_id:
            messagebox.showinfo(APP_NAME, "직접 확인을 기다리는 단계가 없습니다. 실행 기록에서 이어할 작업을 선택하세요.", parent=self)
            return
        step = next((s for s in self.run.get("workflow", {}).get("steps", []) if s["id"] == step_id), {})
        if messagebox.askyesno(APP_NAME, f"‘{step.get('label', '현재 단계')}’의 결과를 직접 확인하거나 필요한 동작을 직접 완료했나요?\n확인하면 이 단계를 다시 입력하지 않고 다음 단계로 진행합니다.", parent=self):
            self._resume(step_id)

    def resume_repaired(self):
        if self.demo or self.busy or not self.run:
            messagebox.showinfo(APP_NAME, "먼저 실행 기록에서 중단된 실행을 선택하세요.", parent=self)
            return
        binding = self.binding()
        if not binding:
            return
        from desktop import DesktopAdapter
        from workflows import Runner
        self.runner = Runner(self.store, DesktopAdapter, on_event=lambda event: self.events.put(("progress", event)))
        run_id = self.run["id"]
        self.status.set("수정본과 이전 실행을 대조합니다. 이미 전달됐을 수 있는 입력은 다시 실행하지 않습니다.")
        self._background(lambda: self.runner.fork_repaired(run_id, binding), self._run_done)

    def open_guide(self):
        path = ROOT / "사용가이드.html"
        if path.exists() and os.name == "nt":
            os.startfile(path)

    def _close(self):
        if self.busy:
            self.cancel()
            self.status.set("작업 정리를 기다립니다. 정리된 뒤 창을 닫아 주세요.")
            return
        self.destroy()


class StepEditor(tk.Toplevel):
    def __init__(self, parent, step, workflow):
        super().__init__(parent)
        self.result = None
        self.original = copy.deepcopy(step)
        self.workflow = copy.deepcopy(workflow)
        self.title("단계 보완 / 새 수정본으로 저장")
        self.geometry("720x700")
        self.transient(parent)
        self.configure(bg="#edf3f7")
        self.vars = {}
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="한 단계만 수정합니다. 기록 당시 대상과 원본은 이전 수정본에 남습니다.", wraplength=650).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 15))
        def entry(row, key, title, value, choices=None):
            ttk.Label(frame, text=title).grid(row=row, column=0, sticky="w", padx=(0, 15), pady=6)
            var = tk.StringVar(value=str(value))
            self.vars[key] = var
            widget = ttk.Combobox(frame, textvariable=var, values=choices, state="readonly") if choices else ttk.Entry(frame, textvariable=var)
            widget.grid(row=row, column=1, sticky="ew", pady=6)
        entry(1, "label", "단계 이름", step["label"])
        entry(2, "action", "동작", step["action"], tuple(ACTION_LABELS))
        entry(3, "parameter", "입력 변수 이름", step.get("parameter", ""))
        entry(4, "value", "값 / 변수 기본값", _parameter_value(step, workflow))
        entry(5, "expect", "완료 조건", step.get("expect", {}).get("kind", "manual"), tuple(EXPECT_LABELS))
        entry(6, "expected", "기대값 또는 출력 파일", step.get("expect", {}).get("path", step.get("expect", {}).get("value", "")))
        self.headed = tk.BooleanVar(value=bool(step.get("headed", step["action"] in {"type", "focus", "right-click", "double-click"})))
        expected = step.get("expect", {})
        self.expect_input_value = tk.BooleanVar(value=expected.get("kind") == "value"
            and "value" not in expected and "target" not in expected
            and step["action"] in {"set-value", "type", "select"})
        flags = ttk.Frame(frame)
        flags.grid(row=7, column=1, sticky="w", pady=8)
        ttk.Checkbutton(flags, text="입력값을 완료 기대값으로 사용 (입력 변수 사용 시 자동 적용)", variable=self.expect_input_value).pack(anchor="w")
        ttk.Checkbutton(flags, text="대상에 필요한 포커스 / 물리 입력 허용", variable=self.headed).pack(anchor="w")
        ttk.Label(frame, text="현재 대상", font=("맑은 고딕", 10, "bold")).grid(row=8, column=0, sticky="nw", pady=10)
        target = step.get("target", {})
        detail = target_text(target)
        if step.get("expect", {}).get("target"):
            detail += "\n완료 조건 대상: " + target_text(step["expect"]["target"])
        if target.get("kind") == "region":
            point = target.get("point", {})
            detail += f"\n영역 내부 위치: 가로 {point.get('x_ratio', '?')}, 세로 {point.get('y_ratio', '?')} (0~1)"
            bounds = target.get("recorded_bounds", {})
            detail += f"\n기록 당시 영역 크기: {bounds.get('width', '?')} × {bounds.get('height', '?')}"
        ttk.Label(frame, text=detail, wraplength=500).grid(row=8, column=1, sticky="w", pady=10)
        self.region_reviewed = tk.BooleanVar(value=bool(target.get("reviewed", False)))
        if target.get("kind") == "region":
            ttk.Checkbutton(frame, text="기록한 대상 영역과 내부 위치가 의도한 곳임을 직접 확인했습니다.", variable=self.region_reviewed).grid(row=9, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Label(frame, text="대상은 ‘이 단계 다시 녹화’로 지정합니다. 요소 ID나 좌표를 추측해서 입력하지 않습니다. 같은 대상에 입력 변수나 ‘입력값을 완료 기대값으로 사용’을 지정하면 실행 입력값을 확인합니다. 그 외에는 기대값 칸의 빈 문자열도 값으로 저장합니다. 별도 완료 조건 대상의 기대값은 독립적으로 유지합니다.\n‘전달 확인 후 계속’은 업무 결과 확인이 아닙니다. 조회/저장/내보내기에는 실제 완료 조건이나 사람 확인을 지정하세요.", wraplength=660, foreground="#617889").grid(row=10, column=0, columnspan=2, sticky="w", pady=12)
        controls = ttk.Frame(frame)
        controls.grid(row=11, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Button(controls, text="직접 수행 단계로 전환", command=self.make_manual).pack(side="left")
        ttk.Button(controls, text="취소", command=self.destroy).pack(side="right")
        ttk.Button(controls, text="새 수정본 저장", style="Accent.TButton", command=self.save).pack(side="right", padx=8)
        frame.columnconfigure(1, weight=1)
        self.grab_set()

    def make_manual(self):
        self.vars["action"].set("manual")
        self.vars["expect"].set("manual")
        self.vars["parameter"].set("")

    def save(self):
        step = copy.deepcopy(self.original)
        step["label"] = self.vars["label"].get().strip()
        step["action"] = self.vars["action"].get()
        if not step["label"]:
            messagebox.showinfo(APP_NAME, "단계 이름을 입력하세요.", parent=self)
            return
        if self.original.get("target", {}).get("kind") == "manual" and step["action"] != "manual":
            messagebox.showinfo(APP_NAME, "인식되지 않은 대상은 ‘이 단계 다시 녹화’로 실제 대상을 먼저 지정하세요.", parent=self)
            return
        if self.original.get("target", {}).get("kind") == "region" and step["action"] not in {"click", "right-click", "double-click", "manual"}:
            messagebox.showinfo(APP_NAME, "영역 대상은 클릭, 우클릭, 두 번 클릭에 사용할 수 있습니다. 다른 동작은 실제 요소를 다시 녹화하세요.", parent=self)
            return
        parameter = self.vars["parameter"].get().strip()
        value = self.vars["value"].get()
        role = str(step.get("target", {}).get("locator", {}).get("role", "")).casefold().replace("_", "").replace(" ", "")
        if role == "combobox" and step["action"] in {"set-value", "type", "select"} and value == "":
            messagebox.showinfo(APP_NAME, _safe_reason("empty_combo_value_requires_review"), parent=self)
            return
        parameters = copy.deepcopy(self.workflow.get("parameters", {}))
        step.pop("parameter", None)
        step.pop("value", None)
        if step["action"] in {"set-value", "type", "select"}:
            if parameter:
                step["parameter"] = parameter
                parameters[parameter] = value
            else:
                step["value"] = value
        elif parameter:
            messagebox.showinfo(APP_NAME, "입력 변수는 값 설정, 입력, 항목 선택 동작에 지정하세요.", parent=self)
            return
        step["headed"] = bool(self.headed.get())
        if step.get("target", {}).get("kind") == "region":
            step["target"]["reviewed"] = bool(self.region_reviewed.get())
        kind = self.vars["expect"].get()
        expected = self.vars["expected"].get()
        step["expect"] = {"kind": kind}
        if kind in {"value", "exists", "checked"} and "target" in self.original.get("expect", {}):
            step["expect"]["target"] = copy.deepcopy(self.original["expect"]["target"])
        if kind == "file":
            if not expected.strip():
                messagebox.showinfo(APP_NAME, "확인할 출력 파일의 전체 경로를 입력하세요.", parent=self)
                return
            step["expect"]["path"] = expected.strip()
        elif kind == "value":
            use_input = (step["action"] in {"set-value", "type", "select"}
                         and (bool(parameter) or self.expect_input_value.get())
                         and "target" not in step["expect"])
            if not use_input:
                step["expect"]["value"] = expected
        elif kind == "checked":
            if expected.casefold() not in {"true", "false", "1", "0", "yes", "no"}:
                messagebox.showinfo(APP_NAME, "체크 기대값은 true 또는 false로 입력하세요.", parent=self)
                return
            step["expect"]["value"] = expected.casefold() in {"true", "1", "yes"}
        if step["action"] == "manual":
            if step.get("target", {}).get("kind") != "element":
                step["target"] = {"kind": "manual", "locator": {}}
            step["expect"] = {"kind": "manual"}
            if step.get("recognition", {}).get("reason") != "combo_value_unobserved":
                step["recognition"] = {"target": "identified" if step["target"]["kind"] == "element" else "manual",
                                       "action": "manual", "result": "manual", "reason": "사용자가 직접 수행하고 확인하는 단계입니다."}
        elif step.get("recognition", {}).get("reason") == "combo_value_unobserved":
            step["recognition"] = {"target": "identified", "action": "configured", "result": "pending",
                                   "reason": "입력값을 보완했습니다. 실행 후 실제 선택값은 별도로 확인합니다."}
        self.result = (step, parameters)
        self.destroy()


def launch(store_path=None, *, demo=False):
    app = WorkflowApp(store_path, demo=demo)
    app.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--store")
    parser.add_argument("--demo", action="store_true", help="In-memory layout preview; no recording or desktop actions")
    parser.add_argument("--close-after", type=int, help="Close preview after milliseconds, for UI smoke only")
    args = parser.parse_args()
    app = WorkflowApp(args.store, demo=args.demo)
    if args.close_after and args.demo:
        app.after(max(100, args.close_after), app.destroy)
    app.mainloop()
