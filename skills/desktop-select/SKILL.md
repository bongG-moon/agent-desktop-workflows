---
name: desktop-select
description: Windows 업무 창의 작업을 녹화하고 저장한 녹화를 시험, 보정하거나 반복 업무 스킬로 등록할 때 사용한다. 등록 업무 실행과 입력란, 콤보, 메뉴, Excel 내보내기도 지원한다.
---

# Desktop Select

현재 요청의 명령을 바로 실행한다. Claude Code의 `${CLAUDE_SKILL_DIR}`은 이 스킬 경로이며 AX Workspace나 ZIP에서는 이 파일이 있는 실제 폴더를 쓴다. 영수증의 스크립트도 `python -X utf8 -B`로 실행한다.

## 새 녹화

첫 셸 호출에 사용자가 말한 앱 이름을 넣는다. 앱을 정하지 않았으면 `--app`을 생략한다. 작업명도 정했으면 `--task "<작업명>"`을 붙인다.

```
python -X utf8 -B "${CLAUDE_SKILL_DIR}/scripts/desktop_select.py" recorder --app "<앱 이름>"
```

명령은 녹화창을 띄운 뒤 반환된다. 일반 셸 호출을 쓰고 `recorder_ready`를 받은 뒤 열렸다고 알린다. 후보가 여럿이면 열린 선택창에서 고른다. 저장 여부만 물으면 `status_args`, 저장한 녹화 수정은 `edit_args`, 처음부터 다시 녹화는 `restart_args`를 쓴다. 수정과 재녹화는 사본으로 저장된다.

## 저장 후 시험과 보정

“녹화했어. 테스트해줘”의 첫 셸 호출은 아래 명령이다. `--set`에는 이번 입력값과 녹화의 변수 이름을 넣는다. 작업명만 알면 세션 대신 `--task "<작업명>"`을 쓴다.

```
python -X utf8 -B "${CLAUDE_SKILL_DIR}/scripts/desktop_select.py" try-recording --session "<session>" --set "Equip ID=LAB-42"
```

- 시연한 물리 입력의 시험을 허용받았으면 같은 호출에 `--allow-physical`을 붙인다. 저장 상태, 보정, 시험 결과를 한 번에 처리하므로 별도 상태 조회, 필드별 시험 입력, 등록, 소스 조사를 먼저 하지 않는다.
- 보정용 화면 확인까지 허용받았으면 `--auto-repair-frame`을 붙일 수 있다. 메뉴 위치가 막혔을 때만 이미지를 반환한다. `menu_repair`가 있으면 그 응답의 `frame_args`와 `resume_args`를 따라 조회 없이 이어가며, 성공한 `effective_task`로 등록한다.
- 응답이 끊기면 같은 명령으로 기존 결과를 받는다. 의도적인 **새 시험** 요청일 때만 새 `--trial-id`를 준다. 이미 전달한 입력을 재전송하지 않는다.
- `report_completed_trial`이면 읽은 값과 완료 범위를 보고한다. `report_unmatched_result`이면 불일치나 식별 불가를 보고한다. `inspect_current_result_without_replaying`이면 [결과 확인과 재개](references/recording-recovery.md)를 읽고 반환된 `result_inspection.args`와 `resume_args`로 현재 결과에서 이어간다.
- 문장이 미인식이어도 사용자에게 다시 쓰라고 하지 않는다. 원문과 녹화 단서로 의도를 한 번 해석하여 [구조화된 의도 연결](references/recording-recovery.md#구조화된-의도-연결)로 저장한다. 후속 창은 `window_context.after_step`의 앞 단계가 연 뒤 찾는다. 열리기 전에 메인 창에서 계속 검색하거나 설치 코드를 수정하지 않는다.
- 보정 인자나 의미가 부족할 때만 [표와 메뉴](references/table-export.md) 또는 [행과 창](references/rows-and-windows.md)을 읽는다. 완전한 재개 인자가 있으면 해당 응답을 사용한다.

## 업무 스킬로 등록하고 실행

업무 스킬로 저장해 달라면 성공한 시험의 실행용 작업명으로 `python_workflow.py register --task "<작업명>" --out "<프로젝트>/.claude/skills/<업무 이름>"`을 호출한다. 등록 응답의 경로와 상태를 사용한다. 성공한 시험이나 파일 목록 조회를 반복하지 않는다.

등록 업무를 실행해 달라면 그 업무의 `scripts/run.py --summary`에 요청받은 `--set "이름=값"`을 붙여 **첫 셸 호출로 실행**한다. 필요한 입력값이나 입력 허용 범위가 빠졌으면 한 번에 묻는다. 영수증에 `result_review.resume_args`가 있으면 같은 실행기에 전달해 재조회 없이 이어간다. 현재 화면의 로컬 확인과 물리 입력을 허용받았으면 `--local-result-review --allow-physical`을 사용한다.

## 공통 원칙

- 명령이 창과 대상을 확인한다. 일반 실행 전후의 `--help`, `inspect`, `--read-only`, `plan`, 소스 및 파일 검색을 반복하지 않는다. 진단에 필요한 내용은 영수증과 해당 참조부터 확인한다.
- 요소와 이번 실행의 영수증을 먼저 쓴다. **값 비교를 요청했을 때만** 이번 입력값과 현재 셀을 비교한다. “결과가 있으면 열기”에는 비교를 추가하지 않으며 “첫 행”을 명시했을 때만 첫 행을 선택한다. 녹화 당시 이미지의 값을 기준으로 고정하지 않는다.
- 이미지가 필요한 중단 응답에서만 [관찰 순서](references/host-details.md#관찰-순서)를 따른다. 로컬 캡처를 무조건 모델에 보내지 않는다. `image_unchanged`이면 이미 본 동일 이미지를 다시 읽지 않는다.
- `verified`는 기대 상태 확인, `delivered_unverified`와 종료 코드 3은 전달 확인이다. `uncertain`이면 현재 효과를 확인하고 불확실한 입력을 반복하지 않는다. 파일 생성은 `verified_outputs`를 사용한다.
- 접근성 동작은 자동 포커스를 억제하고 실제 마우스나 키보드가 필요한 단계에서만 전면화한다. `--background-only`는 물리 입력을 금지한다. 별도 데스크톱이나 잠금 해제 기능이 아니다. `no_interactive_desktop`이면 잠금 해제 뒤 영수증 지시에 따라 이어간다. 캡처 보관 위치가 필요할 때만 `storage`의 `capture_storage`를 확인한다.
- 독립 업무는 별도 프로세스로 실행할 수 있다. 앞 업무 결과에 의존하면 순서대로 실행한다. 업무 데이터와 화면은 허용한 범위 안에서만 사용한다.

저장 목록은 `tasks`, 단계 조회는 `show --task "<작업명>"`, 편집창은 `review --task "<작업명>" --edit`이다. 등록 없는 직접 조작에만 [입력란과 명령](references/host-details.md)을 읽는다.
