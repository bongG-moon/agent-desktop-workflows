# Desktop Select 0.5.0

Windows 화면에서 작업할 위치를 가리키고, 각 단계에 실행할 동작을 자유문장으로 적습니다. Claude Code가 설명과 현재 주변 요소를 해석해 콤보 설정, 버튼 실행, 표 우클릭 메뉴를 진행합니다.

[Windows ZIP 다운로드](https://github.com/bongG-moon/agent-desktop-workflows/releases/tag/desktop-select-v0.5.0) | [사용 가이드](skills/desktop-select/사용가이드.md)

## 이번 버전에서 달라진 점

- 녹화는 위치를 알려주는 용도입니다. 값이 읽히지 않는 콤보도 위치와 설명을 저장하며, 화면에 보였던 값을 자동 고정값으로 삼지 않습니다.
- 실제 표시 이름과 내부 ID가 달라도 문장을 저장합니다. 자유문장은 현재 사용하는 Claude Code 모델이 해석합니다.
- 적용 후 다음, 이전/다음, 단계 삭제, 대상 다시 지정, 저장된 작업 편집을 지원합니다. 수정 전 원본은 자동 보관합니다.
- Family처럼 실행할 때 받을 변수는 고정값으로 바뀌지 않습니다. 변수가 빠지면 입력 전에 필요한 값을 알려줍니다.
- doctor로 실제 설치 버전과 경로를 확인합니다. IsOffscreen 미제공, 동일한 중복 편집값, 가상 옵션의 SetValue, 최소화 복원 호환성은 0.4.1의 개선을 유지합니다.

## 시작하기

1. ZIP의 desktop-select 폴더 전체를 프로젝트의 `.claude/skills/`에 설치합니다. 64비트 Python 3.10 이상과 Tkinter가 필요합니다.
2. Claude Code에 “desktop-select의 doctor로 설치 버전과 경로를 확인해 줘”라고 요청합니다.
3. “FX 조회 내보내기 작업을 녹화해 줘”라고 요청하고 **대상 추가 (3초)**로 각 위치를 가리킵니다.
4. 각 단계에 “MES SETUP 조건을 N으로 설정해 줘”, “실행할 때 입력받은 변수를 FAMILY 조건에 넣어 줘”처럼 적습니다. Family를 변수 칸에도 지정할 수 있습니다.
5. **전체 저장** 후 “FX 조회 내보내기를 Family=Hybrid로 실행해 줘”라고 요청합니다. 수정할 때는 “그 작업을 편집해 줘”라고 요청합니다.

CUA MCP와 별도 시스템이며 CUA 드라이버나 추가 EXE를 설치하지 않습니다. 배포 ZIP은 Python 소스와 문서입니다. 별도 외부 모델 호출이나 정보 전송 기능은 없고, 자연어 해석은 현재 사용하는 Claude Code 모델이 담당합니다. 일반 Python 명령만으로 임의 자연어를 해석하는 내장 모델은 없습니다.

실행안은 현재 관찰한 요소를 대상으로 하는 구조화된 조작입니다. 조회 버튼을 전달한 것과 조회 완료, Excel 파일 생성을 구분합니다. 결과가 불확실하면 같은 입력을 자동 반복하지 않습니다. 행 버튼과 표 우클릭은 실제 포인터를 사용합니다.

## 개발 검증

```powershell
python -X utf8 -B -m unittest discover -s tests -v
python -X utf8 -B tools/build_desktop_select.py
```

Windows 시험 도구 `tools/smoke_intent_workflow.py`는 별도 시험 창에서 위치 녹화, 자유문장 편집, 단계 삭제, 변수 유지와 실제 콤보/버튼/우클릭 Excel 실행을 검증합니다. 모델 응답은 시험용 구조화된 실행안입니다. 회사 FX 화면이나 사내 모델의 해석 정확도를 검증했다는 의미는 아닙니다. 시험용 실행파일은 배포 ZIP에 포함하지 않습니다.

기존 상세 행/결과 메뉴 가르치기와 이전 작업 재생도 유지합니다. 이전 Workflows 자료는 [0.1.6 소스](https://github.com/bongG-moon/agent-desktop-workflows/tree/v0.1.6)에 있습니다.
