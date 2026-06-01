# claude-code-api Skillification — 결정 사항 기록

**작성일**: 2026-04-29
**상태**: Brainstorming 진행 중 (5개 결정 확정, 미해결 항목 있음)

## 배경 / 동기

현재 claude-code-api 운영에서 반복되는 문제:

1. **persona/ 폴더에 통째로 묶여 있음** — claude-code-api가 conceptually 독립적인데 persona git에 vendored됨. 다른 머신/프로젝트에서 재사용 어려움.
2. **포트 충돌 확인 불가** — `.env`의 `PORT=8080`이지만 launchd가 띄운 새 인스턴스가 8082에서 도는데 옛 좀비 프로세스가 8080 점유 중. 클라이언트(`bot.py`)는 옛 포트 때림 → reporter 등 실패.
3. **Claude 세션이 claude-code-api 기능 존재 자체를 망각** — 세션마다 새로 발견해야 함. 표준화된 진입점 부재.

이를 해결하기 위해 claude-code-api를 **글로벌 스킬화** + **자동 포트/디스커버리/좀비 회피** 구조로 재설계.

## 확정된 결정

### Q1. 코드 위치 & 분리
- **GitHub private 레포로 publish 후 `~/.local/share/claude-code-api/`로 clone** (B 단계).
- 이미 publish-ready: `.env.example`, `.gitignore`, `LICENSE`, `README.md`, `README-KR.md`, `.github/workflows/` 모두 갖춰져 있음.
- persona의 git에는 단일 `Initial commit` (068000d)에만 박혀있어서 새 레포 init이 자연스러움.
- **publish 전 점검**: `.env`가 .gitignore에 포함됐는지 검증, 시크릿 노출 방지.

### Q2. persona 정리 시점
- **검증 후 cutover** (B 옵션 선택).
- Publish + clone까지만 먼저, persona의 사본은 일단 유지 → 새 위치에서 4가지 기능 구현/테스트 → 안정 확인되면 그때 persona 사본 제거 + launchd plist 전환.
- 회귀 발생 시 launchd만 옛 위치로 되돌리면 즉시 원상복구 가능.

### Q3. 워커 cwd 격리 모델
- **단일 서버 + 요청별 cwd 파라미터** (A 옵션 선택).
- 서버 1개만 떠 있고, 클라이언트가 `/run` body에 `cwd` 넣어 보내면 워커가 그 cwd로 claude CLI spawn.
- 워커 풀은 가장 자주 쓰는 cwd 기준 1개 warm + 나머지 on-demand.
- 레지스트리/디스커버리 단순화 (포트 1개, 헬스체크 1번).
- 향후 멀티-cwd 부하 관찰 후 필요해지면 cwd별 서브풀(Option C)로 마이그레이션 가능.

### Q4. 디스커버리 + 포트 할당
- **레지스트리 파일 + OS 자동 포트** (A 옵션 선택).
- 서버가 `PORT=0`으로 떠서 OS가 할당한 포트를 `~/.config/claude-code-api/registry.json`에 기록:
  ```json
  {"port": 51234, "pid": 12345, "started_at": "2026-04-29T...", "version": "0.2.0"}
  ```
- 클라이언트는 registry 읽어서 url 구성. 포트 충돌 자체 발생 불가.
- **좀비 회피 프로토콜**:
  - 서버 시작 시 레지스트리의 기존 PID에 `kill -0` 보냄
  - 죽었으면 stale 엔트리 제거 후 본인 등록
  - 살아있고 같은 버전이면 본인은 종료 (중복 방지)
  - 살아있는데 다른 버전이면 명시적 에러 → 사용자 개입 요청
- launchd `KeepAlive=true` 환경에서도 좀비/중복 자동 회피.
- **클라이언트 fallback**: registry 못 읽으면 환경변수 `CLAUDE_CODE_API_URL` 사용.

### Q5. 스킬 호출 시 동작 모델
- **하이브리드: 의미 문서 + 얇은 ccapi CLI** (C 옵션 선택).
- 스킬 본문은 API 모델/auth/엔드포인트를 명시적으로 설명 + 작은 `ccapi` CLI 제공.
- Claude는 의미론적으로 이해한 채로 운영 동작은 CLI에 위임. 디버깅 시 raw curl로도 갈 수 있음.
- **`ccapi` CLI 서브커맨드**:
  - `ccapi ensure` — registry 읽고 → 살아있으면 noop, 죽었으면 launchd 통해 띄우고 health 통과까지 대기
  - `ccapi url` — registry의 base URL 한 줄 출력
  - `ccapi tenants` — 등록된 tenant 목록
  - `ccapi logs --tail` — launchd 로그 tail
  - `ccapi run --tenant X --prompt "..."` — 편의 래퍼 (curl 대신)
- **스킬 description**: "Claude Code session이 외부 LLM 호출/장기 작업/타 디렉토리 컨텍스트가 필요할 때 사용. claude-code-api 서버를 보장하고 호출함" — Claude 망각 방지를 위해 trigger 강하게 작성.

## 미해결 항목 (다음 세션에서 결정)

### Q6. 글로벌 설치 / API 키 저장 / launchd 모델
세 후보 중 사용자 응답 대기:

- **A. 명시적 install 단계** (현재 권장):
  - `ccapi install` 1회 실행 → `~/.config/claude-code-api/.env` (mode 0600) + `~/Library/LaunchAgents/com.claudecodeapi.plist` 생성 + `launchctl load`
  - 이후 `ccapi ensure`는 상태 확인/재기동만
  - 스킬은 install 안 됐으면 명확한 에러 메시지로 안내
  - 키 위치: `~/.config/claude-code-api/.env` (XDG-compliant, 0600)
  - tenant 8개(secondme, research, coder, reporter, hynixprec, factchecker, opsidian-graph, multi-interview) 그대로 이전
  - 보조 커맨드: `ccapi tenants add <name> <key>`, `ccapi tenants ls`

- **B. 자동 install** — `ccapi ensure` 첫 호출 시 plist/키 자동 생성 (Claude가 silent 권한 동작).

- **C. launchd 안 씀** — `ccapi serve --daemon` 으로 수동 띄움. 재부팅 자동시작 X.

### Q7. 기존 클라이언트 마이그레이션
- 영향 받는 곳: `src/bot.py:55` (`CLAUDE_API_URL=http://localhost:8080`), `src/trigger_discourse_poll.py:32` (`localhost:8083`), `src/slack_bot.py:42`.
- 옵션 후보: registry-aware 헬퍼 함수 도입 / `CLAUDE_CODE_API_URL` 환경변수 일괄 / 양쪽 다 지원.

### 추가 검토 필요
- 스킬 위치: `~/.claude/skills/claude-code-api/` vs Claude Code plugin 형태?
- SessionStart hook 추가 여부 (Claude 망각 방지 보강)
- 로그 위치: `~/Library/Logs/claude-code-api/` (macOS 표준)
- 버전 관리: 새 레포의 첫 버전 태그 / SemVer 정책

## 다음 세션 진입점

1. Q6 결정 받기 (install 모델)
2. Q7 결정 받기 (클라이언트 마이그레이션)
3. 추가 검토 항목 일괄 처리
4. 디자인 문서로 전환: `docs/superpowers/specs/2026-04-29-claude-code-api-skillification-design.md`
5. Self-review + 사용자 리뷰
6. `superpowers:writing-plans` 스킬로 구현 계획 수립
