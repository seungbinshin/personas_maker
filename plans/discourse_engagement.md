# Discourse Engagement Pipeline

## 목적

연구 리포트를 Discourse에 게재하고, 댓글에 대해 자동으로 조사/검증/답변하는 독립 파이프라인.

## 아키텍처 원칙

- 기존 `research_pipeline.py`에 **직접 코드를 추가하지 않는다**
- 연결점은 `research_pipeline` → `discourse_publisher`로의 **단방향 호출** 1개뿐
- 댓글 응답 파이프라인은 완전히 독립된 모듈로 동작

## 시스템 구성

```
┌─────────────────────────────────────────────────────┐
│  research_pipeline.py (기존)                          │
│  ... → run_reports() → run_batch_review()            │
│         │                                            │
│         └──→ discourse_publisher.publish_report()     │
│              (topic_id를 report metadata에 저장)      │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  discourse_engagement.py (신규, 독립 모듈)            │
│                                                      │
│  BotScheduler (2분 간격)                              │
│    └──→ DiscourseEngagement.poll_and_respond()       │
│          │                                           │
│          ├─ 1. 새 댓글 감지                           │
│          ├─ 2. 댓글 분류 (LLM)                        │
│          │     → skip / question / correction         │
│          ├─ 3. 정보 탐색 (Searcher)                   │
│          │     → Confluence + Discourse + Web         │
│          ├─ 4. 답변 초안 작성 (Drafter)                │
│          ├─ 5. 팩트 체크 (FactChecker)                │
│          │     → approve / revise                    │
│          └─ 6. 게시 (Publisher)                       │
│                → reply_to_post_number 지정            │
└─────────────────────────────────────────────────────┘
```

## 공유 인프라 (변경 없이 재사용)

- `discourse_client.py` — 쓰기 메서드 추가 (create_topic, create_reply)
- `confluence_knowledge.py` — build_context()
- `discourse_knowledge.py` — build_context()
- `tools/claude_runtime.py` — ClaudeRuntimeClient (claude-code-api)

## 파일 구조

```
src/
  discourse_publisher.py    # 리포트 → Discourse 토픽 게재
  discourse_engagement.py   # 댓글 모니터링 + 응답 파이프라인
  discourse_client.py       # 기존 + 쓰기 메서드 추가
```

## 데이터 흐름

### Phase 1: 리포트 게재

```
research_pipeline.run_reports()
  → report_v1.md 생성
  → discourse_publisher.publish_report(report_id)
    → markdown → Discourse POST /posts (category, tags)
    → topic_id를 state.json metadata에 저장
    → Slack에 Discourse 링크 공유
```

### Phase 2: 댓글 응답 (2분 간격 폴링)

```
poll_and_respond():
  for each published_topic_id:
    1. DETECT: GET /t/{id}.json → 새 post 확인 (last_checked_post_id 이후)
    2. CLASSIFY: 각 새 댓글에 대해 LLM 분류
       - "discussion": 사람끼리 대화 → skip
       - "praise": 감사/동의 → skip
       - "question": 질문 → respond
       - "correction": 지적/오류 → respond
    3. SEARCH: (question/correction만)
       - 댓글에서 키워드 추출
       - Confluence 검색 (build_context)
       - Discourse 검색 (build_context)
       - claude-code-api WebSearch (외부 문헌)
    4. DRAFT: 수집된 자료 + 댓글 내용 → 답변 초안
    5. FACT_CHECK: 팩트 체크 봇이 초안 검증
       - approve → 게시
       - revise → 추가 조사 후 재작성 (최대 2회)
       - reject → 답변하지 않음 (Slack에 알림만)
    6. PUBLISH: POST /posts (topic_id, reply_to_post_number)
       → 해당 댓글에 직접 답글
```

## 댓글 분류 기준

| 유형 | 판단 기준 | 행동 |
|------|----------|------|
| discussion | 다른 사용자에게 reply하면서 봇/리포트를 언급하지 않음 | skip |
| praise | "좋네요", "감사합니다", 이모지 반응 | skip |
| question | 물음표, "어떻게", "왜", 기술적 질의 | respond |
| correction | "틀린 것 같", "실제로는", "확인해보니", 반박 근거 제시 | respond |

## 팩트 체크 기준

| 판정 | 기준 | 행동 |
|------|------|------|
| approve | 출처 명확, 내부 문서와 일치, 논리 일관 | 게시 |
| revise | 일부 근거 부족, 추가 검증 필요 | 재조사 후 재작성 |
| reject | 확신할 수 없는 내용, 오답 위험 | 답변 보류 + Slack 알림 |

## 상태 관리

```json
// state.json metadata에 추가
{
  "discourse_topic_id": 123,
  "discourse_topic_url": "https://hyperaccel.discourse.group/t/...",
  "last_checked_post_number": 5
}
```

## 구현 순서

1. `discourse_client.py` — 쓰기 메서드 추가
2. `discourse_publisher.py` — 리포트 게재
3. `discourse_engagement.py` — 댓글 모니터링 + 분류 + 응답
4. `research_pipeline.py` — publish 호출 1줄 추가
5. `bot.py` — 스케줄러에 engagement polling 등록
