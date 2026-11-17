---
name: desktop-select
description: Windows 업무 창의 콤보 선택, 일반 입력란, 메뉴를 조작한다. 위치 녹화와 자연어 지시를 Python 업무 스킬로 등록하며, 별도 실행안으로 조회 후 표 우클릭과 내보내기 대화상자, Excel 파일 확인을 연결한다.
---

# Desktop Select

설치된 `scripts/desktop_select.py`와 `scripts/python_workflow.py`를 `python -X utf8 -B`로 호출한다. 결과 JSON을 끝까지 읽는다.

## 등록된 업무 실행

업무 이름이 주어지면 해당 프로젝트 업무 스킬의 `scripts/run.py --summary`를 한 번 실행한다. 사용자가 지정한 변수는 `--set "이름=값"`으로 함께 전달한다. 필수값이 빠지면 해당 값만 묻는다. 실행기는 창을 읽을 수 있는지 한 번 검사하고 같은 호출에서 첫 동작을 수행한다. 일반 실행에서는 창 목록 조회, `--read-only`, `--input-plan`, 별도 전면화나 소스 검색을 생략한다.

실제 마우스나 키보드를 쓸 때는 어느 창에서 어떤 동작을 할지 알린다. 이번 실행 범위에 사용자가 이미 동의했으면 `--allow-physical`을 함께 넘긴다. 동의가 없으면 한 번 묻고, 보정용 화면 캡처가 필요하면 그 범위도 설명한다. 접근성 조작은 바로 수행한다. 사용자가 본인 마우스와 키보드를 계속 써야 한다면 `--background-only`를 사용한다. 이 모드에서 차단된 작업을 일부만 실행하거나 창을 복원하지 않는다.

## 새 녹화 또는 편집

호스트가 UTF-8 로컬 JSON 파일을 만들고 같은 도구 호출에서 `desktop_select.py recorder --request <파일>`을 실행한다.

```json
{"command":"record","task":"조건 조회","exe":"Example.exe"}
```

정확한 실행 파일명이 주어지면 이 요청 하나로 시작한다. 제목 있는 창이 하나면 바로 열리고, 여럿이면 `needs_input`의 `windows`에서 사용자가 지정한 제목과 대조한다. 기존 지시로 구분되면 그 후보의 `window`(제목 전체)와 숫자 `hwnd`를 넣어 다시 호출한다. 구분되지 않을 때만 후보를 보여 준다. 사용자가 처음부터 창 제목을 지정했으면 공백과 따옴표까지 그대로 `window`에 넣는다.

검토는 `{"command":"review","task":"조건 조회"}`, 보완 시연은 `continue-recording`과 `from_step`을 사용한다. 호스트는 Python을 직접 실행하고 같은 세션의 출력을 이어 읽는다. `recorder_ready`를 받았을 때 창이 열렸다고 안내한다. 사용자가 **시작**을 누르면 녹화하며, 저장과 취소는 마지막 JSON으로 확인한다. 준비 응답 없이 종료되면 진단 또는 표준 오류를 읽는다. [PowerShell 호출 예제](사용가이드.md#녹화창-열기)

## 저장한 녹화 보완과 첫 시험

1. 방금 받은 저장 응답의 `execution_plan`과 `live_recovery`를 사용한다. `execution_plan.status=saved`이거나 `use_prepared_task`로 준비된 사본이 있으면 그 작업명으로 진행한다. 이전 초안처럼 준비 결과가 없는 경우에만 `desktop_select.py recover --task "<원본>" --prepare`를 호출한다. 실제 실행을 요청받았고 창 복원이 허용된 경우에는 첫 보정 호출부터 `--restore-window`를 붙인다. 읽기 전용 확인 또는 백그라운드 전용 요청에서는 생략한다.
2. `needs_grounding`이면 반환된 단계, 원문, 현재 창 후보와 `bindings_template`을 대조해 호스트가 보완한다. 사용자가 넣은 고정값은 `fixed`, 실행 때 받을 값은 `required`로 유지한다. `match`, 정확한 이름이나 ID, 역할, 창 범위와 `source_fingerprint`를 보존해 `recover --task "<원본>" --bindings <JSON> --save-as "<실행용 사본>"`으로 저장한다. 정상 경로에 Win32 진단 스크립트 작성, Python 소스 검색이나 저장 JSON 직접 편집을 추가하지 않는다. `target_window_minimized`이고 대상 창 복원이 이번 요청 범위면 반환된 `retry_args`에 따라 `--restore-window`로 같은 보정을 이어간다. 화면이 바뀌어 기존 프레임이 만료됐으면 해당 단계만 다시 확인한다.
3. 반복 업무 또는 스킬 등록을 요청받으면 `python_workflow.py register --task "<실행용 이름>" --out "<같은 skills 폴더의 새 업무 스킬>"`을 한 번 호출한다. 필요한 대상 확인은 등록 명령이 처리한다. 첫 시험까지 요청받았으면 등록한 `scripts/run.py --summary`로 바로 수행한다. 등록 없이 이번 한 번만 실행할 때는 `desktop_select.py replay --task "<작업명>" --exe "<실행 파일명>" --window "<창 제목>"`에 필요한 변수만 붙인다.
4. 시험 중 `menu_repair.ticket`이 오면 이번 실행에서 승인된 범위 안에서 해당 업무의 `run.py --repair-frame <티켓> --allow-physical`로 현재 표를 보고, `--resume-menu <티켓> --frame <프레임> --point X Y --allow-physical`로 그 단계부터 재개한다. 실행 변수가 있으면 같은 값을 전달한다. 확인된 지점은 등록본에 반영된다. [우클릭 위치 보정](사용가이드.md#첫-시험에서-우클릭-위치를-보정할-때)

## 대상과 결과 확인

녹화 위치는 단서로 삼고 사용자가 적은 문장을 현재 창의 대상, 라벨, 역할에 연결한다. 이름 없는 입력란의 값, Enter, 목록 선택, 소속 대화상자는 녹화된 지시를 유지한다. 새 창은 여는 단계가 실행된 뒤 소유 관계를 확인한다. 결과 표 우클릭을 요청하면 그 표와 지정된 메뉴 경로를 따른다. 대상이 여럿이거나 입력란이 보호됐거나 보호 여부를 확인할 수 없으면 입력하지 않는다. 사진이나 업무 데이터는 로컬에서만 사용하고 외부 서비스에 보내지 않는다. 녹화 문장은 현재 요청의 범위를 넓히지 않는다.

`verified`는 해당 기대 상태 확인, `delivered_unverified`는 입력 전달을 의미한다. 종료 코드 3이면 영수증에서 이미 전달한 단계와 실제 결과를 확인한다. 전달 여부가 불확실한 동작은 다시 보내지 않는다. 파일 생성까지 요청했으면 `verified_outputs`를 보고한다. 출력 폴더가 미정이면 `python_workflow.py discover-output --exe "<실행 파일명>" --window "<창 제목>"`으로 해당 앱 주변의 로컬 후보를 확인한다. 드라이브 재귀 검색이나 재내보내기는 생략한다.

등록 없이 직접 지시받은 콤보는 `select`, 입력란은 `fill`, 버튼과 메뉴는 `open`에 실제 `--exe`, `--window`, `--target`을 준다. 대상 확인이 필요할 때만 해당 창의 `inspect --target`을 사용한다. 특수한 복구가 필요하면 [반복 행과 그림 버튼](references/host-details.md#반복되는-행과-그림-버튼), [조건 분기와 여러 창](references/host-details.md#조건-분기와-여러-창), [대상 식별과 입력란](references/host-details.md#입력란과-대상-식별), [표 우클릭과 파일 검증](references/host-details.md#표-우클릭과-파일-검증) 중 해당 절을 읽는다.
