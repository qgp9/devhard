# DevHard

UI에서 **Dark → DevHard**를 선택하면 로고에서 가져온 차콜·은색·붉은색 테마를
사용할 수 있습니다. **Light → DevHard Light**는 은백색 배경의 밝은 버전입니다.
두 테마 모두 Rounded·Flat·Terminal 스타일과 조합할 수 있습니다.

[English](README.md) | **한국어**

독립된 Chrome 프로필과 SSH SOCKS5 터널을 워크스페이스별로 관리하는 런처입니다.
M4까지 설정 저장, Manager 잠금, 포트 예약, Chrome/SSH 개별 제어, 전체 시작·종료,
로컬 API와 관리 UI를 구현했습니다. 설계·개발 기록은 형제 디렉터리 `../notes/`에 있습니다.

## 개발 환경

Python은 `mise.toml`에서 **3.13.16**으로 고정합니다. HTTP 계층은 FastAPI/Uvicorn을,
프로세스·저장 코어는 표준 라이브러리를 사용합니다. `uv.lock`으로 의존성을 고정하며
mise가 프로젝트의 `.venv`를 활성화합니다. mise와 uv가 필요합니다.

```sh
mise trust
mise install
mise run setup
mise run test
mise run devhard -- --help
```

터널 제어에는 OpenSSH, `ps`, `lsof`가 필요합니다. macOS 프로세스 식별은 libproc,
Chrome 정상 종료는 AppKit을 사용합니다. Linux 실제 검증은 M5에서 진행합니다.

## 관리 화면 사용

```sh
mise run devhard -- manager run --open
```

Manager는 127.0.0.1의 자동 할당 HTTP 포트에서 관리 화면을 제공하며 준비된 주소를
JSON의 `api_url`로 출력합니다. `--open`은 일반 기본 브라우저에서 해당 주소를 엽니다.
`--port 61400`으로 고정 포트를 사용할 수도 있습니다. 관리 HTTP 포트는 워크스페이스의
SOCKS 예약 포트와 별개입니다.

화면 기본 언어는 영어입니다. 오른쪽 위 **English / 한국어** 선택기로 전환할 수 있습니다.
선택은 해당 origin의 브라우저 저장소에 유지되고 다른 관리 탭에도 동기화됩니다.
저장소를 사용할 수 없으면 현재 페이지에서만 적용됩니다. 자동 포트가 바뀌면 새 origin이므로
기본 영어로 시작합니다. 언어 전환은 입력 중인 폼과 실행 중인 프로세스를 유지합니다.
서버·SSH가 반환한 원래 오류 메시지와 사용자 입력값은 번역하지 않습니다.

1. **＋ 워크스페이스**에서 SSH 호스트와 이름·설명·HTTP(S) URL을 가진 북마크를 등록합니다.
2. **전체 시작**으로 SSH와 독립 Chrome을 실행합니다. SSH 또는 Chrome만 제어할 수도 있습니다.
3. **전체 종료**는 두 프로세스를 종료하고 포트 예약을 해제하며 Chrome 프로필은 보존합니다.
4. 북마크는 실행 중에도 편집·추가·삭제할 수 있습니다. 연결·브라우저 설정을 바꾸려면 Chrome과 SSH를 모두 종료합니다. 워크스페이스 이름은 변경할 수 없습니다.

북마크의 **Initial**은 최초 성공한 Chrome 실행에만 자동으로 여는 표시입니다. 이후에는 모두
일반 북마크이며 **열기**는 Chrome의 기본 `--focus` 옵션으로 일치하는 URL의 탭으로 이동하고,
없으면 새 탭을 엽니다. **모두 열기**는 북마크별로 처리합니다. 기존 세션·PID·터널·예약은
유지합니다. Chrome이 꺼져 있으면 먼저 시작해야 하며 북마크 열기가 Chrome이나 SSH를
자동 시작하지 않습니다. 작업 완료는 Chrome 명령 전달 완료이며 실제 페이지 로드 성공을 뜻하지 않습니다.

URL 일치 여부는 Chrome이 판단합니다. 리다이렉트된 페이지는 저장된 URL과 일치하지 않을 수
있습니다. 쉼표나 끝의 `*`는 선택자 문법이므로 해당 URL은 일반 새 탭으로 엽니다. 구버전
Chrome에서도 중복 탭이 생길 수 있습니다. 결과 보고가 없으면 이미 열렸을 수 있어 재전송하지
않습니다. 명시적인 선택자 파싱 오류만 focus 없이 한 번 재시도합니다. 제품 실행에는 CDP나
탭 목록 폴링을 사용하지 않습니다.

이후 실행은 이전 세션을 복원하거나, 복원을 끄면 빈 탭을 엽니다. Initial을 반복 적용하지 않습니다.
Initial 표시를 편집해도 최초 실행 기록은 초기화되지 않습니다. Chrome 자체의 시작·충돌 복구
설정은 강제 변경하지 않습니다. 일반 브라우저에서 관리 주소를 여러 창이나 탭으로 열면
같은 상태와 작업 이력을 확인할 수 있습니다. DevHard가 실행한 프록시 브라우저는 관리 화면에
사용하지 않습니다. 그 브라우저의 localhost는 원격 서버를 가리킵니다.

요청 접수와 실행 완료는 구분합니다. 작업은 대기 중→실행 중→완료·부분 성공·실패로 표시됩니다.
Chrome 실행 실패로 부분 성공한 경우 정상 SSH 터널을 유지합니다. 상태 조회는 관측 캐시를 읽어
긴 제어 작업 중에도 응답합니다. 최근 오류와 소유권 확인 불가 상태도 표시합니다.

화면이나 Manager를 닫아도 실행 중인 Chrome과 SSH는 유지됩니다. Manager 재시작 후에는
새 주소를 열거나 페이지를 새로고침하세요. 고정 포트여도 제어 토큰이 바뀝니다. 프로세스와
예약은 재발견하지만 작업 이력은 Manager 실행별 메모리에만 남습니다. 최근 이력은 100개,
미완료 요청은 32개로 제한합니다. 정상 종료 시 진행 중 작업을 마치고 대기 작업을 취소합니다.

## 테마 컨트롤의 접근성

관리 화면 헤더의 모드·컬러셋·모양 컨트롤은 네이티브 `<select>` 대신 하나의
직접 만든 드롭다운 컴포넌트(`app.js`의 `createDropdown`)를 공유합니다. 그래서
세 컨트롤이 모든 테마·모양 변형에서 동일하게 보입니다. 상태는 `theme.js`
(모드, 밝기별 스킴, 모양)가 단일 소스이고, 뷰는 렌더마다 이를 읽으며 선택은
콜백으로 되씁니다. 동기화할 두 번째 상태 사본이 없습니다.

"네이티브 `<select>` + JS 강화"를 쓰지 않은 이유:

- 그 패턴의 고전적 이득은 점진적 향상(JS가 실패해도 컨트롤이 동작)인데, 이
  화면 자체가 이미 JS에 의존하므로 여기선 의미가 없습니다.
- 데이터 계층 비용은 작습니다. 네이티브 select를 모델(단일 소스)로 두고
  커스텀을 뷰/컨트롤러로 두면 양방향 동기화가 필요 없습니다.
- 접근성 비용은 "교체"에 특정되지 않습니다. 네이티브를 숨기고 *커스텀*
  컨트롤을 만드는 순간, 키보드·스크린리더 동작은 커스텀의 몫이 됩니다. 즉
  비용은 "교체"가 아니라 "커스텀"에 속합니다.

현재 지원: ARIA listbox 역할/상태와 키보드 조작(방향키, Home/End, Enter/Space,
Escape, Tab, 바깥 클릭, typeahead). 네이티브 select 완전 재현은 아님(예: 모바일
OS 피커 없음).

스크린리더 도달성이 필요해지면, 가장 값싼 방법은 시각적으로 숨긴 네이티브
`<select>`를 모델로 두고 보조기술에 노출하는 것(커스텀 뷰는 `aria-hidden`)과
"네이티브 컨트롤 사용" 선호를 명시적으로 제공하는 것입니다. 스크린리더 자동
감지는 신뢰할 수 없으므로 opt-in이 정직한 선택입니다. 폰 사용을 고려한 단순
노출 정책은 `pointer:fine`은 커스텀, `pointer:coarse`는 네이티브로 미디어
쿼리로 전환해, 탭 순서와 접근성 트리에 항상 하나만 존재하게 하는 것입니다.

**커스텀 컨트롤도 접근성을 만족해야 하며, 아직 검증되지 않았습니다.** 위에서
설명한 ARIA 패턴은 따르고 있지만, 키보드·스크린리더·고대비 등 전용 접근성
점검이 아직 필요하며, 그전까지는 미검증 상태로 봅니다.

## 데이터와 CLI

기본 데이터 위치는 `~/.devhard`입니다. 다른 위치를 쓰려면 모든 명령에서 하위 명령 앞에
같은 `--data-dir`을 지정하세요. 이름은 영문 소문자·숫자·밑줄·하이픈을 사용합니다.

```sh
mise run devhard -- --data-dir .devhard workspace add dev-a --ssh-host dev-a --tab http://127.0.0.1:3000
mise run devhard -- --data-dir .devhard workspace list
mise run devhard -- --data-dir .devhard workspace show dev-a
mise run devhard -- --data-dir .devhard manager run --open
# 다른 터미널에서:
mise run devhard -- --data-dir .devhard manager status
```

동일한 물리 데이터 디렉터리에 두 번째 Manager를 실행하면 exit 73으로 거부합니다.
CLI 변경 명령도 같은 잠금을 사용하므로 실행 중인 Manager를 먼저 종료해야 합니다.
실행 중에는 UI/API로 제어하고, CLI의 조회 명령은 사용할 수 있습니다.

```sh
mise run devhard -- --data-dir .devhard workspace start-all dev-a
mise run devhard -- --data-dir .devhard tunnel status dev-a
mise run devhard -- --data-dir .devhard tunnel restart dev-a
mise run devhard -- --data-dir .devhard browser stop dev-a
mise run devhard -- --data-dir .devhard browser start dev-a
mise run devhard -- --data-dir .devhard browser restart dev-a
mise run devhard -- --data-dir .devhard workspace stop-all dev-a
```

`start-all`은 SSH부터 시작하며 Chrome 실패 시 `result: partial`과 오류·현재 상태를 출력하고
exit 1로 종료합니다. 성공은 `success`입니다. 개별 종료·재시작, 인증 실패, Manager 재시작에서
예약을 유지합니다. 명시적인 전체 종료가 두 프로세스 종료를 확인한 뒤 예약을 해제합니다.

## SSH 준비와 프로세스 소유권

일반 터미널에서 SSH 키/ssh-agent와 최초 host key 확인을 먼저 준비하세요. DevHard는
비대화형 인증을 사용하며 host key 검사를 끄지 않습니다. SSH 연결 공유도 사용하지 않습니다.

분리 실행된 SSH/Chrome은 Manager 종료 후에도 유지됩니다. 제어 전 OS 시작 시각, 실행 파일,
전체 명령, 고유 실행 표식을 확인합니다. macOS는 마이크로초 시작 시각, Linux는 boot ID와
start ticks를 사용합니다. 확인하지 못한 프로세스나 프로세스 그룹을 임의로 종료하지 않습니다.
Chrome의 외부 프로필 사용과 잠금도 검사하며 잠금 파일을 삭제하지 않습니다. macOS에서
정상 종료가 거부되면 강제 종료로 대체하지 않습니다.

SOCKS 기본 예약 범위는 **60000–60999**입니다. 충돌한 기존 예약을 임의 변경하지 않으며,
최초 포트 바인딩 경쟁만 최대 세 후보로 재시도합니다. 터널의 `listening`은 프로세스·소유한
리스너·SOCKS 응답을 확인한 상태이며 원격 앱 식별 검증은 아닙니다. `identity_verified`는 false입니다.

| 오류 코드 | 확인할 내용 |
| --- | --- |
| `authentication_failed` | 키/ssh-agent 준비와 일반 터미널 SSH 접속 |
| `host_key_verification`, `host_key_changed` | 터미널에서 호스트 키 확인. 검사 우회 금지 |
| `dns_failed`, `connection_failed`, `connection_timeout` | SSH 별칭, DNS/VPN, 서버 가용성 |
| `port_conflict`, `port_range_exhausted` | 점유 포트·예약 해제 또는 설정 범위 조정 |
| `ownership_unknown`, `inspection_failed` | 저장 기록과 실제 OS 프로세스 확인 |
| `launch_uncertain` | 중단된 실행 journal과 일치하는 프로세스 확인 |

시작 전 `tunnel-launch.json` 또는 `browser-launch.json`을 기록하여 Manager 중단 후 복구합니다.
유일한 프로세스를 검증하지 못하면 journal을 남기고 중복 실행을 막습니다. 해당 프로세스가
종료됐는지 확인하기 전 journal을 지우지 마세요. 활성 Manager 잠금 파일도 삭제하지 마세요.
로그는 append 방식이며 회전은 배포 준비 단계에서 다룹니다.

## 설정 구조

```text
~/.devhard/
├── config.toml
├── runtime/
│   ├── manager.lock
│   └── manager.json
└── workspaces/dev-a/
    ├── config.toml
    ├── chrome-profile/
    └── runtime/state.json
```

잠금의 실제 유효성은 OS lock으로 판단합니다. 강제 종료 뒤 manager.json은 오래된 정보일 수 있습니다.
설정은 버전 있는 TOML, runtime은 JSON입니다. 관리 하위 경로의 symlink는 허용하지 않으며,
명시한 데이터 루트 symlink는 실제 경로로 해석해 같은 잠금을 사용합니다.

전역 설정:

```toml
schema_version = 1
[ports]
min = 60000
max = 60999
```

워크스페이스 설정:

```toml
schema_version = 1
name = "dev-a"
[ssh]
host = "dev-a"
[browser]
profile = "chrome-profile"
executable = "auto"
restore_session = true

[[bookmarks]]
name = "개발 앱"
url = "http://127.0.0.1:3000"
description = "주 개발 화면"
initial = true

[[bookmarks]]
name = "API 문서"
url = "http://127.0.0.1:6767"
description = "필요할 때 열기"
initial = false
```

기존 `tabs = ["http://..."]`는 URL을 이름으로 가진 Initial 북마크로 읽습니다. 저장할 때는
bookmarks 형식으로 기록합니다. 두 필드를 동시에 지정할 수 없습니다. 빈 목록은 `bookmarks = []`입니다.

전역 `[ssh] config_file = "/절대/경로"`로 별도 OpenSSH 설정을 선택할 수 있습니다.
Chrome 실행 파일은 auto 또는 네이티브 실행 파일의 절대 경로이며 `.app` 디렉터리나 래퍼는
지원하지 않습니다. 일반 실행에는 CDP를 켜지 않습니다. 설정·runtime 손상이나 중복 예약은
시작을 차단하며 기록을 지우지 않습니다. 세션 내용·손상 복구는 Chrome이 담당하며 탭 백업은 하지 않습니다.

## 로컬 API

모든 API는 `Authorization: Bearer TOKEN`을 요구합니다. 같은 origin의 HTML이 실행별 새 토큰을
전달하며 URL·cookie·runtime에 저장하지 않습니다. 정확한 Host와 제공된 Origin을 검사합니다.
변경 요청에는 같은 Origin과 JSON을 요구하며 본문 제한은 16 KiB입니다. CORS 허용과
proxy-header 신뢰는 사용하지 않습니다. no-store/CSP/frame 제한도 적용합니다.
신뢰하는 로컬 프로그램은 HTML을 읽을 수 있습니다. 원격 배포용 서비스가 아닙니다.

| 메서드 | 경로 | 동작 |
| --- | --- | --- |
| GET | `/api/state` | Manager·워크스페이스 관측·최근 작업 |
| GET | `/api/events` | Bearer 인증 SSE 상태 스트림 |
| GET | `/api/jobs/{id}` | 작업 상태와 결과 |
| POST | `/api/workspaces` | 버전 1 설정으로 등록 |
| PUT | `/api/workspaces/{name}` | 북마크는 실행 중 변경, 연결·브라우저 변경은 종료 후 |
| POST | `/api/workspaces/stop_all` | 모든 워크스페이스의 브라우저와 SSH 종료 |
| POST | `/api/workspaces/{name}/workspace/{start_all,stop_all}` | 전체 제어 |
| POST | `/api/workspaces/{name}/{tunnel,browser}/{start,stop,restart}` | 개별 제어 |
| POST | `/api/workspaces/{name}/bookmarks/open` | 전체 `{}` 또는 개별 `{"index": 0}` 북마크 열기 |

프로세스 제어 명령 본문은 `{}`입니다. 북마크 index는 현재 저장 목록의 위치이며, 저장된
HTTP(S) URL만 열 수 있습니다. 임의 URL을 요청에 넣을 수 없습니다. 변경 요청의 HTTP 202와 Location은 접수됐다는 뜻이며
실행 성공은 아닙니다. succeeded/partial/failed/cancelled까지 조회하세요.
잘못된 요청은 400/413/415, 접근 거부는 401/403, 가득 찬 큐는 429로 응답합니다.
실행 중 오류는 접수된 작업의 결과에 기록됩니다.

## Docker 테스트 환경과 검증

[SSH/web fixture](tests/fixtures/ssh-web/README.md)는 이미지 하나로 SSH와 loopback HTTP 앱
3000·6767을 제공하고 서버 ID가 다른 컨테이너 두 개를 띄웁니다. Docker context를 사용하므로
Colima에 종속되지 않습니다. 생략하면 현재 context를 사용합니다.

```sh
mise run fixture -- up --context colima-devhard-tests
mise run fixture -- check
mise run fixture -- probe --chrome
mise exec -- python experiments/api_probe.py --chrome
mise run fixture -- down
```

생성한 키와 고정한 host key는 별도 테스트 설정에 보관하며 사용자 SSH 파일을 수정하지 않습니다.
API probe는 독립 Manager에서 동시 요청·정상/강제 종료·재발견·토큰 교체·부분 성공·정리를
검사하고 `.m4-runs/`에 증거를 남깁니다.

기존 단계별 실험도 사용할 수 있습니다.

```sh
mise exec -- python experiments/lifecycle_probe.py --chrome
mise exec -- python experiments/tunnel_probe.py --ssh-host dev-a \
  --http-url http://127.0.0.1:3000 --http-url http://127.0.0.1:6767
mise exec -- python experiments/browser_probe.py --ssh-host dev-a --second-ssh-host dev-b \
  --http-url http://127.0.0.1:3000 --http-url http://127.0.0.1:6767
```

각 실험은 `.m0-runs/`, `.m2-runs/`, `.m3-runs/`의 독립 데이터와 실제 프로세스를 사용합니다.
브라우저 probe에서만 CDP를 켜 실제 페이지·세션을 검사합니다. 원격 파일은 만들지 않습니다.
실험 도구는 공개 CLI가 아니며 Linux·실제 앱 WebSocket/HMR 검증과 배포 준비는 M5 범위입니다.

## 북마크 검증

```sh
mise exec -- python experiments/bookmark_probe.py
```

독립 실제 Chrome에서 최초 실행, 개별·전체 열기, 기존 탭 활성화, 없는 URL만 추가,
선택자 특수문자와 한글 URL, PID·예약 유지, 실행 중 편집,
복원을 끈 이후 실행의 빈 탭, Chrome 종료 경쟁 시 대체 프로세스 정리를 검사합니다. 탭 조회용
CDP는 검증 프로필에서만 사용합니다. 실제 제품 실행에서는 사용하지 않습니다.

관리 UI는 Bearer 인증 fetch 스트림으로 SSE를 수신합니다. 연결 시 전체 상태를 받고,
상태·작업 변경 시 최신 snapshot을 받습니다. 15초 heartbeat와 자동 재연결을 사용하며
Manager 재시작으로 토큰이 바뀌면 화면을 새로고침해야 합니다. 서버 내부의 프로세스 관측은
계속 주기적으로 실행됩니다. `/api/state`는 수동 조회와 호환성을 위해 유지합니다.
