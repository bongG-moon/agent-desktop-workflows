# 녹화 입력창과 저장 흐름 검증

검증일: 2026-11-21. 대상은 현재 작업본으로 만든 Desktop Select 0.4.26 ZIP이다. 기존에 수정 중이던 코드를 유지했고, 개인 Claude 설정과 녹화 저장소를 변경하지 않았다.

## 수정한 동작

- 녹화를 마치거나 저장본을 편집할 때 실행 문장 입력란에 초점을 준다. 사용자가 단계 목록을 직접 탐색할 때는 목록에서 초점을 빼앗지 않는다.
- 일반 Enter로 다음 단계에 넘어가지 않는다. 한글 입력 확정에 사용할 수 있으며, 적용 버튼 또는 Ctrl+Enter로 다음 단계에 적용한다.
- 편집할 수 없는 특수 메뉴 단계는 그 이유와 수정 방법을 표시한다.
- 저장 결과를 기록하고 녹화 프로세스를 끝낸다. 별도의 저장 완료 창이 계속 남거나 수정·취소 후 반복해서 나타나는 기본 흐름을 제거했다.
- 원본 녹화, 초안, 수정과 재녹화 명령은 유지한다. 수정과 재녹화는 채팅에서 요청할 수 있고 새 사본으로 저장된다.

## 검사 결과

관련 자동 검사 63개를 통과했다. 초기 입력 초점, 일반 Enter 시 단계 유지, Ctrl+Enter 및 버튼 적용 뒤 초점 복귀, 직접 목록 탐색, 잘못된 설명의 초안 보존, 저장 영수증과 후속 명령, 준비 상태와 세션 종료를 확인했다. Tk의 키 이벤트 검사는 실제 Windows 한글 IME 전체 동작을 보장하지 않는다.

수정 ZIP을 별도 프로젝트의 `.claude/skills/desktop-select`에 설치했다. 실제 Claude Code CLI는 `Skill(desktop-select)` 후 `recorder --app "메모장"`을 호출해 녹화창을 열었다. 이후 검증자가 Windows 화면 조작으로 시연, 설명 입력과 저장을 진행했다. 녹화 화면의 클릭과 타이핑을 Claude 모델이 직접 수행한 것은 아니다. 저장 후 새 Claude Code 호출에서 `try-recording --session ... --allow-physical`을 선택했다.

검증자는 기존 개인 메모장 문서와 다른 빈 탭을 만들고 합성 시험 문장을 입력했다. 수정 전에는 검토 화면의 입력 초점이 보장되지 않았다. 수정 ZIP에서는 클릭 없이 일반 키 입력과 한국어 문장 입력을 확인했고, Enter 후 문장이 유지됐다. 저장된 초안에서 `메모장 본문을 선택해줘`라는 문장을 다시 읽었다. 저장 후 `complete.json`의 종료 코드는 0이었고 Desktop Select 창이 남지 않았다.

## 기본 메모장의 제한

이 PC의 기본 메모장 본문은 일반 `textfield`로 노출되지 않았다. 초기 관찰은 창 제어 버튼만 보였고, 이후 저장 후 복구 응답에서는 `document` 역할의 `텍스트 편집기`가 보였다. 현재 녹화기는 이 편집기에 일반 입력란과 동일한 읽기·쓰기 계약을 적용하지 못한다. 수정 전 시연에는 `free_text_not_recorded`, `accessibility_unavailable`, `target_lifetime_changed`가 남았다. 메모장이 입력한 첫 줄로 제목을 바꾸는 동작도 현재 창 제목 고정 검사에 영향을 준다.

실제 Claude Code의 후속 시험은 `needs_grounding`, `window_root_not_replay_target`, `input_dispatched: false`로 끝났다. 본문 입력 또는 재생 성공으로 판정하지 않았다. 창 전체를 임의로 입력 대상으로 지정하거나 다른 자동화 도구로 대체하지 않았다. 일반 문서 편집기를 지원하려면 입력 대상 식별, 보호 상태, 읽기·쓰기 및 결과 검증을 갖춘 별도 지원이 필요하다.

## Claude 실행 환경

첫 호출은 OAuth 만료로 도구 호출 전에 종료됐다. 사용자가 직접 재로그인한 뒤 실제 모델 호출이 정상 진행됐다. 사용자 설정 해시는 두 성공한 모델 호출의 전후에 같았다. 기존 Company Agent 1.5.1 후크가 개인 상태에 별도 검증 기록을 쓰는 명령을 추가로 요구했으나, 해당 명령은 이번 합성 시험의 허용 범위 밖으로 거절됐다. Desktop Select 자체의 녹화와 첫 시험 명령은 실제 실행됐다.

## 로컬 증거와 결과물

- 수정 전 설치 ZIP: `.build/notepad-20261121-baseline-package/desktop-select-0.4.26-windows-x64.zip`
- 수정 ZIP: `.build/notepad-20261121-fixed-package/desktop-select-0.4.26-windows-x64.zip`
- Claude가 새 녹화창을 연 기록: `.build/claude-notepad-20261121-fixed/summary.json`
- 설치한 시험 스킬과 보존한 초안: `.build/claude-notepad-20261121-fixed/project/`
- Claude의 후속 시험 기록: `.build/claude-notepad-20261121-try/summary.json`

공개 GitHub 릴리즈는 이번 검증에서 변경하지 않았다.
