---
name: desktop-select
description: Windows 업무 창의 작업을 녹화하고 저장한 녹화를 시험, 보정하거나 반복 업무 스킬로 등록할 때 사용한다. 등록 업무 실행과 입력란, 콤보, 메뉴, Excel 내보내기도 지원한다.
---

# Desktop Select

사용자가 요청한 명령을 첫 셸 호출로 실행한다. 실행기가 창과 입력 가능 여부를 확인하므로 일반 실행 전후에 도움말, 소스, 파일 목록이나 `inspect`를 따로 조회하지 않는다. Claude Code에서는 `${CLAUDE_SKILL_DIR}`, AX Workspace에서는 이 파일이 있는 폴더를 사용한다. 아래 명령과 응답의 스크립트는 `python -X utf8 -B`로 실행한다.

## 녹화 시작

```
python -X utf8 -B "${CLAUDE_SKILL_DIR}/scripts/desktop_select.py" recorder --app "<앱 이름 또는 창 제목의 일부>"
```

앱을 지정하지 않았으면 `recorder`로 창 선택 화면을 연다. 작업명은 `--task "<작업명>"`으로 받는다. `recorder_selection_required`면 창을 고르도록 안내한다. `recorder_ready`면 시작을 누르고 평소처럼 시연한 뒤 **마치고 검토**에서 저장하도록 안내한다.

## 저장 후 시험

```
python -X utf8 -B "${CLAUDE_SKILL_DIR}/scripts/desktop_select.py" try-recording --summary --session "<세션>" --set "변수명=이번 값"
```

방금 저장한 녹화는 같은 세션으로 바로 시험한다. 저장 응답을 받았다면 `first_trial_args`를 쓰고, 작업명만 있으면 `--task "<작업명>"`을 쓴다. 요청에 있는 변수는 `--set`, 허용받은 실제 입력은 `--allow-physical`로 함께 전달한다. PC에서 행 위치 지정까지 허용하면 `--local-result-review`, 메뉴 화면 확인을 허용하면 `--auto-repair-frame`을 붙인다. 백그라운드만 요청하면 `--background-only`를 쓴다.

- 필요한 값과 물리 입력 허용 여부는 한 번에 묻는다. `unknown_parameter`면 응답의 `required_parameters`와 요청을 대조해 대상이 명확한 값은 재사용한다. 저장 처리 중이면 `retry_args`로 이어간다. 중단된 시험은 같은 요청으로 결과를 확인하고, 사용자가 새 실행을 요청했을 때만 새 `--trial-id`를 지정한다.
- 보정은 이미 받은 단계와 현재 화면의 근거를 사용한다. 메뉴 이미지와 `resume_args`가 있으면 해당 이미지를 한 번 확인해 그 지점부터 이어간다. 이미지가 필요하고 아직 없을 때만 `frame_args`를 쓴다. `image_unchanged`이면 이미 본 이미지를 재사용한다.
- 준비가 막히면 `next_action`을 따른다. 상세 창은 여는 단계 뒤에 찾고, 행의 값 비교는 요청한 경우에만 한다. 문장 해석이 필요할 때만 [실행 의도 연결](references/recording-recovery.md#구조화된-의도-연결)을 읽는다.
- 결과 확인이 필요하면 응답의 `result_inspection`과 재개 인자를 따른다. 추가 판단이 필요한 경우에만 [결과 확인](references/recording-recovery.md), [표와 메뉴](references/table-export.md), [행과 창](references/rows-and-windows.md)을 읽는다.

## 등록과 반복 실행

등록 요청에는 성공한 시험의 `effective_task`로 `python_workflow.py register --task "<작업명>" --out "<프로젝트>/.claude/skills/<업무 이름>"`을 실행한다. 등록 업무 호출은 그 업무의 `scripts/run.py --summary`에 이번 `--set` 값을 붙여 바로 실행한다. 로컬 결과 확인과 물리 입력을 허용받았다면 `--local-result-review --allow-physical`을 사용한다. `result_review.resume_args`는 같은 실행기에 전달한다.

실행 결과는 영수증에서 읽는다. `verified`는 기대 상태를 확인했다는 뜻이다. `delivered_unverified`는 입력 전달까지만 확인한 상태이고, `uncertain`은 현재 효과를 확인해야 한다. 불확실한 입력이나 이미 한 조회를 다시 보내지 않는다. 파일 생성은 `verified_outputs`로 보고한다. `no_interactive_desktop`이면 `desktop_check.reason`에 따라 실행 세션과 잠금 상태를 확인한다.

저장 목록은 `tasks`, 단계 조회는 `show --task "<작업명>"`, 편집은 `review --task "<작업명>" --edit`를 쓴다. 등록 없는 직접 조작은 [입력란과 명령](references/host-details.md), 이미지가 필요한 중단은 [관찰 순서](references/host-details.md#관찰-순서), 캡처 보관 위치는 `storage`의 `capture_storage`를 사용한다. 업무 데이터와 화면은 허용된 범위에서만 사용한다.
