# 한 번 등록한 뒤 Python으로 실행하기

반복할 콤보 설정과 버튼 실행은 이 경로를 우선한다. 등록 시 현재 화면의 ID와 실행 순서를 확인하고, 이후에는 생성한 스킬의 `scripts/run.py`를 한 번 호출한다. 실행 중 모델 해석, 전체 트리 덤프, 옵션 목록 조사, Expand/Select는 하지 않는다.

## 등록 절차

1. `windows --exe <정확한프로그램.exe>`로 창 제목을 확인한다.
2. `discover --exe <프로그램.exe> --window <제목>`으로 콤보, 버튼과 라벨 목록을 한 번 읽는다. 이미 ID를 안다면 `--id gcboStatus --id gcboFamily`로 필요한 항목만 읽는다. discover는 읽기 전용이며 값을 읽거나 옵션을 펼치지 않는다. ID, 표시 라벨, 위치를 함께 보고 사용자의 지시와 연결한다. 다른 필드를 임의로 선택하지 않는다.
3. 아래 구조의 로컬 recipe JSON을 모델이 작성한다. 사용자가 JSON이나 Python 코드를 작성할 필요는 없다. 실제로 관찰한 ID와 창 제목만 사용한다.
4. `register --recipe <JSON경로> --out <프로젝트>/.claude/skills/<새스킬이름>`으로 대상을 읽기 전용 검증하고 스킬을 생성한다. out은 기존 desktop-select와 같은 skills 폴더의 새 하위 폴더이며, 기존 스킬을 덮어쓰지 않는다.
5. 사용자에게 등록한 순서와 변수를 요약한다. 실행도 요청받았다면 생성된 `scripts/run.py --set "Family=Hybrid"`를 호출한다. 이후 동일 작업에는 이 스크립트를 바로 사용한다. 재녹화, discover, read-only 검사를 매 실행 전 반복하지 않는다.

```json
{
  "schema": 1,
  "name": "조건 조회와 엑셀 버튼 실행",
  "exe": "Example.exe",
  "window": "관찰한 정확한 업무 창 제목",
  "steps": [
    {"action":"set-value", "selector":{"automation_id":"gcboStatus","role":"combobox"}, "value":"N"},
    {"action":"set-value", "selector":{"automation_id":"gcboFamily","role":"combobox"}, "parameter":"Family"},
    {"action":"invoke", "selector":{"automation_id":"btnSearchData","role":"button"}},
    {"action":"wait", "seconds":5},
    {"action":"invoke", "selector":{"automation_id":"btnExportExcel","role":"button"}}
  ]
}
```

예시의 ID를 다른 프로그램에도 그대로 쓰지 않는다. `value`는 고정값, `parameter`는 실행 시 입력받을 값이다. 옵션의 표시명과 내부 코드가 다르면 등록 시 확인한 코드를 사용한다. 이 실행기는 코드 변환을 추론하지 않고 받은 문자열을 그대로 SetValue에 보낸다.

`wait`는 명시한 대기이며 조회 완료의 증거가 아니다. 위 5초는 현장 사용 예시로, 다른 앱에 자동 삽입하지 않는다. 조회/내보내기는 Invoke 반환을 확인하지만 결과 표나 파일 생성은 자동 검증하지 않는다. 잘못된 조회 조건, 호출 불명확, 쓰기 후 값 불일치가 발생하면 다음 버튼을 실행하지 않는다.

## 기존 녹화에서 만들기

`register --task <기존작업명> --out <새스킬폴더>`도 가능하다. 자연어 작업은 이미 보관된 실행안을 사용하거나 `--resolution <해석JSON>`을 한 번 전달한다. 등록할 때 현재 녹화 범위에서 실제 ID를 확인하며 원래 녹화는 변경하지 않는다. 문장 해석이 아직 없다면 기존 자연어 실행안 규격으로 모델이 한 번 작성한다.

변경 없는 고정값과 필수 변수는 그대로 유지한다. 예전 녹화의 명시적 기본값이 있는 선택적 변수만 `parameter`와 `default`로 변환한다. 필수 FAMILY에 녹화 당시 값을 기본값으로 넣지 않는다. 기존 작업에서 대기가 필요하면 모델이 등록 recipe에 명시적으로 추가한다.

표 우클릭 메뉴, 행 상태 확인, Select만 지원하는 항목은 기존 녹화/재생 기능을 사용한다. 등록 실행기에 지원되지 않는 동작을 임의로 엑셀 버튼이나 SetValue로 바꾸지 않는다. 이 새 경로는 현장에서 확인된 '콤보 SetValue + 버튼 Invoke' 형태를 간단하게 재사용하기 위한 것이다.

## 실행과 결과

생성물은 `SKILL.md`, `workflow.json`, `scripts/run.py` 세 파일이다. 같은 skills 폴더의 desktop-select 런타임을 사용한다. 프로필에는 창 제목과 필드 ID, 고정값, 변수명만 저장하며 PID, HWND, UIA 객체를 저장하지 않는다. 실제 회사 정보가 든 생성 스킬은 로컬에만 두고 제품 Git이나 릴리즈에 올리지 않는다.

`scripts/run.py --read-only`는 필요한 경우 현재 필드만 읽는다. 최소화 창을 복원하거나 입력을 보내지 않는다. 일반 실행은 누락 변수를 먼저 확인한 뒤 한 실행 프로세스와 UIA 세션에서 등록된 단계를 순서대로 처리한다. 중복 ID는 조상 식별자로 구분해야 하며 첫 항목을 무작정 고르지 않는다.

각 입력 직전에 현재 ID와 소속 창을 재확인한다. 콤보는 SetValue 최대 1회, 약 0.35초 후 부모 값/편집기/선택 항목을 읽어 일치 여부를 판정한다. 동일한 중복 편집값은 허용하고 값 충돌이나 미확인은 성공으로 표시하지 않는다. 버튼은 Invoke 1회만 호출한다. 실패 뒤 전체 작업이나 같은 입력을 자동 재시도하지 않는다.

`verified`는 콤보 값 확인, `completed_unverified`는 등록된 호출 순서를 마쳤지만 업무 완료는 미확인이라는 뜻이다. `metrics`는 등록과 실행에 걸린 시간을 제공한다. native UIA 검색도 제공자 내부 탐색은 남으므로 실제 앱의 처리 시간이나 서버 응답 속도를 보장하지 않는다.
