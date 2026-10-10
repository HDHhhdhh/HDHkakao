# KakaoSender 메인 화면 디자인

대표 화면은 현재 로컬 프로젝트의 KakaoSender 메인 화면이다. Spoiler의 WPF 화면은 이번 변경 범위에 포함하지 않는다.

## 확인한 기술 기반

| 항목 | 기존 구조 | 이번 선택 |
|---|---|---|
| 프레임워크 | Python 3.12, Tkinter | 유지 |
| UI 위젯 | 기본 ttk, `clam` 테마 | 유지 |
| 파일 드롭 | 기존 `tkinterdnd2` | 유지 |
| 스타일 | `configure_style`의 색상·글꼴·ttk 스타일 | `kakao_gui_theme.py`로 시각 토큰과 스타일 분리 |
| 아이콘 | 텍스트 버튼 중심, 별도 라이브러리 없음 | 기존 텍스트 버튼 유지 |

기존 버튼 명령, 키보드 단축키, 발송 잠금, 데이터 모델, API, CSV/XLSX와 설정·로그 포맷을 유지한다. 추가 패키지를 설치하지 않는다.

## 시각 기준

- 회색 계열의 배경과 흰 표면을 사용한다. 발송 시작에는 파란색을 사용한다.
- 중지는 빨간 글자와 경계로 표시한다. 일반 버튼과 비활성 버튼에는 중립색을 사용한다.
- 맑은 고딕을 유지한다. 본문은 10pt, 보조 설명은 최소 9pt, 영역 제목은 11pt로 표시한다.
- 버튼 모서리는 6px로 맞춘다. 일반 테두리는 1px, 키보드 포커스는 2px로 표시한다. 버튼 크기는 상태가 바뀌어도 유지한다.
- 버튼의 기본, 마우스 오버, 누름, 키보드 포커스, 비활성 상태를 별도로 지정한다.
- 처리 중에는 기존 실행 상태를 관찰해 `준비 중` 또는 `처리 중`을 표시한다. 기존 비활성 잠금과 콜백은 그대로 둔다.
- 입력란, 표, 첨부 목록과 로그의 색상 및 선택 표시를 통일한다. 본문 뒤에는 무늬나 장식 애니메이션을 넣지 않는다.

## 참고 소스와 고지

아래 소스의 디자인 원칙을 참고했다. React, Tailwind, CSS 코드를 이 프로젝트에 복사하지 않았다. Tkinter 테마와 버튼 이미지 생성 코드는 이 프로젝트에 맞춰 직접 작성했다.

| 참고 소스 | 사용 범위 | 확인한 라이선스 |
|---|---|---|
| [shadcn/ui 테마](https://ui.shadcn.com/docs/theming), [버튼 소스](https://github.com/shadcn-ui/ui/blob/main/apps/v4/registry/new-york-v4/ui/button.tsx) | 표면·글자·경계의 시각 토큰, 버튼 계층과 포커스/비활성 상태 | [MIT, Copyright (c) 2023 shadcn](https://github.com/shadcn-ui/ui/blob/main/LICENSE.md) |
| [tweakcn](https://github.com/jnsahaj/tweakcn) | 밝은 중립 테마의 색상 역할 구분 | [Apache-2.0](https://github.com/jnsahaj/tweakcn/blob/main/LICENSE) |

[Uiverse Galaxy](https://github.com/uiverse-io/galaxy)와 [Pattern Craft](https://github.com/megh-bari/pattern-craft)는 웹용 구현이며 이번 화면에 가져오지 않았다. [Lucide](https://github.com/lucide-icons/lucide)의 아이콘 파일도 포함하지 않았다. 업무 화면의 기존 텍스트 버튼을 유지해 추가 자산 로딩을 피했다.

소스와 라이선스 링크는 2026-10-07에 확인했다. 위 표는 출처를 기록하며 각 저장소의 라이선스 파일을 대체하지 않는다.

## 확인 방법

기존 `tests/test_kakao_gui_layout.py`의 격리된 실제 Tk 화면을 사용한다. 이 시험 장치는 인증 저장소, 네트워크, 발송 서비스와 카카오톡 조작을 차단한다. 시험용 CSV와 PDF만 사용한다.

```powershell
python -m pytest -q -p no:cacheprovider tests/test_kakao_gui_theme.py tests/test_kakao_gui_layout.py::GuiLayoutTests tests/test_kakao_gui_feedback.py tests/test_kakao_gui_polish.py tests/test_kakao_gui_send_preparation_cancel.py tests/test_kakao_gui_run_lifecycle.py tests/test_kakao_gui_send_status_reset.py
python exports/gui-design-20261007/check-scaling.py 1.3333333333333333
python exports/gui-design-20261007/check-scaling.py 2.0
python exports/gui-design-20261007/check-scaling.py 2.6666666666666665
```

작은 창과 큰 창, 100%·150%·200%에 해당하는 Tk 배율에서 표, 로그, 첨부와 설정의 접근성을 확인한다. 이 PC에서는 한 프로세스에서 Tk를 반복 초기화할 때 창이 종료되거나 표시 초기화가 생략되는 오류가 있었다. 따라서 배율별 검사는 새 프로세스에서 같은 배치 검증문으로 실행했다. 100%와 150%는 1024×720, 1280×800, 1600×900에서 통과했다. 200%는 2048×1440, 2560×1600에서 통과했다.

실제 위젯을 사용하는 시험용 대표 화면도 실행했다. 현재 디스플레이의 배율에서는 2048×1440으로 생성됐고 시험용 명단 3건을 표시했다. 미리보기 장치의 외부 작업 시도 수는 0이었다. 컴퓨터 제어 도구의 창 활성화가 `foreground window did not report a process id` 오류로 실패해 최종 화면 캡처를 시각 검증 증거로 사용하지 않았다.

초기 배치 검사에서 로그 본문이 44px로 계산되는 문제를 찾아 카드의 경계와 내부 여백에 맞춰 높이를 보완했다. 최종 크기 검사에서 로그 최소 45px 기준을 유지했다. 실제 발송이나 예약은 시험하지 않았다.

최종 관련 검사 48개와 하위 검사 90개가 통과했다. 별도 배율 검사 8개 창 크기도 통과했다. 기존 코드의 함수 인자와 반환 선언, 전역 함수, 실행 피드백 모듈은 변경 전과 동일함을 별도로 확인했다. 변경 및 검증 기록은 `exports/gui-design-20261007`에 보관한다.
