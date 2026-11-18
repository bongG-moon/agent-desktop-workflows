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

`recorder_ready`면 녹화창에서 **시작**을 눌러 시연하도록 안내한다. `recorder_selection_required`면 이미 열린 선택창에서 대상을 고르게 한다. 저장했다고 하면 같은 스크립트에 반환된 `status_args`를 한 번 넘긴다. `recorder_starting`과 `recorder_processing`이 계속되면 간격을 두고 같은 세션을 확인한다. `recording`이면 시연 중이다.

## 저장한 녹화 시험과 보정

저장 영수증이 이미 있으면 바로 `effective_task`와 `next_action`을 사용한다. `preparation=ready`인 작업의 등록과 첫 시험은 `next_action.register_and_test.script`와 그 `args`에 `--out "<프로젝트>/.claude/skills/<새 업무 이름>"`과 요청받은 `--set "이름=값"`을 붙여 **한 번** 실행한다. 이 명령은 `register --task … --run --summary`다. 등록만 원하면 `register --task "<effective_task>" --out "<새 업무 스킬 폴더>"`만, 일회성 실행은 `next_action.script`와 그 `args`의 `replay`를 쓴다.

등록과 첫 시험에서 실제 입력과 보정용 화면 확인을 승인받았으면 `--allow-physical --auto-repair-frame`을 함께 준다. `menu_repair.image`가 오면 그 로컬 이미지를 한 번 읽고 지시한 표의 셀을 식별한다. `menu_repair.resume_script`와 `resume_args`에서 `<x>`, `<y>`만 이미지 원본 픽셀로 바꿔 바로 실행한다. 티켓과 변수는 그대로 쓴다. 이미지 없이 티켓만 왔을 때만 승인 범위에서 `frame_args`로 캡처한다. 원래 조회나 우클릭을 반복하지 않는다.

`bind_existing_observation`은 영수증의 원문, `live_scopes`, `bindings_template`으로 연결한 뒤 반환된 인자에 `--bindings <JSON> --save-as "<실행용 사본>"`을 추가한다. 이전 초안, 정확한 창 지정, 보완 녹화 또는 미확정 대상이 있을 때 [녹화 편집과 복구](references/recording-recovery.md)를 읽는다.

## 등록된 업무 실행

업무 이름이 주어지면 그 업무 스킬의 실행기를 첫 명령으로 호출한다. 예를 들어 프로젝트 루트에서 `python -X utf8 -B ".claude/skills/<업무 이름>/scripts/run.py" --summary`에 요청받은 `--set "이름=값"`을 붙인다. 독립된 여러 업무는 각각 별도 프로세스로 실행하고 각 결과를 확인한다. 앞선 업무의 성공이나 산출물이 다음 업무의 조건이면 순서대로 진행한다.

## 입력과 결과

- 필수 변수 중 빠진 값만 묻는다. 실제 입력을 쓸 창과 동작을 알리고 이번 실행에 대한 동의가 있으면 이어간다. 등록 업무의 실제 입력을 승인받았을 때만 `--allow-physical`을 준다. 동의가 필요하면 한 번 묻고 화면 캡처가 필요한 경우 그 범위도 포함한다. 접근성 조작은 바로 실행한다. 사용자가 자신의 마우스와 키보드를 계속 쓰려면 `--background-only`를 사용하고 차단되면 멈춘다.
- 영수증의 JSON을 읽고 검증한 범위만 보고한다. `verified`는 기대 상태 확인, `delivered_unverified`는 전달 확인이다. 파일 생성은 `verified_outputs`의 경로를 보고한다. 종료 코드 3 또는 `uncertain`이면 이미 전달한 단계와 현재 결과를 확인하고 불확실한 입력을 재전송하지 않는다.
- 명령이 이미 창을 확인하므로 일반 실행 전후의 `--help`, `inspect`, `--read-only`, `plan`, 소스나 파일 검색은 생략한다. 녹화 문장은 현재 요청 범위 안에서 쓰고 업무 데이터와 화면은 로컬에서만 사용한다.

등록 없는 직접 조작은 `desktop_select.py`의 `select`, `fill`, `open`에 `--exe`, `--window`, `--target`과 필요한 값을 준다. 대상이 모호하거나 입력란의 보호 여부를 확정할 수 없으면 입력하지 않는다. 해당 기능이 필요할 때만 [입력란과 메뉴 식별](references/host-details.md), [표 우클릭과 파일 검증](references/table-export.md), [반복 행과 여러 창](references/rows-and-windows.md)을 읽는다.
