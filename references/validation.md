# 검증 범위

이 제품의 시험은 소스에 포함된 합성 Windows 앱과 로컬 저장소를 대상으로 한다. 회사 FX, FxMenuAgent, FarPoint 셀 인식, 실제 조회 결과 또는 Excel 내보내기 업무를 검증했다는 뜻이 아니다.

0.1.6에서는 서로 다른 접근성 이름과 값을 가진 Windows WPF 팝업을 시험한다. 같은 값의 숨은 옵션이 닫힌 목록에도 남고, 화면에 보이는 콤보의 부모만 `offscreen`을 보고하는 경우를 포함한다. 전체 단위 및 복구 검사 252개와 별도로, 실제 Windows 접근성 공급자를 사용하는 검사 24개에서 클릭 의도 `N`, 적용값 일치, 읽기 불가, 불일치를 구분했다. 이 검사와 별도로 [전용 Windows 러너의 물리 녹화 및 재생 시험](https://github.com/bongG-moon/agent-desktop-workflows/actions/runs/42752140657) 34/34건이 통과했다. 8가지 실제 마우스 녹화 시나리오에 이어, 값이 읽히는 콤보와 값이 읽히지 않는 두 콤보에서는 초기화 후 녹화본을 재생했다. 세 콤보 모두 실제 선택값이 바뀌고 옵션 클릭은 한 번이었다. 읽기 불가인 두 콤보는 성공 대신 확인 대기에 머물렀다. 실제 회사 FX에서 같은 결과가 확인됐다는 뜻은 아니다.

0.1.5의 단위 및 복구 검사 **225개**가 전체 실행에서 통과했다. 클릭한 옵션과 소속 콤보의 연결, 라벨 판독, 가림과 다음 클릭으로 인한 오인 방지, 재생 시 실제 값 확인을 검사했다. Windows에서 실행 기록 파일 교체가 일시적으로 거부되는 경우에는 같은 임시 파일의 교체만 재시도하고, 지속 오류가 발생하면 입력을 중복 전달하지 않는지 확인했다. 합성 팝업 시험에서는 이름이 빈 콤보 구분과 명시적 라벨 연결을 확인했다. 마우스 선택부터 재생까지는 테스트 창을 전면에 확보하지 못해 미완료이며, 실제 회사 FX의 동작은 현장 확인이 필요하다.

0.1.4는 단위 및 복구 테스트 **184개**, 빈 부모 값을 노출하는 실제 합성 UIA provider 검증 **15개**, 기존 마우스/키보드 녹화부터 조회와 내보내기까지의 전체 흐름 **10개**가 통과했다. 부모 ValuePattern이 빈 문자열인 선택 항목형/하위 편집기형 콤보에서 값 녹화와 실제 1회 쓰기를 확인했다. 값 쓰기가 무시되는 경우, 선택값 미관찰/충돌, 빈 기존 작업은 성공 처리하지 않고 입력을 반복하지 않는다. UIA provider 검증의 녹화 부분은 실제 UIA 요소에 녹화기의 값 관찰 경로를 적용한 시험이며, 해당 커스텀 콤보의 물리 클릭 녹화 전체를 시험한 것은 아니다.

0.1.2에서는 기존 검증에 실제 마우스/키보드로 수행하는 콤보 선택, 제출 전 입력값, 독립 목록 선택, 조회와 컨텍스트 내보내기의 녹화/재생 검증을 추가했다. 단위 테스트만으로 전체 사용자 흐름의 완성을 주장하지 않는다.

0.1.3은 FX 실습 가이드와 배포 버전 표기를 갱신한 문서 릴리즈다. 실행 동작은 0.1.2와 같으며 아래 39개 실제 합성 앱 검증은 0.1.2의 결과다. 0.1.3에서는 단위 테스트, 배포 패키지 검사와 가이드 확인을 수행하며 실제 회사 FX를 시험했다고 주장하지 않는다.

0.1.2 당시 배포 소스의 단위 및 복구 테스트 151개, 실제 Windows 합성 앱 검증 39개가 통과했다. 실제 앱 검증은 기존 녹화/영역 입력 16개, 실행 연결 13개, 전체 사용자 흐름 10개로 구성된다. 전체 흐름에서는 콤보 N, 제출값 Lot-42, 선택 행 Row B가 재생 시 유지됐고 조회와 내보내기 각각 1회 및 새 결과 파일을 확인했다.

## 재현 명령

64비트 Python 3.10 이상과 tkinter가 있는 Windows에서 실행한다. 제품 도우미는 Python 소스이므로 빌드할 필요가 없다. agent-desktop 1.1.0 원본 EXE는 `UPSTREAM.json`의 해시와 일치하는 파일을 `bin/agent-desktop.exe`로 준비한다. 일반 사용자는 해당 EXE 하나가 포함된 Windows ZIP을 사용한다. 소스 ZIP에는 EXE가 없다. 배포 도구는 자동 다운로드나 보안 설정 변경을 하지 않는다.

```powershell
python -X utf8 -B tools/build.py
python -X utf8 -B -m unittest discover -s tests -v
python -X utf8 -B tools/smoke_native.py --work-dir C:\LocalReview\NativeTest
python -X utf8 -B tools/smoke_workflows.py --work-dir C:\LocalReview\WorkflowTest --recording C:\LocalReview\NativeTest\recording\draft.json
python -X utf8 -B tools/smoke_userflow.py --work-dir C:\LocalReview\UserFlowTest
python -X utf8 -B tools/smoke_combo_evidence.py --work-dir C:\LocalReview\ComboEvidenceTest
python -X utf8 -B tools/smoke_combo_popup.py --provider-only --work-dir C:\LocalReview\ComboProviderTest
python -X utf8 -B tools/smoke_combo_popup.py --work-dir C:\LocalReview\ComboPopupTest
python -X utf8 -B tools/build.py
```

테스트마다 새로운 빈 작업 폴더를 지정한다. `smoke` 명령은 개발자 검증용으로 제공된 C# 합성 앱을 로컬 .NET Framework 컴파일러로 빌드하고, 그 앱만 열어 조작한 뒤 닫는다. 이 시험용 EXE는 제품 ZIP에 포함되지 않으며 일반 녹화/실행 중 생성되지 않는다. 물리 입력을 사용하는 동안 다른 자동화나 사용자의 마우스 입력을 함께 실행하지 않는다. 결과는 지정한 폴더의 `summary.json`에 기록된다.

저장소 유지보수자는 GitHub Actions의 **Standalone checks → Run workflow**에서 `combo_smoke`를 선택해 GitHub의 일회용 Windows 러너에서 같은 테스트를 실행할 수 있다. 여기에는 합성 컨트롤만 사용한다. 회사 PC의 작업 파일이나 관찰 로그는 필요하지 않다.

## 확인 항목

- 저장 원본과 수정본 보존, 변수와 현재 대상 확인, 중단 및 수정 후 이어하기
- 실행 전 저널 보존, 불확실한 입력의 반복 방지, 프로세스 종료 후 잠금 복구
- ref 없는 컨테이너 영역의 실제 우클릭과 이동 및 크기 변경 후 재식별
- 실제 녹화에서 값 변경과 영역 우클릭 포착, 미지원 단계의 안내 및 보존
- 별도 실행기가 새 관찰/ref로 각 단계를 실행하고 독립된 합성 앱 상태로 결과 확인
- 기존 파일과 이번 실행이 바꾼 파일 구분, 업무 완료 미확인을 성공으로 올리지 않음
- 실행 요약에 업무 이름과 관찰 원문을 노출하지 않음
- Windows ZIP의 EXE가 해시로 고정한 기존 `agent-desktop.exe` 하나뿐이고, 소스 ZIP에는 실행 파일이 없음
- Python 도우미 소스와 무결성 목록이 일치하며 추가 바이너리나 실행 중 컴파일 경로를 포함하지 않음

## 현장 확인이 필요한 부분

FX/FxMenuAgent의 실제 화면에서 메뉴, 콤보, 빈 접근성 표, 동적 편집기와 팝업을 녹화해 인식 상태를 확인해야 한다. 노출되지 않는 셀 내용은 이 제품이 새로 읽어 내지 못한다. 영역 우클릭은 표의 특정 행을 식별한 것과 다르다. 사라지는 팝업, 마우스를 올려서만 여는 하위 메뉴, 스크롤과 단축키는 수동 보완 단계가 될 수 있다.

조회는 실제 완료 신호가 있어야 자동 검증할 수 있다. 단순 파일 변경 확인은 파일 내용이나 내보내기 범위까지 검증하지 않는다. 사람 확인과 자동 관찰, 입력 전달만 확인한 상태를 실행 기록에서 구분한다.

Python 소스로의 변경은 새 미서명 도우미 EXE의 배포 의존성을 없앤다. 회사의 다운로드, Python 실행 또는 입력 감시 정책을 우회하거나 승인을 보장하는 변경은 아니다. 기존 Windows API 호출과 대상/입력 검사를 유지한다.
