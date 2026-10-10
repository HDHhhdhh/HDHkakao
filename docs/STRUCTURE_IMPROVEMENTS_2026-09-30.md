요청한 후속 제안 **1·2·5번**을 KakaoSender와 Spoiler에 구현했다. KakaoSender는 현재 프로젝트 소스에 적용했고, Spoiler는 원본 프로젝트의 소스·개발 의존성·`build/Spoiler.exe`에 적용했다. 변경 전 소스와 실행파일은 작업 증거 폴더에 보존했다.

| 선택한 개선 | 적용한 동작 |
|---|---|
| 1. 빌드 복구·인증 런타임 통일 | 누락된 package·lock·빌드 스크립트·인증 검사를 복구. Node 24.19.0 바이너리 해시, SDK 2.37.1, esbuild 0.28.2를 고정하고 현재 소스로 번들을 빌드. Spoiler가 검증 후 공개한 `active-runtime.manifest`를 Hub가 같은 형식으로 검증 |
| 2. 두 앱의 서버 인증 갱신 조율 | 공용 잠금으로 OAuth 갱신·태블릿 등록을 조율. 대기 앱은 잠금 획득 후 저장소를 다시 읽고 같은 계정·기기의 새 토큰을 재사용. 기존 저장 버전 검사·저장 ACK 유지 |
| 5. 책임 분리·레거시 정리 | GUI의 매칭·결과 표시·이력 및 진단 창을 분리. Spoiler의 인증·수신 저장·프로토콜·프로세스 실행·JSON 파싱을 분리. 미사용 MessageCheckpointStore 1,115행 및 전용 검사를 정리하고 커서 DTO·필요한 검증은 현재 InboxStore에 보존 |

Hub 인증기는 파일 수정 시각이나 임의의 이전 캐시를 기준으로 선택하지 않는다. 공개 manifest의 버전·파일 크기·SHA-256을 모두 확인하고, Windows에서는 검증한 파일의 쓰기·교체를 차단하는 읽기 핸들을 자식 프로세스 종료까지 유지한다. 공개 정보가 없거나 검증이 실패하면 Spoiler 재시작을 안내한다. 직접 전송의 기존 Node·SDK 명시 경로 설정은 유지했다.

공용 인증 잠금은 암호화 저장소 옆 `.loco-credentials.dat.auth.lock`의 같은 바이트를 Python과 C#이 잠근다. 로컬 저장용 잠금과 분리해 서버 작업 동안 저장도 가능하다. 백그라운드 잠금 대기는 최대 10초이며 앱 상태 잠금을 잡고 기다리지 않는다. Spoiler 수동 등록은 다른 앱이 인증 중이면 즉시 안내한다. 갱신 프로세스의 65초 감시, 중단 시 대기 취소, 종료 확인 후 잠금 해제, 오래된 응답의 저장 거부를 포함한다. 다른 계정·기기로 바뀐 응답은 재사용하지 않는다. 새 자동 강제 등록 동작은 추가하지 않았다.

통합 검사에서 인증 저장 ACK의 중첩 제한시간 경쟁을 발견해 수정했다. 단일 입력 대기 제한시간을 사용하고, 해당 시간 초과만 기존 ACK 시간 초과 코드로 변환한다. 조기 응답 보관·저장 완료 후 세션 연결·이전 메시지 복구 순서 검사는 유지했다.

GUI 주 파일은 5,762행에서 3,423행으로, Spoiler 수신 주 파일은 1,801행에서 745행으로 줄었다. 이동한 GUI 함수·메서드 49개의 AST 본문이 이전과 동일함을 확인했다. Tk 상태와 화면 반영은 기존 앱·소유 스레드에 유지했다. 별도 검토에서도 순환 import, 초기화 MRO 충돌, 누락된 전역이나 메서드를 찾지 못했다. 계정 이력 파일을 삭제하거나 레거시 데이터를 임의로 이전하지 않았다.

검증은 실제 Windows 파일 잠금·DPAPI와 가상 SDK·가상 자식 프로세스·격리된 시험 데이터를 사용했다.

- Hub 전체: **820 passed, 1 skipped, 367 subtests passed**, 43.76초. Windows에 적용되지 않는 POSIX 파일 권한 대체 경로 1건을 건너뛰었다. [로그](../exports/structure-improvements-20260930/hub-final-tests.txt)
- 이후 추가한 계정 전환 거부 검사를 포함한 인증 관련 검사: **54 passed**. 실제 C#↔Python 잠금 상호 운용, 소유 프로세스 종료 후 해제, 가상 서버 갱신 1회와 대기 앱의 승자 재사용, 중단 및 오래된 응답 거부를 확인했다. [로그](../exports/structure-improvements-20260930/auth-python-final.txt)
- Spoiler 통합 빌드: Node **64개**, InboxStore **13개**, 가상 자식 프로세스 **14개** 시나리오, core·grouping·런타임·데모 창 계약 검사 통과. [로그](../exports/structure-improvements-20260930/spoiler-final-build-verified.txt)
- 원본에서 고정 의존성 **209개** 복구 후 새 실행파일 컴파일·내장 런타임 자체 검사 통과. 독립적으로 빌드한 두 bridge 번들과 manifest의 SHA-256이 스테이지 결과와 동일했다. EXE 자체의 바이트 단위 재현성까지 주장하지 않는다. [빌드 로그](../exports/structure-improvements-20260930/spoiler-original-build.txt), [적용·해시 기록](../exports/structure-improvements-20260930/spoiler-build-applied.json)
- 마지막 진단 검토에서 Node·C# 양쪽의 고정 문구 목록에 새 인증 대기·조율·계정 변경 오류를 추가했다. 임의의 민감 원문 9개 조합을 검사했고, 등록 종료 실패 시 프로세스 소유권을 유지하는 방어도 포함했다. 추가 변경 후 최종 소스 컴파일·내장 런타임 검증 및 원본 적용 파일 **31개**의 해시 확인을 완료했다. [문구 검사](../exports/structure-improvements-20260930/auth-diagnostics-tests.txt), [최종 원본 빌드](../exports/structure-improvements-20260930/spoiler-original-final-build.txt), [최종 기록](../exports/structure-improvements-20260930/completion.json)
- GUI 경계 분리 증거: [본문 일치 기록](../exports/structure-improvements-20260930/gui-extraction-evidence.json). Spoiler 삭제 전 검증을 현재 저장소로 옮긴 근거: [책임 분리 검토](../exports/structure-improvements-20260930/spoiler-boundaries/review.md)

**Spoiler를 먼저 다시 실행하고 Hub도 재시작해야 적용된다.** 실행 중인 사용자 앱을 재시작하지 않았다. 실제 계정 로그인·태블릿 교체·메시지 발송·예약 등록은 수행하지 않았다. 위 검증은 인증 조율과 저장 순서를 확인하며, 카카오 서버의 모든 네트워크 상황에서 지속 동작한다는 보장은 아니다.

이번 후속 제안의 3번 실제 GUI 결과 재확인과 4번 기간별 수집·CSV 내보내기는 선택 범위에 포함되지 않았다. 앞선 검토의 번호와 이번 후속 제안의 번호는 서로 다르다.
