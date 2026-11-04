# Python helper protocol v1

Agent Desktop Workflows 0.1.1 replaces its own compiled helper with three inspectable Python files: `scripts/py_helper.py`, `scripts/win_bridge.py`, and `scripts/recorder.py`. The supported runtime is x64 Python 3.10 or later on x64 Windows; the recorder UI also needs tkinter. Python's standard library calls Windows' built-in UI Automation COM and Win32 APIs. No CUA runtime, driver, model, third-party Python package, helper compiler, or runtime download is required.

The Windows ZIP still includes the unchanged upstream `bin/agent-desktop.exe` for semantic element actions. The source ZIP includes no EXE. A user can manually copy the previously obtained Agent Desktop Local 0.2.0 copy into this separate product's `bin` directory after checking its SHA-256 against `UPSTREAM.json`. The product does not search or import another installation. Removing the additional helper EXE changes the distribution; it does not override company rules governing Python, hooks, or desktop input.

## Invocation and source integrity

The desktop adapter invokes the current Python interpreter with an isolated module path and no bytecode writes:

```
python -I -X utf8 -B scripts/py_helper.py COMMAND --request ABSOLUTE_JSON --response ABSOLUTE_JSON
```

Use the public workflow CLI rather than invoking the helper to bypass workflow checks. The adapter checks all three source hashes against `engine/bridge-manifest.json` before every helper process. The manifest is `{version:"0.1.1",files:{"scripts/py_helper.py":"SHA256","scripts/win_bridge.py":"SHA256","scripts/recorder.py":"SHA256"}}`. `tools/build.py` refreshes it from source when producing release archives; it does not compile source or create executables. These files use LF line endings so Git checkout conversion does not invalidate their hashes. A hash mismatch fails closed; the runtime does not regenerate the manifest.

Release archives also contain `PACKAGE-MANIFEST.json` with the product, version, distribution and hashes of all allowlisted files. The build verifier rejects extra files and enforces exactly one pinned EXE in the Windows archive and none in the source archive. Hashes detect mismatch or corruption; they are not a digital signature or a sandbox against a party able to replace both source and manifests.

JSON uses UTF-8. Requests are limited to 2 MiB and responses to 16 MiB. Files and ancestors must be on a local fixed drive without reparse points. stdout and stderr carry no business content. Local responses use fixed diagnostic codes. Observation and action processes have bounded timeouts; recording is deliberately interactive and longer lived. The existing `native-*` local request directory names and internal adapter function names remain for compatibility; they no longer launch a compiled product helper.

## Target binding

Targeted operations require `pid` integer, `hwnd` integer and exact `exe_name` basename. Windows session, HWND ownership, executable basename and process creation time are checked. `process_started_utc_ticks` is a decimal string returned by inventory or inspection. It is required for `activate` and `point`; when present on other commands it is checked too.

## windows

Request `{}`. Response `{ok:true,status:"observed",data:[...]}`. Visible current-session windows include pid, hwnd, exe_name, title, class_name, owner_hwnd, root_owner_hwnd, process_started_utc_ticks, visible, minimized and dpi. Ownership comes from GW_OWNER and GA_ROOTOWNER. Unrelated business windows are inventory candidates only, never implicitly included in a targeted operation.

## inspect

Request target binding. Response `{ok:true,status:"observed",data:{...binding,nodes:[...],complete:bool}}`. Uses the specified window's UIA ControlView, maximum 2,500 nodes and depth 18. Truncation or unavailable children sets complete false. No input is sent.

Nodes include name, canonical role, automation_id, value (string or null, empty preserved), checked (bool or null), enabled, offscreen, secure, states, available_actions, locator, bounds, bounds_valid, hwnd, path and parent_path. Password values are withheld. Bounds use current desktop coordinates `{x,y,width,height}`. Empty, nonfinite or unusable rectangles become zero-area bounds with `bounds_valid:false` and `offscreen:true`; they are never substitute click locations. JSON contains no NaN/Infinity. Structural noninteractive nodes are included even when agent-desktop issues no ref.

A locator uses a stable nonnumeric automation_id when available, name, role, and up to four significant ancestors ordered nearest parent outward. Ancestors omit unnamed containers and the inspected window root. These are observed runtime identifiers, not fields inferred from static application source.

## probe

Request target binding plus `cursor:true`, or x/y integers. This is read-only. The point must belong to the target window or an owned same-process popup. Response data is one node and `window:{hwnd,title,class_name}`. This operation never sends input.

## activate

Request complete target binding including process_started_utc_ticks. Restores a minimized target and requests foreground activation without bypassing Windows foreground restrictions. Response includes foreground_verified and input_dispatched true if activation was attempted. A false foreground result is not permission to send physical input.

## point

Request complete target binding plus `locator`, `point:{x_ratio,y_ratio}` and action click/right-click/double-click. Ratios must be between 0.02 and 0.98. The point is calculated from the uniquely resolved current non-window UIA region. No absolute screen-coordinate input is accepted.

Before one SendInput call, the helper checks a complete scoped tree, unique locator, nonpassword enabled onscreen target, visible nonminimized window, expected foreground, matching window and PID at the point, UIA hit-test within the selected element, absence of held modifiers/mouse buttons, and unchanged process lifetime and bounds. Current DPI-aware virtual desktop geometry is used. There are no retries or alternate-target fallbacks.

Success is `{ok:true,status:"executed_unverified",input_dispatched:true,delivery:"delivered_unverified",retry:"unsafe",business_commit:"not_verified"}`. Partial SendInput or an exception after the dispatch boundary is uncertain with nullable input_dispatched. Physical delivery never proves business completion.

## record

Request target binding plus existing output_dir, progress_path, draft_path, optional stop_path and name. Default opens a tkinter recorder in ready state. Start installs temporary low-level hooks; pause, finish and close unhook. `auto_start:true` explicitly starts immediately for the synthetic acceptance harness. No hooks persist beyond this process. Hook callbacks retain only scoped event metadata; a separate UIA worker owns COM objects, and the UI thread renders copied data.

The recorder displays each step's target recognition, supported action and result-verification status. Only the selected window and verified same-process owned popups are recorded. Other applications and the recorder UI are excluded. `outside_target` reports departure without other-window content. Password values are never recorded. The helper records final ValuePattern values rather than raw typed characters. Unsupported keyboard transitions, scrolling, missing stable identity and incomplete reads produce explicit manual steps.

Progress is atomically replaced at roughly 200 ms intervals: `{state,event_count,warning_count,partial,last_step?,hover_target?,diagnostic?}`. States include ready, recording, paused, outside_target, review, failed and cancelled. A background worker observes the cursor within the target to preserve pre-click identity when a popup covers the original control. A witnessed child HWND can help identify the original named region; a window root cannot substitute for a target. Recognition is recording evidence, not proof of successful replay. Delayed reads report accessibility_read_delayed. Finishing waits only a bounded interval and preserves pending work as a manual step. Callers retain previous progress across transient file sharing errors.

Final draft_path is workflow JSON schema v1. A separate `recording-<uuid>.events.jsonl` append-only file preserves original steps and later revisions. Final response is `{ok:true,status:"recorded",draft_path,recording_path,event_count,warning_count,partial}`. Cancel returns status cancelled without a workflow draft. Creating stop_path requests finish with partial preservation; the caller allows several seconds before terminating a hung process.

Region steps capture the witnessed region's locator, normalized point and recorded bounds with region_review_required. They require human review before replay. Clicks outside the 0.02–0.98 interior become manual steps with region_edge_requires_retarget; the recorder does not silently move them inward. Element value and checkbox steps carry independently checkable expected values. Generic buttons require a completion condition or manual review; menu delivery alone does not prove business completion. Distinct owned popups are recorded in step.target.window. Unowned same-PID business windows are excluded.

Known limits: UIA cannot reconstruct children a provider does not expose. Popup controls that disappear before observation may become manual steps. Hover-only submenu transitions are not synthesized into clicks. Double-clicks revise the first click into one manual step with the original step ID, preserving both events instead of replaying two independent clicks. Arbitrary shortcuts and scrolling remain explicit manual steps. Pixel recognition and OCR are not implemented by this helper. Existing recording originals, workflow revisions and storage paths remain compatible with 0.1.0.

## Developer acceptance

`tools/smoke_native.py` retains its historical filename but invokes the Python backend. The smoke tools compile only the supplied synthetic C# fixture apps using an installed local .NET Framework compiler. Those test EXEs are not shipped and are not created during ordinary recording or replay. Interactive desktop tests exercise real UIA and input on those fixtures only; they do not prove FX/FarPoint behavior. See [validation.md](validation.md) for scope and commands.
