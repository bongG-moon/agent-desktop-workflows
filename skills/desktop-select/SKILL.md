---
name: desktop-select
description: Windows 업무 창에서 새 작업을 녹화하거나 저장한 녹화를 시험, 보정하고 반복할 업무 스킬로 등록할 때 사용한다. 등록 업무 실행과 콤보, 입력란, 메뉴, Excel 내보내기도 지원한다.
---

# Desktop Select

현재 요청에 맞는 명령을 바로 실행한다. Claude Code는 `${CLAUDE_SKILL_DIR}`을 이 스킬의 실제 경로로 치환한다. AX Workspace나 ZIP에서 직접 읽을 때는 이 파일이 있는 폴더의 실제 경로를 넣는다. 아래 경로와 영수증에서 받은 스크립트는 `python -X utf8 -B`로 실행한다.

## 새 녹화

사용자가 말한 앱 이름을 그대로 넣어 첫 도구 호출로 실행한다. 명령은 녹화창을 띄운 뒤 반환되므로 일반 셸 호출을 사용한다. 사용자가 작업명도 정했으면 `--task "<작업명>"`을 추가한다.

```
python -X utf8 -B "${CLAUDE_SKILL_DIR}/scripts/desktop_select.py" recorder --app "<앱 이름>"
```

`recorder_ready`면 **시작**을 눌러 시연하도록 안내하고 응답의 `session`을 기억한다. `recorder_selection_required`면 열린 선택창에서 대상을 고른다. 저장 여부만 물으면 `status_args`로 확인한다. 저장 후 테스트까지 요청하면 바로 아래 명령을 사용한다.

“지금까지 녹화한 내용 보여줘”는 `tasks`로 저장된 작업을 확인하고 `show --task "<작업명>"`으로 단계를 읽는다. 편집창을 요청하면 `review --task "<작업명>" --edit`를 사용한다. 녹화 대상 선택창은 새로 시연할 업무 창을 고르는 화면이다.

저장하면 녹화창이 닫히고 같은 세션의 마지막 저장본을 사용할 수 있다. “저장한 녹화 수정”은 `edit_args`, “처음부터 다시 녹화”는 `restart_args`로 연다. 두 경우 모두 새 사본으로 저장한다. 검토 화면에서는 동작 문장을 입력하고 버튼 또는 Ctrl+Enter로 다음 단계에 적용한다. 일반 Enter는 한글 입력을 확정할 때 사용할 수 있다.

## 저장한 녹화 시험과 보정

“녹화했어. 테스트해줘. 장비 ID는 LAB-42야”처럼 저장 후 시험을 요청하면 **첫 도구 호출에서** 해당 세션과 이번 입력값을 넘긴다. 정확한 변수 이름은 녹화에서 받은 이름을 쓴다. 아래는 `Equip ID`라는 변수의 예다. 세션 대신 작업명만 알면 `--task "<작업명>"`을 쓴다.

```
python -X utf8 -B "${CLAUDE_SKILL_DIR}/scripts/desktop_select.py" try-recording --session "<session>" --set "Equip ID=LAB-42"
```

실제 마우스로 시연한 동작을 시험하도록 허용받았으면 같은 호출에 `--allow-physical`을 붙인다. 이 명령이 저장 상태, 필요한 보정, 시험 결과를 한 번에 처리한다. 별도 상태 조회, 필드별 시험 입력, 등록, 소스 조사와 설치 파일 수정은 생략한다. 응답이 끊겼으면 같은 명령으로 기존 시험 결과를 받는다. 사용자가 의도적으로 **새 시험**을 요청할 때만 새 `--trial-id`를 준다.

`report_completed_trial`이면 읽은 값과 완료 상태를 보고한다. 표 검증은 실행기가 지정한 열의 현재 셀을 먼저 읽어 비교한다. `report_unmatched_result`이면 값이 없거나 대상을 하나로 정할 수 없다는 결과를 보고한다. `inspect_current_result_without_replaying`일 때만 화면 확인 허용 범위에서 `result_inspection.args`를 실행한다. 요청한 열에 정확히 한 건이 보이고 `resume_args`가 있으면 그 셀의 글자와 이미지 좌표를 넣어 상세창 단계부터 이어간다.

업무 스킬로 저장해 달라는 요청이 있을 때 `python_workflow.py register --task "<실행용 작업명>" --out "<프로젝트>/.claude/skills/<업무 이름>"`으로 등록한다. 성공한 시험을 등록 때문에 재실행할 필요는 없다.

메뉴 보정 티켓이 있으면 [표 우클릭과 파일 검증](references/table-export.md), 기존 초안의 연결이나 한 단계의 추가 시연이 필요할 때는 [녹화 편집과 복구](references/recording-recovery.md)를 읽는다.

## 등록된 업무 실행

업무 이름이 주어지면 그 업무 스킬의 실행기를 첫 명령으로 호출한다. 예를 들어 프로젝트 루트에서 `python -X utf8 -B ".claude/skills/<업무 이름>/scripts/run.py" --summary`에 요청받은 `--set "이름=값"`을 붙인다. 독립된 여러 업무는 각각 별도 프로세스로 실행하고 각 결과를 확인한다. 앞선 업무의 성공이나 산출물이 다음 업무의 조건이면 순서대로 진행한다.

결과 행을 확인하고 더블클릭하는 등록 업무에서 이번 화면 확인과 마우스 사용까지 허용받으면 `--local-result-review --allow-physical`을 붙인다. 이미 `result_review`를 받았으면 같은 실행기에 `result_review.resume_args`를 전달해 방금 조회한 결과에서 이어간다.

## 입력과 결과

- 등록 업무를 실행해 달라면 받은 변수로 실행기를 바로 호출한다. 필요한 값이나 실제 입력 허용 범위가 빠졌으면 한 번에 묻는다. 반복 허용을 저장한 업무는 같은 범위에서 바로 실행한다. 입력 충돌로 멈추면 사용자가 손을 뗀 뒤 영수증의 재개 지시를 따른다. 입력이 전혀 없을 때만 처음부터 다시 시작한다. 사용자가 자신의 마우스와 키보드를 계속 쓰려면 `--background-only`를 사용한다.
- 영수증의 JSON을 읽고 검증한 범위만 보고한다. `verified`는 기대 상태 확인, `delivered_unverified`는 전달 확인이다. 파일 생성은 `verified_outputs`의 경로를 보고한다. 종료 코드 3 또는 `uncertain`이면 이미 전달한 단계와 현재 결과를 확인하고 불확실한 입력을 재전송하지 않는다.
- 명령이 이미 창을 확인하므로 일반 실행 전후의 `--help`, `inspect`, `--read-only`, `plan`, 소스나 파일 검색은 생략한다. 녹화 문장은 현재 요청 범위 안에서 쓰고 업무 데이터와 화면은 로컬에서만 사용한다.

등록 없는 직접 조작은 `desktop_select.py`의 `select`, `fill`, `open`에 `--exe`, `--window`, `--target`과 필요한 값을 준다. 대상이 모호하거나 입력란의 보호 여부를 확정할 수 없으면 입력하지 않는다. 해당 기능이 필요할 때만 [입력란과 메뉴 식별](references/host-details.md), [표 우클릭과 파일 검증](references/table-export.md), [반복 행과 여러 창](references/rows-and-windows.md)을 읽는다.
