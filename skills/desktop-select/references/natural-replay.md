# 자유문장을 실행안으로 연결하기

이 문서는 Claude Code 모델용이다. 사용자는 위치를 가리키고 문장만 작성한다. 모델이 저장된 설명과 현재 화면을 읽어 아래 JSON 실행안을 만든다. 별도 외부 모델이나 셸 코드를 호출하지 않는다.

## 절차

1. `show --task <이름>`의 `task.steps`와 `plan_digest`를 읽는다. 설명 원문, 위치, 변수와 순서를 유지한다.
2. 각 단계에 `inspect --task <이름> --near <번호> --exe <프로그램.exe> --window <정확한 제목>`을 실행한다. 현재 나타난 주변 요소와 라벨을 보고 요청에 맞는 대상을 고른다. 저장된 위치는 탐색 범위이며 가리켰던 요소에만 동작을 제한하지 않는다. 실제 동작 대상은 현재 관찰에서 찾아야 한다.
3. 아래 규격으로 로컬 JSON을 작성한다. 예시의 ID를 그대로 쓰지 말고 방금 관찰한 ID나 이름을 넣는다. 변수는 `value`로 바꾸지 말고 `parameter`로 연결한다. 현재 요청에 값이 없다면 필요한 변수값만 묻는다.
4. `replay --task <이름> --exe <프로그램.exe> --window <정확한 제목> --resolution <로컬JSON경로> --set "Family=Hybrid"`로 실행한다. 모델이 해석하고 이어 실행하는 것이 정상 흐름이다. `needs_interpretation`을 작업 불가능으로 보고하지 않는다.
5. 결과가 불확실하면 현재 상태를 확인한다. 동일 입력을 자동 반복하지 않는다. 작업을 수정했다면 새 digest와 현재 화면으로 실행안을 다시 만든다.

빈 설명이나 위치가 없는 초안은 동작을 임의로 만들지 않는다. 사용자의 현재 요청으로 의미를 확정할 수 없다면 `edit`로 설명 또는 위치를 보완한다. 문장의 표현이 로컬 정규식에 맞지 않는다는 이유로 거부하지 않는다. 요청이 현재 지원하는 동작 밖이면 그 동작을 구체적으로 설명하고 멈춘다.

## 기본 실행안

```json
{
  "schema": 1,
  "plan_digest": "show가 반환한 전체 작업의 해시",
  "steps": [
    {"number": 1, "actions": [
      {"action": "select", "role": "combobox", "match": "automation_id", "target": "conditionState", "value": "N"}
    ]},
    {"number": 2, "actions": [
      {"action": "select", "role": "combobox", "match": "automation_id", "target": "conditionFamily", "parameter": "Family"}
    ]},
    {"number": 3, "actions": [
      {"action": "open", "role": "button", "match": "automation_id", "target": "searchCommand", "completion": "delivery"}
    ]}
  ]
}
```

`number`는 저장된 전체 작업의 단계 번호다. 모든 `action: intent` 단계가 정확히 한 번씩 들어가야 한다. 기존 상세 녹화 단계는 실행안에 넣지 않는다. 한 단계는 1~32개 동작, 전체는 300개 이하다. 설명에 입력 변수로 지정한 이름은 실제 동작의 `parameter`로 사용해야 하며, 녹화 당시 값이나 모델이 추정한 고정값으로 대체할 수 없다. 자유문장에서 추가 변수를 해석했다면 실행안에 새 `parameter`로 지정할 수도 있다. 변수값은 `--set`으로 전달한다.

- `select`: 현재 콤보에 값을 선택한다. `value` 또는 `parameter` 하나만 지정한다. 완료 시 실제 읽기값이 일치해야 한다.
- `open`: 관찰한 `role`, `target`을 실행한다. `match`는 `automation_id` 또는 `name`이며 기본은 전자다. 펼침 동작은 `expand: true`를 추가한다. 역할별 지원은 실제 관찰과 실행 결과를 따른다.
- `result-menu`: 아래와 같이 가리킨 표 안에서 우클릭 메뉴 경로를 실행한다.

위치 근처에 후보가 여러 개면 라벨과 구조를 확인한다. 사용자가 가리킨 영역 밖의 다른 창이나 프로그램으로 대상을 넓히지 않는다. 임의 좌표, Python, PowerShell, shell 명령을 실행안에 넣을 수 없다.

## 표 우클릭과 Excel 메뉴

아래 `menu.target`에는 현재 관찰한 표나 그룹의 식별자를 넣는다. `menu.point`는 해당 단계의 `anchor.point`에 저장된 상대 위치를 쓴다. 기록된 위치가 없으면 임의로 중심점을 만들지 말고 다시 지정한다. 실제 `context` 규격은 기존 `result-menu` 상세 단계와 같다.

```json
{
  "action": "result-menu",
  "label": "결과 표 내보내기",
  "completion": "delivery",
  "context": {
    "trigger": null,
    "ready": null,
    "menu": {
      "target": {"role": "group", "automation_id": "resultGrid"},
      "point": [0.55, 0.32],
      "path": ["EXPORT", "EXPORT TO EXCEL"]
    },
    "output": null
  }
}
```

이 예시의 좌표는 설명용이다. 앞선 조회가 완료됐다는 표식이 관찰된다면 `ready`에 `{"kind":"text","target":{"role":"statictext","automation_id":"resultStatus"},"expected":"완료"}`처럼 현재 관찰한 표식을 지정한다. 조회도 한 상세 동작에 넣으려면 `trigger`에 버튼 식별자를 넣고 `ready`를 반드시 함께 지정한다. 이미 조회한 단계가 있으면 조회 버튼을 중복 호출하지 않는다.

메뉴 항목은 새로 열린 대상 창 소속 메뉴에서 다시 찾는다. 경로 이름은 실제 메뉴 표기에 맞춘다. 파일까지 검증하려면 기존 상세 가르치기 화면에서 출력 조건을 지정하고 그 `output`을 사용한다. 앱이 실제로 저장할 로컬 .xlsx/.xlsm 경로 또는 폴더만 사용할 수 있다. 파일이 아닌 Excel 창만 열리는 설정에서는 파일 생성 완료를 주장하지 않는다.

## 결과를 해석하는 방법

`completion`의 기본은 `verified`다. `delivery`는 조회/내보내기처럼 사용자가 요청한 버튼이나 메뉴 호출을 전달한 뒤 다음 지시를 이어갈 때 사용한다. 콤보나 항목 선택에는 사용할 수 없다.

`Invoke`가 정상 반환했거나 메뉴 마지막 항목까지 전달됐다는 근거가 있어야 이어간다. 시간초과, 불명확한 호출, 값 불일치, 지정한 파일 확인 실패를 `delivery`로 덮을 수 없다. 이 방식으로 이어간 작업의 최종 상태는 `completed_unverified`이며 업무 결과 전체가 검증됐다는 의미는 아니다.

`verified`는 실제 값 또는 지정한 결과의 확인, `needs_input`은 변수 누락, `needs_target`은 위치 미지정이다. `blocked`와 `uncertain`의 진단 코드를 그대로 보고하고 현재 상태를 먼저 확인한다.
