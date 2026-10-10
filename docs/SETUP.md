# 재현 가능한 Windows 설치

검증 기준은 Windows, CPython **3.12.10**, Node.js **24.19.0**, `agent-messenger` **2.37.1**입니다. Python 실행 의존성 전체는 `requirements.lock.txt`, 테스트 도구는 `requirements-dev.txt`, Node 의존성 전체는 `bridge/package-lock.json`에 버전을 고정했습니다.

## 새 환경 설치

프로젝트 폴더의 PowerShell에서 실행합니다. 가상환경 활성화는 필요하지 않습니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
node --version
npm --prefix .\bridge ci --ignore-scripts --no-audit --no-fund
.\.venv\Scripts\python.exe .\kakao_sender_gui.py
```

`requirements.txt`는 고정된 실행 의존성 목록을 읽습니다. `npm ci`는 저장된 잠금 파일의 정확한 버전을 설치합니다. `--ignore-scripts`는 SDK에 함께 포함된 다른 메신저용 설치 스크립트를 실행하지 않습니다. 카카오 브리지 검증에는 SDK 모듈과 필요한 인터페이스를 불러오는 검사도 포함합니다.

Node SDK의 탐색 순서는 명시적 설정의 `sdk_root`, `KAKAO_AGENT_MESSENGER_ROOT`, 프로젝트의 `bridge/node_modules/agent-messenger`, 프로젝트의 `agent-messenger`, 기존 Spoiler 설치 경로입니다. 기존 설정에 외부 `sdk_root`가 저장되어 있으면 해당 경로를 계속 사용합니다. `연결 준비 점검`에서 실제 선택된 SDK와 버전을 확인하세요.

Python 설치에는 Tkinter가 포함되어 있어야 합니다. Windows의 Python 정식 설치 프로그램에서 Tcl/Tk 옵션을 포함하면 됩니다. 파일 드래그앤드롭에는 잠금 파일에 포함된 `tkinterdnd2`가 필요하며, 로딩하지 못한 환경에서도 파일 선택과 경로 입력은 사용할 수 있습니다. 직접 전송을 켠 즉시 텍스트·첨부 행에는 화면 좌표가 필요하지 않습니다. 즉시 화면 전송에는 PC 카카오톡과 화면 좌표 설정이 필요합니다. 자동 예약은 PC 카카오톡의 실제 창을 찾아 제어하며 Windows OCR의 한국어 인식 기능을 사용합니다. Codex·LLM API는 사용하지 않습니다.

## 인증과 사용자 데이터

SDK 설치와 태블릿 인증은 별도 단계입니다. 기존 계정과의 호환을 위해 인증은 `%LOCALAPPDATA%\Spoiler\loco-credentials.dat`의 Windows DPAPI 보관소를 사용하며, 이 프로젝트의 설치 명령은 인증 정보를 읽어서 출력하지 않습니다. 새 PC에서 직접 전송을 사용하려면 Spoiler의 태블릿 설정에서 사용자가 등록 절차를 완료해야 합니다.

자동 인증 복구를 사용하려면 아래 명령을 한 번 실행합니다. 계정 비밀번호는 프로젝트 설정이나 로그에 기록되지 않고 `%LOCALAPPDATA%\Spoiler\tablet-account.dat`에 현재 Windows 사용자용 DPAPI로 암호화됩니다.

```powershell
.\.venv\Scripts\python.exe .\kakao_tablet_reauth.py setup
```

그 뒤 `refresh_rejected`, 토큰 만료, 태블릿 인증 해제가 명시적으로 확인되면 Hub가 설치된 Spoiler 인증기를 호출해 같은 태블릿 UUID로 한 번 재등록합니다. 단순 네트워크 끊김에는 계정 비밀번호를 사용하지 않습니다. 새 기기 인증이 필요한 경우 Hub에 표시되는 인증번호를 카카오톡에서 확인해야 합니다.

업데이트 뒤 Spoiler를 먼저 실행하고 Hub를 다시 시작하세요. Spoiler가 검증 후 공개한 `active-runtime.manifest`와 파일 해시로 인증기를 선택합니다. 공개 정보가 없거나 파일 검증에 실패하면 오래된 캐시를 추측해 실행하지 않고 Spoiler 재시작을 안내합니다. 두 앱의 OAuth 갱신·태블릿 등록은 공용 잠금으로 조율하며, 기다린 앱은 같은 계정·기기의 새 인증 정보를 다시 사용합니다. Spoiler의 수동 등록 중 다른 앱이 인증 중이면 잠시 뒤 다시 시도하세요.

앱 설정과 매핑은 `%APPDATA%\KakaoSender`, 결과 기록은 `%LOCALAPPDATA%\KakaoSender` 아래에 보관합니다. 프로그램 소스나 의존성을 다시 설치해도 사용자 설정 파일을 덮어쓰지 않습니다. `연결 준비 점검`의 로컬 확인은 메시지를 보내지 않습니다.

## 개발·회귀 검사

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider
npm --prefix .\bridge test
```

테스트는 합성 데이터, 모의 프로세스, 임시 설정을 사용합니다. Node 테스트는 전송 WRITE가 한 번만 호출되는지, 오류 후 결과 불명이 유지되는지와 채팅 메타데이터 검증을 확인합니다. 실제 계정 로그인·발송·카카오 창 조작은 별도 운영 점검입니다.

## 버전 변경

Python 의존성을 변경할 때는 새 가상환경에서 직접 의존성과 전이 의존성 버전을 함께 갱신하고 위 회귀 검사를 실행합니다. Node SDK 변경 시에는 `bridge/package.json`의 정확한 버전과 `kakao_direct.EXPECTED_SDK_VERSION`을 함께 변경하고 잠금 파일을 다시 생성합니다.

```powershell
npm --prefix .\bridge install --package-lock-only --ignore-scripts --no-audit --no-fund
npm --prefix .\bridge ci --ignore-scripts --no-audit --no-fund
```

기존 `node_modules`를 복사해서 배포하지 말고 잠금 파일로 설치합니다. `package-lock.json`의 내려받기 주소는 공식 npm 레지스트리로 제한되어 있습니다.
