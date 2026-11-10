# Python helper protocol v1

Agent Desktop Workflows 0.1.6 uses three inspectable Python files instead of its own compiled helper: `scripts/py_helper.py`, `scripts/win_bridge.py`, and `scripts/recorder.py`. The supported runtime is x64 Python 3.10 or later on x64 Windows; the recorder UI also needs tkinter. Python's standard library calls Windows' built-in UI Automation COM and Win32 APIs. No CUA runtime, driver, model, third-party Python package, helper compiler, or runtime download is required.

The Windows ZIP still includes the unchanged upstream `bin/agent-desktop.exe` for semantic element actions. The source ZIP includes no EXE. A user can manually copy the previously obtained Agent Desktop Local 0.2.0 copy into this separate product's `bin` directory after checking its SHA-256 against `UPSTREAM.json`. The product does not search or import another installation. Removing the additional helper EXE changes the distribution; it does not override company rules governing Python, hooks, or desktop input.

## Invocation and source integrity

The desktop adapter invokes the current Python interpreter with an isolated module path and no bytecode writes:

```
python -I -X utf8 -B scripts/py_helper.py COMMAND --request ABSOLUTE_JSON --response ABSOLUTE_JSON
```

Use the public workflow CLI rather than invoking the helper to bypass workflow checks. The adapter checks all three source hashes against `engine/bridge-manifest.json` before every helper process. The manifest carries the installed product version and SHA-256 of each bridge source. `tools/build.py` refreshes it from source when producing release archives; it does not compile source or create executables. These files use LF line endings so Git checkout conversion does not invalidate their hashes. A hash mismatch fails closed; the runtime does not regenerate the manifest.

Release archives also contain `PACKAGE-MANIFEST.json` with the product, version, distribution and hashes of all allowlisted files. The build verifier rejects extra files and enforces exactly one pinned EXE in the Windows archive and none in the source archive. Hashes detect mismatch or corruption; they are not a digital signature or a sandbox against a party able to replace both source and manifests.

JSON uses UTF-8. Requests are limited to 2 MiB and responses to 16 MiB. Files and ancestors must be on a local fixed drive without reparse points. stdout and stderr carry no business content. Local responses use fixed diagnostic codes. Observation and action processes have bounded timeouts; recording is deliberately interactive and longer lived. The existing `native-*` local request directory names and internal adapter function names remain for compatibility; they no longer launch a compiled product helper.

## Target binding

Targeted operations require `pid` integer, `hwnd` integer and exact `exe_name` basename. Windows session, HWND ownership, executable basename and process creation time are checked. `process_started_utc_ticks` is a decimal string returned by inventory or inspection. It is required for `activate`, `point` and `combo-option`; when present on other commands it is checked too.

## windows

Request `{}`. Response `{ok:true,status:"observed",data:[...]}`. Visible current-session windows include pid, hwnd, exe_name, title, class_name, owner_hwnd, root_owner_hwnd, process_started_utc_ticks, visible, minimized and dpi. Ownership comes from GW_OWNER and GA_ROOTOWNER. Unrelated business windows are inventory candidates only, never implicitly included in a targeted operation.

## inspect

Request target binding. Response `{ok:true,status:"observed",data:{...binding,nodes:[...],complete:bool}}`. Uses the specified window's UIA ControlView, maximum 2,500 nodes and depth 18. Truncation or unavailable children sets complete false. No input is sent.

Nodes include name, canonical role, automation_id, value (string or null, empty preserved), checked (bool or null), enabled, offscreen, secure, states, available_actions, locator, bounds, bounds_valid, hwnd, path and parent_path. Password values are withheld. Bounds use current desktop coordinates `{x,y,width,height}`. Empty, nonfinite or unusable rectangles become zero-area bounds with `bounds_valid:false` and `offscreen:true`; they are never substitute click locations. JSON contains no NaN/Infinity. Structural noninteractive nodes are included even when agent-desktop issues no ref.

A locator uses a stable nonnumeric automation_id when available, name, role, and up to four significant ancestors ordered nearest parent outward. Ancestors omit unnamed containers and the inspected window root. These are observed runtime identifiers, not fields inferred from static application source.

For a combo node, `value` is its observed selection rather than an unconditional copy of the parent's ValuePattern. `value_evidence` records `status` (`observed`, `unavailable`, `conflict`) and `source` (`parent`, `selection`, `editor`, `corroborated`, `none`). Nonempty parent values, explicitly selected list items, and a single same-combo edit value are evidence candidates. Empty parent values, unselected option labels and decorative names are not selection evidence. Multiple candidates, conflicting values, protected/foreign content or an incomplete bounded traversal yield no value. Ordinary edit controls continue to preserve intentional empty strings. This does not add OCR or infer values from `[Editor]` names.

## probe

Request target binding plus `cursor:true`, or x/y integers. This is read-only. The point must belong to the target window or an owned same-process popup. Response data is one node and `window:{hwnd,title,class_name}`. This operation never sends input.

## activate

Request complete target binding including process_started_utc_ticks. Restores a minimized target and requests foreground activation without bypassing Windows foreground restrictions. Response includes foreground_verified and input_dispatched true if activation was attempted. A false foreground result is not permission to send physical input.

## point

Request complete target binding plus `locator`, `point:{x_ratio,y_ratio}` and action click/right-click/double-click. Ratios must be between 0.02 and 0.98. The point is calculated from the uniquely resolved current non-window UIA region. No absolute screen-coordinate input is accepted.

Before one SendInput call, the helper checks a complete scoped tree, unique locator, nonpassword enabled onscreen target, visible nonminimized window, expected foreground, matching window and PID at the point, UIA hit-test within the selected element, absence of held modifiers/mouse buttons, and unchanged process lifetime and bounds. Current DPI-aware virtual desktop geometry is used. There are no retries or alternate-target fallbacks.

Success is `{ok:true,status:"executed_unverified",input_dispatched:true,delivery:"delivered_unverified",retry:"unsafe",business_commit:"not_verified"}`. Partial SendInput or an exception after the dispatch boundary is uncertain with nullable input_dispatched. Physical delivery never proves business completion.

## combo-option (0.1.6)

For an element combo with `action:"set-value"`, the optional `combo_replay:"visible_option"` requires `headed:true`. The recorder uses this strategy for clicked choices on expandable combos; existing steps without it retain their original behavior. Editing the workflow can also request this strategy without inventing historical selection evidence. The `combo-option` helper requires a bound window, process lifetime, locator and the exact runtime option value. It inspects the live combo and its scoped visible options, expanding at most once when needed. A clickable option must uniquely match the value and pass a fresh identity, visibility, foreground and hit test. A stale or unclassified action is never retried with a second mechanism.

Option identity uses the clicked item's accessible value (UIA Value or LegacyIAccessible.Value) before its accessible name. A disagreement between value providers cannot identify a choice. An option that is merely present in the inventory is never evidence of a current selection. The run receipt distinguishes opening the popup (`popup_expanded`), dispatching the option click (`option_dispatched`), and observing the selected value after delivery. A nonempty LegacyIAccessible.Value on the combo or its unique editor can supply independent evidence; conflicting value providers block verification. If the app exposes no independent current value, the workflow requires review when the completion condition is a value check.

## record

A combo with no trustworthy value is preserved as `action:manual`, `expect.kind:manual`, `recognition.reason:combo_value_unobserved`. Its identified element target and step ID stay available for editing. A recorded blank combo is not automatically promoted to a valid empty selection. The editor and replay preflight reject empty combo value-setting inputs with `empty_combo_value_requires_review`; the user can supply a real value or parameter in a new revision. Original recordings remain unchanged.

Request target binding plus existing output_dir, progress_path, draft_path, optional stop_path and name. Default opens a tkinter recorder in ready state. Start installs temporary low-level hooks; pause, finish and close unhook. `auto_start:true` explicitly starts immediately for the synthetic acceptance harness. No hooks persist beyond this process. Hook callbacks retain only scoped event metadata; a separate UIA worker owns COM objects, and the UI thread renders copied data.

The recorder displays each step's target recognition, supported action and result-verification status. It records the selected window and verified same-process owned popups. Native ComboLBox candidates without a Win32 owner must also be proven descendants of the selected main window by the UIA worker before their content is recorded. Same PID alone never proves scope. Other applications and independent sibling windows remain excluded. `outside_target` reports departure without other-window content. Password values are never recorded. The helper records observed control values, including the combo selection evidence described above, rather than raw typed characters. Unsupported keyboard transitions, scrolling, missing stable identity and incomplete reads produce explicit manual steps.

Combo arrows and options are associated with their observed parent combo. Independent list-item clicks remain separate actions. Before a new transition, the recorder commits only value evidence observed before that transition; a button's later clearing of an input cannot overwrite the submitted value. If the last typing event is newer than trustworthy value evidence, the existing value step becomes an explicit manual review step with its original ID. Original events remain append-only. Missing-target gaps are placed after prior input, not before it.

Progress is atomically replaced at roughly 200 ms intervals: `{state,event_count,warning_count,partial,last_step?,hover_target?,diagnostic?}`. States include ready, recording, paused, outside_target, review, failed and cancelled. A background worker observes the cursor within the target to preserve pre-click identity when a popup covers the original control. A witnessed child HWND can help identify the original named region; a window root cannot substitute for a target. Recognition is recording evidence, not proof of successful replay. Delayed reads report accessibility_read_delayed. Finishing waits only a bounded interval and preserves pending work as a manual step. Callers retain previous progress across transient file sharing errors.

Final draft_path is workflow JSON schema v1. A separate `recording-<uuid>.events.jsonl` append-only file preserves original steps and later revisions. Final response is `{ok:true,status:"recorded",draft_path,recording_path,event_count,warning_count,partial}`. Cancel returns status cancelled without a workflow draft. Creating stop_path requests finish with partial preservation; the caller allows several seconds before terminating a hung process.

Region steps capture the witnessed region's locator, normalized point and recorded bounds with region_review_required. They require human review before replay. Clicks outside the 0.02–0.98 interior become manual steps with region_edge_requires_retarget; the recorder does not silently move them inward. Element value and checkbox steps carry independently checkable expected values. Generic buttons require a completion condition or manual review; menu delivery alone does not prove business completion. Distinct owned popups are recorded in step.target.window. Unowned same-PID business windows are excluded.

When multiple owned popup windows share a title and class, replay requires exactly one complete current popup tree containing the recorded locator. Ambiguous or incomplete evidence blocks input. It does not select the first matching window.

A confirmed delivered receipt with only `snapshot_unavailable` after the popup closes may be checked against an explicitly configured file/value/checked/exists postcondition once. The original receipt and pre-input baseline are preserved. A match verifies the step without another input; a mismatch pauses. Unknown/partial delivery, other errors, interruption and none/manual conditions never use this recovery path.

For combo value actions with known delivery, the adapter checks the current selection evidence from a complete, uniquely resolved native observation. It preserves `engine_status` and the original engine action files, and records the independent check in `value_readback`. A confirmed value mismatch stays unsuccessful. An unavailable or conflicting value is not a matched empty value. An interruption during readback remains interrupted.

The pinned agent-desktop binary can return `ACTION_FAILED` with `details.kind:post_action_verification` after a successful nonempty combo `ValuePattern.SetValue` because its parent readback is empty. Only the exact single-step `semantic_api` success receipt with consistent `delivered_unverified/unsafe` dispositions, a blank combo post-state and a complete unique after-snapshot is classified as delivered but unverified. The raw `action.json` is unchanged. This is not permission to promote arbitrary errors, unknown delivery, partial input or interruption by looking at a currently matching value. No second input or alternative action is sent.

Known limits: UIA cannot reconstruct children a provider does not expose. Popup controls that disappear before observation may become manual steps. Hover-only submenu transitions are not synthesized into clicks. Double-clicks revise the first click into one manual step with the original step ID, preserving both events instead of replaying two independent clicks. Arbitrary shortcuts and scrolling remain explicit manual steps. Pixel recognition and OCR are not implemented by this helper. Existing recording originals, workflow revisions and storage paths remain compatible with 0.1.0.

## Developer acceptance

### Generic combo selection recording (0.1.5)

`LabeledBy` supplies display-only `semantic_label`; a mutually unique same-parent label/control geometry is a fallback. Execution locators remain control identities, never labels, option inventories, indices or session refs. `SelectionItem.SelectionContainer` can prove an owned popup option belongs to the selected window's combo. Arbitrary unowned same-PID windows remain excluded. `SelectionPattern.GetCurrentSelection` supplements parent/editor/child-selection readback with scope, protection, uniqueness and conflict checks.

The recorder retains bounded in-memory option observations before a click and joins them only to matching mouse-event time, hit window and point. The user choice is separate from its effect. Optional schema-v1 step metadata is `selection:{source:"option_click",value:"N",result:"matched"|"mismatch"|"unavailable",observed_value?:"N"}`. `observed_value` is required for matched/mismatch and absent for unavailable. Values are examples, not application mappings. The metadata is recording history only; runtime parameters and subsequent effects are independently checked. Original recordings and step IDs are retained.

A pending selection gets bounded post-click reads, stopping at the next user transition. Escape does not infer a choice. No readable/cached clicked option means review, not a guessed option. Unknown effects retain the explicit intended value as a set-value step with its value expectation. `combo_selection_unverified` and `combo_selection_mismatch` distinguish these cases in the UI.

On replay, a complete native observation with a unique unprotected combo and unavailable value may use one read-only engine inspect/get. Ref-map/window/protection checks precede the value read; native scope and current value evidence are checked again afterward. Conflicting evidence is never replaced by a convenient match. This fallback dispatches no input and does not promote uncertain delivery or historical recording evidence into success.

`tools/smoke_native.py` retains its historical filename but invokes the Python backend. The smoke tools compile only the supplied synthetic C# fixture apps using an installed local .NET Framework compiler. Those test EXEs are not shipped and are not created during ordinary recording or replay. Interactive desktop tests exercise real UIA and input on those fixtures only; they do not prove FX/FarPoint behavior. See [validation.md](validation.md) for scope and commands.
