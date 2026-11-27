# Desktop Select 입력 방식과 원본 기능 검토

이 문서는 원본의 기능과 Desktop Select 0.4.29의 입력 정책을 설명한다. 실제 회사 FX/FarPoint에서의 동작과 동시 사용 가능 여부는 해당 환경에서 확인해야 한다.

## 적용하는 입력 정책

일반 콤보 선택, 입력란 값 설정과 버튼/메뉴 호출은 현재 대상이 제공하는 UIA 기능을 사용한다. `anchor`로 지정한 입력란도 현재 창의 상위 요소와 상대 위치로 식별한다. 이 단계들은 다른 창 뒤에서 실행할 수 있고, 대상 창이 최소화된 경우에는 멈춘다. 표 우클릭과 행 버튼에는 실제 마우스를 사용하고, `submit`은 실제 키보드로 Enter를 보낸다.

`--input-plan`은 저장된 동작에서 각 단계가 요구하는 입력 방식을 분류하는 읽기 전용 확인이다. `desktop_select.py replay`와 등록된 Python 업무 스킬의 `scripts/run.py`에서 사용한다. 필수 변수값이나 실행 중인 업무 앱 없이 확인할 수 있으며, 창을 관찰하거나 복원하거나 입력하지 않는다. 저장된 실행 동작의 분류이며 실제 대상의 존재, UIA 지원 여부 또는 업무 결과를 검증하지 않는다. 녹화에서 실행안을 저장하는 `plan --task`나 현재 창을 관찰하는 Python `--read-only`와 구분한다.

`row-launch --ordinal N --preview`는 현재 보이는 목록에서 N번째 행의 실행 버튼을 찾고 입력 없이 돌려준다. 실제 행 실행은 창을 앞으로 가져와 사용자 마우스를 움직인다.

`--background-only`는 `select`, `fill`, `open`, `row-launch`, `replay`와 등록된 Python 실행에서 도구의 물리 입력, 창 전면화와 최소화 복원을 금지한다. 요청한 단계에 우클릭, Enter 제출 또는 행 버튼처럼 실제 입력이 필요한 동작이 포함되면 첫 입력 전에 전체 요청을 차단한다. `--only`로 한 단계만 요청하면 그 선택 범위를 평가한다. 최소화되었거나 지원되는 UIA 경로가 없는 대상에서는 중지하고 이유를 보고한다.

기본 실행은 UIA 세션을 열 때 자동 포커스를 끄고 설정값을 확인한다. 이를 지원하지 않는 환경에서는 해당 호출이 다른 물리 입력과 겹치지 않도록 차례를 기다린다. `--background-only`에서는 실제 조작에 쓰는 클라이언트에서 `AutoSetFocus=False`가 확인되어야 진행한다. [Microsoft AutoSetFocus 문서](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomation2-get_autosetfocus)

이 옵션은 앱 내부 동작을 격리하지 않는다. UIA 버튼 호출에 반응해 앱이 스스로 대화상자를 열거나 포커스를 바꾸는 것까지 막지 않으며, 업무 결과 확인이나 이미 전달된 입력의 재실행 방지 규칙도 유지한다. 입력 계획을 보여 주거나 background-only로 차단한 결과를 업무 실행 성공으로 보고하지 않는다.

## 참조한 원본과 현재 실행기

기존 `computer-use-mcp/agent-desktop-local` 0.2.0은 `vercel-labs/agent-desktop` 1.1.0을 고정한 Python 래퍼다. 저장소의 `UPSTREAM.json`은 커밋 `d3dc190afbf34cd2377252398ddd5c30efbd093a`와 바이너리 해시를 기록한다. 기본 UIA 조작과 `--headed` 물리 입력을 분리하고, `right-click`, `double-click`, `type`, `focus`에는 headed를 요구한다. [agent-desktop 1.1.0 Windows 스킬](https://github.com/vercel-labs/agent-desktop/blob/v1.1.0/skills/agent-desktop-windows/SKILL.md)

Desktop Select는 별도 Python 실행기다. Windows ZIP에 원본 agent-desktop EXE나 CUA Driver를 넣지 않으므로 원본 바이너리의 버전 변경만으로 새 동작이 추가되지 않는다. 이번 반영은 입력 방식을 미리 보여 주고 도구의 물리 입력과 전면화를 제한하는 실행 정책이다. 기존 원본 녹화, 수정본, 필수 변수와 결과 확인 규칙을 유지한다.

## 별도 커서, background 입력, 독립 데스크톱의 차이

| 기능 | 실제 의미 | Desktop Select와의 관계 |
|---|---|---|
| agent-desktop의 headless UIA 조작 | 접근성 API로 대상 기능을 호출하며 사용자 커서를 움직이지 않음 | 일반 UIA 조작과 같은 원칙을 사용 |
| agent-desktop의 cursor overlay | 투명하고 클릭을 통과시키는 창에 조작 위치를 그림 | 표시 기능이며 입력 능력을 추가하지 않음 |
| CUA Driver의 background 입력 | UIA와 대상 HWND의 창 메시지, 조건부 좌표 입력으로 앱을 조작 | 별도 Driver 구현이며 이번에 가져오지 않음 |
| 독립 데스크톱 | 별도 OS 또는 가상 머신의 화면과 입력 환경에서 앱을 실행 | 이번 범위에 포함하지 않음 |

agent-desktop의 Windows overlay는 실제 커서와 분리된 표시다. headless 의미 조작의 이동을 보여 주며 headed 동작은 실제 커서를 사용한다. overlay를 켜는 것으로 물리 입력이 background 입력으로 바뀌지 않는다. [1.1.0 overlay 설명](https://github.com/vercel-labs/agent-desktop/blob/v1.1.0/skills/agent-desktop-windows/references/shell-and-overlay.md#cursor-overlay)

CUA Driver 0.28.2에는 별도 background 입력 구현이 있다. `delivery_mode=background`는 대상 창의 UIA 기능과 Win32 `PostMessage` 등을 사용한다. `foreground`는 실제 입력과 일시적인 전경 활성화를 허용한다. 원본 코드에서 대상 HWND의 가장 안쪽 컨트롤로 마우스 메시지를 보내는 경로와, 세션별 시각 커서를 그리는 경로가 분리되어 있다. background 지원 여부는 앱과 동작에 따라 다르고 전달 응답은 실제 적용의 증거가 아니다. [입력 모드 계약](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/platform-windows/src/input/delivery.rs), [창 메시지 입력](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/platform-windows/src/input/mouse.rs), [커서 표시 구현](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/platform-windows/src/overlay.rs)

기존 Computer Use MCP의 `vendor/guard.py`는 background/foreground 계약을 유지한다. Driver를 `--no-overlay`로 시작하는 것은 대상 인식과 녹화 시 표시 창이 가림 검사에 영향을 주는 문제를 피하기 위한 설정이며, background 입력 자체를 없애는 설정이 아니다. 이 기존 CUA 구성은 이번 변경 대상이 아니다.

CUA의 `virtualdesk.rs`라는 이름도 독립 데스크톱을 뜻하지 않는다. 이 파일은 여러 모니터를 합친 Windows 화면 좌표를 `SendInput` 좌표로 변환한다. [다중 모니터 좌표 구현](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/platform-windows/src/virtualdesk.rs)

## agent-desktop 1.3.0에서 선별한 내용

[agent-desktop v1.3.0](https://github.com/vercel-labs/agent-desktop/releases/tag/v1.3.0)과 [해당 버전의 기본 스킬](https://github.com/vercel-labs/agent-desktop/blob/v1.3.0/skills/agent-desktop/SKILL.md)을 기준으로 검토했다. headless 의미 조작과 headed 물리 입력의 구분은 이전 원본에도 있던 원칙이다. 이를 최신 버전에서 새로 생긴 Windows 독립 입력 기능으로 설명하지 않는다.

| 검토 항목 | 이번 반영 |
|---|---|
| 의미 조작과 물리 입력 구분 | 입력 계획과 `--background-only`로 명시하고, 제한 모드에서 UIA 자동 포커스를 해제한 설정값 확인 |
| 대상 재식별과 실행 후 상태 확인 | 기존 실행기의 규칙 유지 |
| 전달 불확실 시 중복 입력 금지 | 기존 중단과 결과 보고 규칙 유지 |
| 별도 커서 표시 | 입력 경로를 바꾸는 기능이 아니므로 미도입 |
| sandbox 제공자 | Windows 지원 제공자가 없어 미도입 |
| CUA의 창 메시지와 pen/touch 입력 | 별도 앱별 검증이 필요한 입력 구현이므로 미도입 |
| 외부 연결, MCP 서버, 가상 머신 | 현재 로컬 Python 실행 범위에 추가하지 않음 |

v1.3.0의 sandbox 제공자는 Apple Silicon Mac의 macOS VM이다. 원본 스킬은 Windows와 Linux에 제공자가 없다고 명시한다. 따라서 이 기능을 현재 Windows PC에 가져와 별도 화면에서 FX를 실행할 수 있다고 안내하지 않는다. 이번 작업은 새 외부 연결, MCP 구성, 가상 머신, 관리자 권한 또는 Windows 보안 설정 변경을 요구하지 않는다.

## 확인 범위

원본 문서와 고정 버전 소스에서 입력 경로를 확인한 기록이다. 이 조사만으로 운영 FX가 background 입력을 지원한다거나 모든 앱에서 사용자와 동시에 작업할 수 있다고 판정하지 않는다. 새 입력 정책의 시험 결과는 해당 변경의 검증 기록에서 확인하며, 기존 0.4.8 성능 수치와 릴리스 이력은 그대로 둔다.
