# Desktop Select 0.4.1 호환성 검증

0.4.0 현장 보고의 증상을 독립적인 Windows 시험 제공자에 재현했다. 사내 업무 프로그램의 코드나 데이터는 사용하지 않았다. 기존 CUA MCP 및 이전 Workflows의 고정 브리지는 수정하지 않았다.

| 보고된 증상 | 0.4.1 처리 | 검증 |
|---|---|---|
| IsOffscreen 미제공 | 표시된 대상 창, 같은 소속, 유효 사각형 및 창 내부 포함 여부를 확인 | 실제 raw UIA 제공자의 None 속성으로 직접 선택, 버튼 호출, 저장 작업 재생 |
| 같은 값을 노출하는 편집기 두 개 | 선택값을 읽는 공통 함수에서 일치하는 근거로 결합 | 부모 빈 값 + 동일 편집기, ValuePattern과 실제 MSAA 제공자 경로 모두 시험 |
| 옵션 좌표 0x0, Select만 제공 | 옵션은 실제 값의 후보 확인에만 사용하고, 콤보 SetValue 후 다시 읽기 | Expand 상태가 계속 0이어도 한 번 확장 후 SetValue; 옵션 Select는 호출하지 않음 |
| 최소화된 창의 사각형 0x0 | 조작 요청의 정확한 창을 복원하고 다시 확인 | 읽기 전용 inspect는 최소화 유지; select/open/replay는 복원 후 실행 |
| 녹화 문장의 대상 불일치 | 실제 라벨 연결 보완, 현재 행을 가리키는 표현, 확인 후 별칭 연결 | 같은 요소의 임시 식별값 변경 시 지시 유지; 다른 요소로 지시 이전은 차단 |
| 수정 시 모호한 skill_incomplete | 별도 manifest와 정확한 파일명 및 오류 구분 | 누락, 내용 불일치, 손상 manifest, 호환 모듈 독립 수정 검사 |

재현 시험은 다음과 같이 실행한다. 실제 마우스를 사용하는 시험은 다른 작업과 동시에 실행하지 않는다. work-dir에는 아직 존재하지 않는 절대 경로를 지정한다.

```powershell
python -X utf8 -B -m unittest discover -s tests
python -X utf8 -B tools/build_desktop_select.py --out .build/release-check
python -X utf8 -B tools/smoke_provider_compatibility.py --skill-dir <ZIP을푼폴더/desktop-select> --work-dir <새절대경로>
python -X utf8 -B tools/smoke_desktop_context.py --skill-dir <ZIP을푼폴더/desktop-select> --work-dir <다른새절대경로>
```

제공자 시험은 정상 경로뿐 아니라 값 충돌, 이름만 노출되는 옵션, Expand/Select/SetValue의 불확실한 호출 후 재시도 금지도 검증한다. 조회와 파일 내보내기 시험은 생성된 통합 문서를 다시 열어 조건값을 독립적으로 확인한다. 합성 시험 성공을 사내 FX 화면의 인수 시험 완료로 해석하지 않는다.

## 개발 시 변경 위치

- `provider_visibility.py`: 속성 미제공과 화면 밖을 구별하고 실제 창 내부 위치를 확인한다.
- `provider_values.py`: 조작과 녹화가 함께 사용하는 선택값 근거 결합.
- `provider_labels.py`: 실행 대상 ID를 바꾸지 않고 표시 라벨을 연결.
- `runtime-manifest.json`: 원본 `win_bridge.py`, `recorder.py`의 버전 일치 확인.

앞의 호환 파일을 수정할 때 기본 브리지 해시를 변경할 필요가 없다. 기본 브리지를 의도적으로 수정하는 개발 작업은 별도 검토 후 manifest도 같은 변경에 맞춰 갱신하고 회귀 시험 및 ZIP 빌드를 통과해야 한다. 값이 맞지 않으면 빌드도 실패하므로 서로 다른 버전 파일을 섞어 배포하지 않는다. 이 해시는 파일의 버전 일치를 확인하는 장치이며 코드 서명이나 보안 제품의 승인 여부를 뜻하지 않는다. 사용자는 파일을 부분 수정하지 않고 완성된 ZIP 전체를 교체한다.

Windows UIA의 속성 미제공과 기본값 구분에 대한 API 규약: [Microsoft GetCurrentPropertyValueEx](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-getcurrentpropertyvalueex).
