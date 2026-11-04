# Native helper protocol v1

This is a separate product helper. It has no CUA runtime, driver, model or network dependency. The local .NET Framework compiler is needed only to build the source; the Windows distribution includes the compiled EXE.

```
WorkflowNative.exe COMMAND --request ABSOLUTE_JSON --response ABSOLUTE_JSON
```

JSON uses UTF-8. Request maximum 2 MiB. Files and ancestors must be on a local fixed drive without reparse points. stdout contains no business content. Detailed local response errors contain fixed diagnostic codes. The Python caller must apply a bounded subprocess timeout to observation/action calls. Recording is a deliberately long-lived interactive helper.

## Target binding

All targeted operations require `pid` integer, `hwnd` integer and exact `exe_name` basename. Windows session, HWND ownership, executable basename and process creation time are checked. `process_started_utc_ticks` is a decimal **string** returned by inventory/inspection. It is required for `activate` and `point`; when present on other commands it is checked too.

## windows

Request `{}`. Response `{ok:true,status:"observed",data:[...]}`. Each visible current-session window has pid, hwnd, exe_name, title, class_name, owner_hwnd, root_owner_hwnd, process_started_utc_ticks, visible, minimized, dpi. Ownership comes from GW_OWNER and GA_ROOTOWNER. Unrelated business windows are only inventory candidates, never implicitly included in an operation.

## inspect

Request target binding. Response `{ok:true,status:"observed",data:{...binding,nodes:[...],complete:bool}}`. Uses the specified window's UIA ControlView, maximum 2,500 nodes and depth 18. Truncation or unavailable children sets complete false. No input is sent.

Each node: name, canonical role, automation_id, value (string or null, empty preserved), checked (bool or null), enabled, offscreen, secure, states, available_actions, locator, bounds, bounds_valid, hwnd, path, parent_path. Roles include textfield, combobox, button, checkbox, menuitem, menu, group, table, row, window. Bounds are current desktop coordinates `{x,y,width,height}`. Empty, nonfinite or unusable UIA rectangles are represented by zero-area bounds with `bounds_valid:false` and `offscreen:true`; this is invalid geometry, never a substitute click location. JSON contains no NaN/Infinity. Structural noninteractive nodes are included even when agent-desktop issues no ref.

Locator uses stable nonnumeric automation_id when available, name, role, and up to four significant ancestors ordered **nearest parent outward**. Ancestors omit unnamed containers and the inspected window root. These are observed runtime identifiers, not fields inferred from static source.

## probe

Request target binding plus `cursor:true`, or x/y integers. This is read-only. The point must belong to the target window or an owned same-process popup. Response data is one node and `window:{hwnd,title,class_name}`. This operation never sends input.

## activate

Request complete target binding including process_started_utc_ticks. It restores a minimized target using ShowWindow and requests SetForegroundWindow. It does not bypass Windows foreground restrictions. Response has foreground_verified and input_dispatched true if activation was attempted. A false foreground result is not permission to send physical input.

## point

Request complete target binding plus `locator`, `point:{x_ratio,y_ratio}` and action click/right-click/double-click. Ratios must be between 0.02 and 0.98. The point is calculated from the uniquely resolved **current** non-window UIA region. No absolute screen-coordinate input is accepted.

Before one SendInput call the helper verifies: complete scoped tree; unique locator; nonpassword enabled onscreen target; visible/nonminimized window; foreground target (or owner for popup); WindowFromPoint in exact target window/child and same PID; UIA hit-test inside selected element with nonpassword hit control; no held modifiers/mouse buttons; unchanged process lifetime and bounds. Current DPI-aware virtual desktop geometry is used. No retries or alternate-target fallback.

Success: `{ok:true,status:"executed_unverified",input_dispatched:true,delivery:"delivered_unverified",retry:"unsafe",business_commit:"not_verified"}`. Partial SendInput/exception after the dispatch boundary is uncertain with nullable input_dispatched. Physical delivery never proves business completion.

## record

Request target binding plus existing output_dir, progress_path, draft_path, optional stop_path and name. Default opens a recorder in ready state. Start installs temporary low-level hooks; pause/finish/close unhooks. `auto_start:true` explicitly starts immediately and is useful for the synthetic acceptance harness. No hooks persist beyond this process.

The native window keeps a step list visible with target recognition, supported action and result-verification status. Only selected window and verified same-process owned popups are recorded. Other applications and the helper UI are excluded. `outside_target` is reported without other-window content. Password values are never recorded. The helper records final ValuePattern values rather than raw typed characters. Unsupported keyboard transitions, scrolling, missing stable identity and incomplete reads produce explicit manual steps.

Progress is atomically replaced, roughly every 250 ms: `{state,event_count,warning_count,partial,last_step?,hover_target?,diagnostic?}`. States: ready, recording, paused, outside_target, review, failed, cancelled. `last_step` has the workflow step including recognition. `hover_target` is an optional locator observed within the selected target while recording. A background worker observes the current cursor location to preserve pre-click identity when a popup covers the original control. If needed, the child HWND witnessed at mouse-down can supply the original named region; a window root cannot. Recognition is recording evidence, not proof of successful replay. Delayed UIA reads report accessibility_read_delayed and finishing stops waiting after a bounded interval, preserving an explicit pending manual step. Callers should retain previous progress and retry a transient sharing violation while this file is replaced.

Final draft_path is direct workflow JSON v1. A separate `recording-<uuid>.events.jsonl` append-only file preserves original step events and later step revisions. Final response `{ok:true,status:"recorded",draft_path,recording_path,event_count,warning_count,partial}`. Cancel returns status cancelled and does not create a workflow draft. Creating stop_path requests finish with partial preservation; the caller should allow several seconds before killing a hung helper.

Region steps capture the witnessed region's locator, normalized point and recorded bounds, with `region_review_required`. They require review before replay. Clicks outside the 0.02–0.98 interior become explicit manual steps with `region_edge_requires_retarget`; the recorder does not silently move them inward. A window-root click is not substituted for an unidentified control. Element value and checkbox steps include independently checkable expected values; generic buttons default to manual completion until a user specifies the next expected state. Menu items default to delivery-only verification, which does not claim business completion. Menu popup windows are stored in step.target.window when distinct from the source window. Unowned same-PID business windows are excluded.

Known limits: native UIA cannot reconstruct children a provider does not expose. Popup controls disappearing before point observation or state inspection become manual steps. Hover-only submenu transitions are not synthesized into clicks. Arbitrary shortcuts and scrolling are currently explicit manual steps, not silently dropped. Pixel recognition and OCR are not implemented by this helper.

## Build and synthetic acceptance

`tools/build_native.ps1` uses the installed .NET Framework compiler with explicit UTF-8 source encoding and writes `bin/native-manifest.json` containing version, binary SHA-256 and source SHA-256. No downloads, execution-policy changes, drivers or installation are required.

`python tools/smoke_native.py --work-dir ABSOLUTE_LOCAL_DIRECTORY` compiles and opens only `tests/fixtures/NativeFixture.cs`. It exercises real UIA, protected-value withholding, a structural region without an action ref, guarded right-click after movement/resizing, lifetime and foreground rejection, actual passive recording of a final value and region right-click, visible warning progress, immutable events and recorder shutdown. The test requires an interactive Windows desktop. It never proves actual FX/FarPoint behavior; company-PC acceptance remains separate.
