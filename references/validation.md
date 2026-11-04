# 검증 범위

이 제품의 시험은 소스에 포함된 합성 Windows 앱과 로컬 저장소를 대상으로 한다. 회사 FX, FxMenuAgent, FarPoint 셀 인식, 실제 조회 결과 또는 Excel 내보내기 업무를 검증했다는 뜻이 아니다.

## 재현 명령

Python 3.10 이상과 tkinter가 있는 Windows에서 실행한다. 원본 소스만 받은 경우 로컬 .NET Framework 컴파일러로 자체 도우미를 먼저 빌드한다. agent-desktop 1.1.0 원본 EXE는 `UPSTREAM.json`의 해시와 일치하는 파일을 `bin/agent-desktop.exe`로 준비한다. 일반 사용자는 두 EXE가 포함된 Windows ZIP을 사용한다. 빌드 과정은 자동 다운로드나 보안 설정 변경을 하지 않는다.

```powershell
powershell -NoProfile -File tools/build_native.ps1
python -X utf8 -B -m unittest discover -s tests -v
python -X utf8 -B tools/smoke_native.py --work-dir C:\LocalReview\NativeTest
python -X utf8 -B tools/smoke_workflows.py --work-dir C:\LocalReview\WorkflowTest --recording C:\LocalReview\NativeTest\recording\draft.json
python -X utf8 -B tools/build.py
```

테스트마다 새로운 빈 작업 폴더를 지정한다. 두 `smoke` 명령은 각각 자신이 만든 합성 앱을 열어 조작하고 닫는다. 물리 입력을 사용하는 동안 다른 자동화나 사용자의 마우스 입력을 함께 실행하지 않는다. 결과는 지정한 폴더의 `summary.json`에 기록된다.

## 확인 항목

- 저장 원본과 수정본 보존, 변수와 현재 대상 확인, 중단 및 수정 후 이어하기
- 실행 전 저널 보존, 불확실한 입력의 반복 방지, 프로세스 종료 후 잠금 복구
- ref 없는 컨테이너 영역의 실제 우클릭과 이동 및 크기 변경 후 재식별
- 실제 녹화에서 값 변경과 영역 우클릭 포착, 미지원 단계의 안내 및 보존
- 별도 실행기가 새 관찰/ref로 각 단계를 실행하고 독립된 합성 앱 상태로 결과 확인
- 기존 파일과 이번 실행이 바꾼 파일 구분, 업무 완료 미확인을 성공으로 올리지 않음
- 실행 요약에 업무 이름과 관찰 원문을 노출하지 않음

## 현장 확인이 필요한 부분

FX/FxMenuAgent의 실제 화면에서 메뉴, 콤보, 빈 접근성 표, 동적 편집기와 팝업을 녹화해 인식 상태를 확인해야 한다. 노출되지 않는 셀 내용은 이 제품이 새로 읽어 내지 못한다. 영역 우클릭은 표의 특정 행을 식별한 것과 다르다. 사라지는 팝업, 마우스를 올려서만 여는 하위 메뉴, 스크롤과 단축키는 수동 보완 단계가 될 수 있다.

조회는 실제 완료 신호가 있어야 자동 검증할 수 있다. 단순 파일 변경 확인은 파일 내용이나 내보내기 범위까지 검증하지 않는다. 사람 확인과 자동 관찰, 입력 전달만 확인한 상태를 실행 기록에서 구분한다.
