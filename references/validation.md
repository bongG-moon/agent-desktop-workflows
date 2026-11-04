# 검증 범위

이 제품의 시험은 소스에 포함된 합성 Windows 앱과 로컬 저장소를 대상으로 한다. 회사 FX, FxMenuAgent, FarPoint 셀 인식, 실제 조회 결과 또는 Excel 내보내기 업무를 검증했다는 뜻이 아니다.

0.1.1에서 단위 및 복구 테스트 103개, 실제 Windows 합성 앱의 녹화 및 영역 입력 16개, 작업 실행 연결 13개가 통과했다. Python 소스 교체와 기존 실행 및 저장 형식의 호환성을 확인한 결과다.

## 재현 명령

64비트 Python 3.10 이상과 tkinter가 있는 Windows에서 실행한다. 제품 도우미는 Python 소스이므로 빌드할 필요가 없다. agent-desktop 1.1.0 원본 EXE는 `UPSTREAM.json`의 해시와 일치하는 파일을 `bin/agent-desktop.exe`로 준비한다. 일반 사용자는 해당 EXE 하나가 포함된 Windows ZIP을 사용한다. 소스 ZIP에는 EXE가 없다. 배포 도구는 자동 다운로드나 보안 설정 변경을 하지 않는다.

```powershell
python -X utf8 -B tools/build.py
python -X utf8 -B -m unittest discover -s tests -v
python -X utf8 -B tools/smoke_native.py --work-dir C:\LocalReview\NativeTest
python -X utf8 -B tools/smoke_workflows.py --work-dir C:\LocalReview\WorkflowTest --recording C:\LocalReview\NativeTest\recording\draft.json
python -X utf8 -B tools/build.py
```

테스트마다 새로운 빈 작업 폴더를 지정한다. 두 `smoke` 명령은 개발자 검증용으로 제공된 C# 합성 앱을 로컬 .NET Framework 컴파일러로 빌드하고, 그 앱만 열어 조작한 뒤 닫는다. 이 시험용 EXE는 제품 ZIP에 포함되지 않으며 일반 녹화/실행 중 생성되지 않는다. 물리 입력을 사용하는 동안 다른 자동화나 사용자의 마우스 입력을 함께 실행하지 않는다. 결과는 지정한 폴더의 `summary.json`에 기록된다.

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
