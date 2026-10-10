# HDHkakao 통합 프로젝트

프로젝트의 목표와 개발 경과는 [개발 기록](history/README.md)을 참고한다. 오류 수정 과정과 구조 변경을 과거 세션·검증 자료에서 재구성했고, 실제 시험과 남은 한계를 구분했다.

KakaoSender는 저장소 루트에 있습니다. Spoiler는 `spoiler/`에 있습니다. 기존 KakaoSender 실행 파일 경로와 Python 모듈 경로를 유지하기 위해 이 구성을 사용합니다.

| 위치 | 내용 |
|---|---|
| `kakao_*.py` | KakaoSender 프로그램 |
| `bridge/` | KakaoSender의 Node 브리지와 의존성 잠금 파일 |
| `tests/` | KakaoSender 회귀 검사 |
| `spoiler/src/` | Spoiler의 C#·WPF 소스 |
| `spoiler/bridge/` | Spoiler의 Node 브리지와 검사 |
| `spoiler/test/` | Spoiler의 C# 검사와 합성 자료 |
| `spoiler/assets/` | Spoiler 아이콘과 화면 자산 |
| `docs/` | 설치·기능·개선 기록 |

## 새 Windows PC 준비

Python 3.12, Node.js 24.19.0과 .NET Framework의 빌드 구성요소를 준비합니다. 정확한 버전과 설치 방법은 [KakaoSender 설치 안내](SETUP.md)와 [Spoiler 빌드 안내](../spoiler/README.md)를 따릅니다.

저장소 루트에서 KakaoSender를 설치하고 실행합니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm --prefix .\bridge ci --ignore-scripts --no-audit --no-fund
.\.venv\Scripts\python.exe .\kakao_sender_gui.py
```

Spoiler는 해당 폴더에서 빌드합니다.

```powershell
Push-Location .\spoiler
try {
    .\build.ps1 -RestoreDependencies
} finally {
    Pop-Location
}
```

결과는 `spoiler/build/Spoiler.exe`입니다. 새 PC에서는 Spoiler의 태블릿 설정에서 사용자가 인증해야 합니다. 두 프로그램은 `%LOCALAPPDATA%\Spoiler`의 기존 DPAPI 인증 보관소와 잠금을 사용합니다. 서로 다른 Windows 사용자나 PC로 인증 파일을 복사해서 사용할 수 없습니다.

Git에는 프로그램 소스·검사·필수 화면 자산·예제·의존성 잠금 파일을 보관합니다. `exports/`, `build/`, `dist/`, `node_modules/`, 실제 인증 파일과 실행 기록은 제외합니다. 실제 등록·발송 시험은 일반 회귀 검사에 포함하지 않습니다.

공개 저장소에는 실제 학생의 대화명 부분 화면과 `native_schedule_recipient_samples.json`을 넣지 않습니다. 이 PC의 해당 파일은 그대로 보존합니다. 새 PC에서는 해당 자료 없이 기본 OCR을 사용합니다. 지역 화면 자료를 확인해 사용하는 선택 기능의 빈 형식은 `native_schedule_recipient_samples.example.json`에 있습니다. 개인 자료가 필요한 검사 2개만 새 복제본에서 건너뛰며, 합성 자료로 픽셀 일치·다른 대상 거부를 검사하는 회귀 검사는 계속 실행합니다.

공개 예제와 검사 코드의 대화명은 합성 이름입니다. 공개 첫 커밋은 기존 로컬 운영 기록이 포함되지 않도록 새 이력으로 만듭니다. 이전 로컬 Git 이력은 보존합니다.

원래 Spoiler 프로젝트에서 `spoiler/`로 복사한 파일은 원본과 SHA-256을 대조했습니다. 앞으로 통합 소스의 Spoiler 수정은 이 저장소의 `spoiler/`에서 진행합니다. 기존에 설치된 Spoiler의 교체는 별도 설치 작업입니다.
