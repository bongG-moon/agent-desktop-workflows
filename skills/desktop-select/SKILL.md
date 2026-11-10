---
name: desktop-select
description: Windows에서 현재 열린 업무 창의 필드, 콤보 옵션과 메뉴 명령을 이름으로 찾아 선택한다. FxMenuAgent처럼 같은 프로세스에 업무 창이 여러 개 있거나 라벨과 실제 컨트롤 이름이 다른 화면에 사용한다.
---

# Desktop Select

사용자에게 필요한 것은 **화면 이름, 필드 이름, 원하는 값이나 명령**이다. 녹화와 ref는 필요 없다. 예: “Device Setup Standard에서 MES Setup을 N, Family를 Hybrid로 선택해 줘.” 이 스킬 디렉터리의 `scripts/desktop_select.py`를 회사 PC에서 로컬 Python으로 실행한다. 개인 Claude 설정 변경, MCP 등록, 드라이버 설치 또는 외부 모델 호출은 필요 없다.

1. `windows --exe <프로그램.exe>`로 업무 창을 찾는다. MenuAgent의 실행 파일 이름은 로컬 PowerShell에서 `Get-Process -Name '*MenuAgent*' | Select-Object -ExpandProperty ProcessName -Unique`로 확인하고 `.exe`를 붙인다. 사용자가 지목한 업무 창의 현재 제목 전체를 `--window`에 준다. 같은 제목이 둘이면 어느 창인지 확인하고 해당 `--hwnd`를 덧붙인다.
2. 알아볼 때는 `inspect`, 특정 필드만 보면 `inspect --target <이름>`을 쓴다. 실행 요청을 받으면 `select --target <필드> --value <값>` 또는 `open --target <명령>`으로 바로 조작한다. 각 명령에 `--exe`와 `--window`가 필요하다. 매번 새로 찾으므로 이전 출력의 순번이나 좌표는 쓰지 않는다.
3. 같은 이름이 둘이면 입력하지 않는다. `inspect`로 영역을 보고, 예를 들어 업무 폼의 조회는 `--within content`, 바깥 도구 모음의 조회는 `--within commands`로 구별한다. 요청으로 어느 쪽인지 알 수 없으면 두 후보를 보여 준다. 단지 ‘조회’였다는 이유로 임의 선택하지 않는다.

업무 창 안에서 라벨, 실제 자동화 ID, 역할, 현재 컨테이너를 비교한다. REA에서 확인된 창/폼/도구 모음 구조는 **현재 접근성 트리에도 보일 때만** 사용한다. 콤보의 `options`는 현재 노출된 후보의 값과 이름이고, `value_evidence`는 현재 선택값의 근거다. 둘을 구분한다. 닫힌 목록이 비어 있어도 특정 필드 ID, 옵션 목록, 화면 좌표를 가정하지 않는다. 현재 트리에 없는 그리드 셀이나 메뉴는 선택하지 않는다.

메뉴는 현재 보이는 부모를 `open --target <부모> --role menuitem --expand`로 펼친 뒤 자식을 현재 트리에서 다시 찾는다. 부모가 같은 트리에 있으면 `--within <부모 이름>`을 준다. 업무 창에 속한 별도 팝업은 `inspect` 결과의 `owned_popups`에서 확인하고 `--popup <hwnd>`로 범위를 지정한다. 소속이 확인되지 않은 팝업 또는 출처를 구분할 수 없는 동명 메뉴는 실행하지 않는다. 내보내기 버튼과 우클릭 메뉴는 서로 대체하지 않는다.

`verified`는 현재 컨트롤에서 기대값이나 펼침/선택 상태를 다시 읽었다는 뜻이다. `delivered_unverified`는 전달됐지만 결과를 확인할 수 없다는 뜻이다. 조회 결과 생성이나 업무 저장 완료와 구분한다. 확인되지 않은 입력을 반복하거나 물리 클릭으로 전환하지 않는다. 여러 필드를 연달아 요청받으면 각 필드가 확인된 다음에만 다음 조작을 한다.

설치, 회사 PC에서 처음 해 볼 요청, 결과별 대응은 [시작 안내](사용가이드.md)를 참고한다.
