"""Tests for the generic convergence guard (anti self-replication, phase 2).

The 2026-06-02 fixes killed verbatim repetition; the attractor then moved one
abstraction level up — opener lock-in (흠 X / …), topic perseveration (멤도,
류희왕 dragged across 10+ turns), and a fixed 2-line skeleton. Hardcoded token
bans only relocate the attractor (banning ㅋ-openers produced the 흠 lock), so
this guard is CONTENT-AGNOSTIC: it detects whatever pattern the bot's own
recent output converged on and feeds that back as a dynamic instruction,
masks the flagged tokens out of the rendered context (removes induction fuel),
and enforces with one regeneration only after the soft nudge was ignored.

All detector tests use synthetic tokens (쩝, 갑자기, 보라돌이…) to prove there
is no hardcoded token list.
"""

from __future__ import annotations

import os

# bot.py reads SLACK_USER_ID at import; provide a dummy so it imports in tests.
os.environ.setdefault("SLACK_USER_ID", "UTEST")

from skills.conversation.session_orchestrator import (
    ConversationSessionOrchestrator as Orch,
)


def _turns(*texts: str) -> list[str]:
    return list(texts)


# ─── detect_convergence: OPENER attractor ───────────────────────────


def test_opener_flagged_at_4_of_5():
    bot = _turns(
        "쩝 그건 아니지\n둘째 줄",
        "쩝 또 그러네\n다른 말",
        "오 새로운 거네",
        "쩝 별로다\n흠흠",
        "쩝 마지막이다\n끝",
    )
    report = Orch.detect_convergence(bot, ["새 질문이야"])
    assert report["opener"] == "쩝"
    assert report["opener_count"] == 4


def test_opener_not_flagged_at_3_of_5_signature_flavor_allowed():
    # 3/5 is normal persona flavor (흠/헉 are real signatures) — must NOT flag.
    bot = _turns(
        "쩝 하나\n둘", "쩝 둘\n셋", "오 셋", "쩝 넷\n다섯", "야 다섯",
    )
    report = Orch.detect_convergence(bot, ["새 질문"])
    assert report["opener"] is None


def test_opener_needs_min_window():
    # 3 turns all same opener — too few turns to call it a lock-in.
    bot = _turns("쩝 하나", "쩝 둘", "쩝 셋")
    report = Orch.detect_convergence(bot, ["질문"])
    assert report["opener"] is None


def test_opener_collapses_character_runs():
    # ㅋㅋㅋ / ㅋㅋㅋㅋㅋ variants count as the same opener (generic run-collapse).
    bot = _turns(
        "ㅋㅋㅋ 웃기네", "ㅋㅋㅋㅋㅋ 또 웃기네", "ㅋㅋ 진짜", "ㅋㅋㅋㅋ 아 배야", "오 그건 좀",
    )
    report = Orch.detect_convergence(bot, ["새 질문"])
    assert report["opener"] == "ㅋ"
    assert report["opener_count"] == 4


def test_closer_flagged_at_4_of_5():
    bot = _turns(
        "그건 아니지 쩝", "새로운 말도 했다 쩝", "전혀 다른 말", "또 끝났네 쩝", "마지막 쩝",
    )
    report = Orch.detect_convergence(bot, ["새 질문"])
    assert report["closer"] == "쩝"


# ─── detect_convergence: TOPIC perseveration ─────────────────────────


def _topic_locked_bot_turns() -> list[str]:
    # Bot drags 보라돌이 (with Korean particle variants) across 5 of 8 turns.
    return _turns(
        "오 그건 몰랐네",
        "보라돌이 얘기 또 하네",
        "근데 보라돌이가 문제라니까",
        "보라돌이는 진짜 아니다",
        "딴 얘기 좀 하자",
        "그래도 보라돌이랑 비교하면 약하지",
        "어제 본 영화는 별로였어",
        "결국 보라돌이 얘기로 돌아오네",
    )


def test_topic_flagged_when_bot_only_and_bursty():
    humans = ["영화 봤어?", "딴 얘기 하자니까", "요즘 뭐 듣냐"]
    report = Orch.detect_convergence(_topic_locked_bot_turns(), humans)
    assert "보라돌이" in report["topics"]  # particle variants grouped to one stem


def test_topic_not_flagged_when_human_engaged():
    # Humans keep feeding the topic — it's a live collaborative gag, not a loop.
    humans = [
        "보라돌이 ㅋㅋㅋ", "보라돌이 또 나왔네", "보라돌이 짤 보내줘", "보라돌이 최고다",
    ]
    report = Orch.detect_convergence(_topic_locked_bot_turns(), humans)
    assert "보라돌이" not in report["topics"]


def test_topic_not_flagged_when_in_latest_human_message():
    # The human just brought it up — responding to it is legitimate.
    humans = ["딴 얘기 하자", "보라돌이는 어떻게 생각해?"]
    report = Orch.detect_convergence(_topic_locked_bot_turns(), humans)
    assert "보라돌이" not in report["topics"]


def test_topic_not_flagged_when_constant_across_windows():
    # A token the bot uses at the same rate in the PRIOR window too is its
    # normal discourse glue (근데/그래서…), not a bursty topic attractor.
    prior = ["근데 옛날 얘기 하나", "근데 옛날 얘기 둘", "근데 셋", "근데 넷",
             "근데 다섯", "근데 여섯", "근데 일곱", "근데 여덟"]
    recent = ["근데 하나", "근데 둘", "근데 셋이야", "근데 넷이지",
              "딴 말", "근데 다시", "또 딴 말", "근데 끝"]
    report = Orch.detect_convergence(prior + recent, ["새 질문이야"])
    assert report["topics"] == []


def test_topic_needs_min_turns():
    report = Orch.detect_convergence(["보라돌이 하나", "보라돌이 둘", "보라돌이 셋"], ["질문"])
    assert report["topics"] == []


# ─── detect_convergence: STRUCTURE attractor ─────────────────────────


def test_structure_flagged_when_line_count_locked():
    bot = _turns("하나\n둘", "셋\n넷", "다섯\n여섯", "일곱\n여덟")
    report = Orch.detect_convergence(bot, ["질문"])
    assert report["line_count"] == 2


def test_structure_not_flagged_when_varied():
    bot = _turns("하나\n둘", "셋", "다섯\n여섯\n일곱", "여덟\n아홉")
    report = Orch.detect_convergence(bot, ["질문"])
    assert report["line_count"] is None


def test_no_flags_on_healthy_conversation():
    bot = _turns(
        "오 그거 좋네", "ㅋㅋㅋㅋ 미쳤다", "근데 좀 비싸지 않냐\n나라면 안 삼", "헉 진짜?",
        "그건 좀 무리수 같은데", "야 저녁 뭐 먹지\n배고프다\n진짜로",
    )
    report = Orch.detect_convergence(bot, ["저녁 뭐 먹을래"])
    assert report["opener"] is None
    assert report["closer"] is None
    assert report["topics"] == []
    assert report["line_count"] is None


# ─── render_convergence_note ─────────────────────────────────────────


def test_render_note_names_detected_patterns():
    bot = _turns(
        "쩝 하나\n둘", "쩝 둘\n셋", "쩝 셋\n넷", "쩝 넷\n다섯", "쩝 다섯\n여섯",
    )
    report = Orch.detect_convergence(bot, ["새 질문"])
    note = Orch.render_convergence_note(report)
    assert "쩝" in note  # names the actual detected token, not a hardcoded one
    assert "시작" in note
    assert "2줄" in note


def test_render_note_empty_when_no_flags():
    report = Orch.detect_convergence(["오 좋네"], ["질문"])
    assert Orch.render_convergence_note(report) == ""


def test_render_note_escalates_on_streak():
    bot = _turns("쩝 하나", "쩝 둘", "쩝 셋", "쩝 넷", "쩝 다섯")
    report = Orch.detect_convergence(bot, ["질문"])
    soft = Orch.render_convergence_note(report, streak=1)
    hard = Orch.render_convergence_note(report, streak=2)
    assert soft != hard  # second consecutive flag gets a sharper note


# ─── convergence_violations / strip_flagged_opener ───────────────────


def _flagged_report() -> dict:
    bot = _turns(
        "쩝 보라돌이 하나\n둘", "쩝 보라돌이 둘\n셋", "쩝 보라돌이가 셋\n넷",
        "쩝 보라돌이는 넷\n다섯", "쩝 다섯\n보라돌이 여섯",
    )
    return Orch.detect_convergence(bot, ["전혀 새로운 질문"])


def test_violations_detect_flagged_opener_and_topic():
    report = _flagged_report()
    assert Orch.convergence_violations("쩝 새로운 말이야", report)
    assert Orch.convergence_violations("아니 보라돌이가 왜 나와", report)
    assert Orch.convergence_violations("완전히 새로운 반응이다", report) == []


def test_strip_flagged_opener_removes_short_interjection():
    report = _flagged_report()
    assert Orch.strip_flagged_opener("쩝 그건 아니지\n둘째 줄", report) == "그건 아니지\n둘째 줄"


def test_strip_flagged_opener_keeps_nonviolating_text():
    report = _flagged_report()
    assert Orch.strip_flagged_opener("오 새로운 말", report) == "오 새로운 말"


def test_strip_flagged_opener_never_returns_empty():
    report = _flagged_report()
    assert Orch.strip_flagged_opener("쩝", report) == "쩝"


# ─── classify_question: 졌 boundary fix ───────────────────────────────


def test_classify_off_topic_passive_verbs_as_chat():
    # -아/어졌- conjugations must not trigger the sports keyword.
    assert Orch.classify_question("로컬옵티마에 빠졌니 승빈아") == "chat"
    assert Orch.classify_question("너 좋아졌어") == "chat"
    assert Orch.classify_question("噫 터졌다 그거") == "chat"


def test_classify_real_score_questions_as_research():
    assert Orch.classify_question("한화 졌어?") == "research"
    assert Orch.classify_question("어제 경기 이겼냐") == "research"


# ─── ChannelMemory: accessors / capped+masked rendering / ephemeral ──


def _seed(bot, cid: str, n_bot: int = 10, opener: str = "쩝"):
    bot.memory.clear_channel(cid)
    for i in range(n_bot):
        bot.memory.add_message(cid, "Harry", f"사람질문{i} 어떻게 생각해")
        bot.memory.add_message(
            cid, bot.DISPLAY_NAME, f"{opener} 봇답변{i}이다\n둘째줄{i}", is_bot=True
        )
    bot.memory.add_message(cid, "Harry", "마지막 새 질문이야")


def test_recent_bot_and_human_turn_accessors():
    import bot

    cid = "C_acc"
    _seed(bot, cid, n_bot=4)
    bot_turns = bot.memory.recent_bot_turns(cid, 8)
    human_turns = bot.memory.recent_human_turns(cid, 12)
    assert len(bot_turns) == 4
    assert bot_turns[-1] == "쩝 봇답변3이다\n둘째줄3"
    assert human_turns[-1] == "마지막 새 질문이야"
    assert all("사람질문" not in t for t in bot_turns)


def test_generation_context_caps_bot_turns_keeps_all_human():
    import bot

    cid = "C_cap2"
    _seed(bot, cid, n_bot=10)
    conv = bot.memory.get_conversation_for_generation(cid, max_bot_turns=8)
    # All 10 human messages survive; only the most recent 8 bot turns render.
    assert "사람질문0" in conv and "사람질문9" in conv
    assert "봇답변0이다" not in conv and "봇답변1이다" not in conv
    assert "봇답변2이다" in conv and "봇답변9이다" in conv


def test_generation_context_still_drops_trailing_bot_turn():
    import bot

    cid = "C_trail"
    bot.memory.clear_channel(cid)
    bot.memory.add_message(cid, "Harry", "안녕")
    bot.memory.add_message(cid, bot.DISPLAY_NAME, "오 안녕", is_bot=True)
    conv = bot.memory.get_conversation_for_generation(cid)
    assert "오 안녕" not in conv  # the self-continue edge stays cut
    assert "안녕" in conv


def test_generation_context_masks_flagged_opener_token():
    import bot

    cid = "C_mask"
    _seed(bot, cid, n_bot=6)
    conv = bot.memory.get_conversation_for_generation(cid, mask_tokens=("쩝",))
    assert "쩝" not in conv  # induction fuel removed from rendered context
    assert "봇답변5이다" in conv  # content itself stays


def test_ephemeral_interim_excluded_from_generation_context():
    import bot

    cid = "C_eph"
    bot.memory.clear_channel(cid)
    bot.memory.add_message(cid, "Harry", "비트코인 얼마야")
    bot.memory.add_message(cid, bot.DISPLAY_NAME, "잠시만 검색좀 ㄱㄱ", is_bot=True, ephemeral=True)
    bot.memory.add_message(cid, "Harry", "빨리")
    conv = bot.memory.get_conversation_for_generation(cid)
    assert "잠시만 검색좀" not in conv  # filler never becomes a persona example
    assert bot.memory.recent_bot_turns(cid, 8) == []


def test_human_message_invalidates_prompt_cache():
    import bot

    cid = "C_cache"
    bot.memory.clear_channel(cid)
    bot.memory.cache_prompt(cid, "stale prompt")
    bot.memory.add_message(cid, "Harry", "완전 새로운 화제")
    assert bot.memory.get_cached_prompt(cid) is None


# ─── generate_chat_response integration ──────────────────────────────


class _FakeAPI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def __call__(self, prompt, timeout_ms, session_id=None):
        self.prompts.append(prompt)
        return self.responses.pop(0) if self.responses else ""


def test_chat_prompt_has_no_hardcoded_token_ban(monkeypatch):
    import bot

    fake = _FakeAPI([""])
    monkeypatch.setattr(bot, "_call_api", fake)
    cid = "C_noban"
    bot.memory.clear_channel(cid)
    bot.memory.add_message(cid, "Harry", "뭐해")
    bot.generate_chat_response(cid, "casual")
    p = fake.prompts[0]
    assert "ㅋ로 시작하거나" not in p  # hardcoded token ban removed (whack-a-mole)
    assert "다양하게" in p  # generic diversity guidance stays


def test_chat_prompt_injects_dynamic_note_when_locked(monkeypatch):
    import bot

    fake = _FakeAPI(["완전 새로운 반응"])
    monkeypatch.setattr(bot, "_call_api", fake)
    cid = "C_inject"
    _seed(bot, cid, n_bot=6)
    bot.state.convergence_streaks.pop(cid, None)
    bot.generate_chat_response(cid, "casual")
    p = fake.prompts[0]
    assert "[반복" in p  # dynamic corrective block present
    assert "쩝" in p.split("---")[-1]  # names the live detected opener
    # masked context: rendered bot turns no longer carry the flagged opener
    assert "[승빈] 쩝" not in p


def test_chat_prompt_no_note_on_healthy_channel(monkeypatch):
    import bot

    fake = _FakeAPI([""])
    monkeypatch.setattr(bot, "_call_api", fake)
    cid = "C_healthy"
    bot.memory.clear_channel(cid)
    bot.memory.add_message(cid, "Harry", "뭐해")
    bot.generate_chat_response(cid, "casual")
    assert "[반복" not in fake.prompts[0]  # zero noise when not converged


def test_no_retry_on_first_flag(monkeypatch):
    import bot

    fake = _FakeAPI(["쩝 또 같은 시작이다"])  # violates, but soft phase
    monkeypatch.setattr(bot, "_call_api", fake)
    cid = "C_soft"
    _seed(bot, cid, n_bot=6)
    bot.state.convergence_streaks.pop(cid, None)
    response = bot.generate_chat_response(cid, "casual")
    assert len(fake.prompts) == 1  # hysteresis: no enforcement on first flag
    assert response == "쩝 또 같은 시작이다"


def test_retry_once_on_persistent_violation(monkeypatch):
    import bot

    fake = _FakeAPI(["쩝 또 같은 시작이다", "이번엔 완전 다르게 간다"])
    monkeypatch.setattr(bot, "_call_api", fake)
    cid = "C_retry"
    _seed(bot, cid, n_bot=6)
    bot.state.convergence_streaks[cid] = 1  # flagged last turn too
    response = bot.generate_chat_response(cid, "casual")
    assert len(fake.prompts) == 2  # exactly one regeneration
    assert response == "이번엔 완전 다르게 간다"


def test_retry_backstop_strips_flagged_opener(monkeypatch):
    import bot

    # The model ignores the injection twice — the visible streak still breaks.
    fake = _FakeAPI(["쩝 첫 시도", "쩝 그래도 쩝으로 시작\n내용은 새로움"])
    monkeypatch.setattr(bot, "_call_api", fake)
    cid = "C_strip"
    _seed(bot, cid, n_bot=6)
    bot.state.convergence_streaks[cid] = 1
    response = bot.generate_chat_response(cid, "casual")
    assert not response.startswith("쩝")


def test_retry_skip_falls_back_to_silence(monkeypatch):
    import bot

    fake = _FakeAPI(["쩝 위반이다", ""])  # retry returns empty/[SKIP]
    monkeypatch.setattr(bot, "_call_api", fake)
    cid = "C_skip"
    _seed(bot, cid, n_bot=6)
    bot.state.convergence_streaks[cid] = 1
    response = bot.generate_chat_response(cid, "casual")
    assert response == ""  # silent — never reverts to the flagged original


def test_streak_resets_on_healthy_turn(monkeypatch):
    import bot

    fake = _FakeAPI([""])
    monkeypatch.setattr(bot, "_call_api", fake)
    cid = "C_reset"
    bot.memory.clear_channel(cid)
    bot.memory.add_message(cid, "Harry", "뭐해")
    bot.state.convergence_streaks[cid] = 3
    bot.generate_chat_response(cid, "casual")
    assert bot.state.convergence_streaks[cid] == 0


def test_research_prompt_also_gets_note_and_mask(monkeypatch):
    import bot

    fake = _FakeAPI(["검색 결과야"])
    monkeypatch.setattr(bot, "_call_api", fake)
    cid = "C_research"
    _seed(bot, cid, n_bot=6)
    bot.state.convergence_streaks.pop(cid, None)
    bot.generate_research_response(cid, "casual")
    p = fake.prompts[0]
    assert "[반복" in p
    assert "[승빈] 쩝" not in p


# ─── burst merge: answer ALL unanswered messages, not just the last ──


def test_unanswered_human_count_counts_trailing_humans():
    import bot

    cid = "C_tail"
    bot.memory.clear_channel(cid)
    bot.memory.add_message(cid, "Harry", "첫 질문")
    bot.memory.add_message(cid, bot.DISPLAY_NAME, "첫 답변", is_bot=True)
    bot.memory.add_message(cid, "Harry", "둘째 질문")
    bot.memory.add_message(cid, "준희", "셋째 질문")
    assert bot.memory.unanswered_human_count(cid) == 2


def test_unanswered_count_spans_ephemeral_interim():
    import bot

    cid = "C_tail_eph"
    bot.memory.clear_channel(cid)
    bot.memory.add_message(cid, "Harry", "비트코인 얼마야")
    bot.memory.add_message(cid, bot.DISPLAY_NAME, "잠시만 검색좀 ㄱㄱ", is_bot=True, ephemeral=True)
    bot.memory.add_message(cid, "Harry", "빨리 좀")
    # the interim filler is not an answer — both human messages are unanswered
    assert bot.memory.unanswered_human_count(cid) == 2


def test_chat_prompt_single_message_keeps_last_message_focus(monkeypatch):
    import bot

    fake = _FakeAPI([""])
    monkeypatch.setattr(bot, "_call_api", fake)
    cid = "C_single"
    bot.memory.clear_channel(cid)
    bot.memory.add_message(cid, "Harry", "뭐해")
    bot.generate_chat_response(cid, "casual")
    assert "마지막 메시지에" in fake.prompts[0]
    assert "한꺼번에 왔어" not in fake.prompts[0]


def test_chat_prompt_merges_concurrent_burst(monkeypatch):
    import bot

    fake = _FakeAPI([""])
    monkeypatch.setattr(bot, "_call_api", fake)
    cid = "C_burst"
    bot.memory.clear_channel(cid)
    bot.memory.add_message(cid, "Harry", "첫 질문이야")
    bot.memory.add_message(cid, bot.DISPLAY_NAME, "첫 답변", is_bot=True)
    bot.memory.add_message(cid, "Harry", "음란봇 모드는 없나")
    bot.memory.add_message(cid, "준희", "ㅋㅋㅋㅋㅋ")
    bot.generate_chat_response(cid, "casual")
    p = fake.prompts[0]
    assert "2개" in p  # tells the model how many messages are unanswered
    assert "빼먹지" in p  # …and to cover all of them in one reply
    assert "마지막 메시지에 대한 새로운 반응만" not in p


def test_research_prompt_merges_concurrent_burst(monkeypatch):
    import bot

    fake = _FakeAPI(["검색 결과"])
    monkeypatch.setattr(bot, "_call_api", fake)
    cid = "C_burst_r"
    bot.memory.clear_channel(cid)
    bot.memory.add_message(cid, "Harry", "비트코인 얼마야")
    bot.memory.add_message(cid, "준희", "이더리움도")
    bot.generate_research_response(cid, "casual")
    assert "2개" in fake.prompts[0]


def test_superseded_handler_skips_generation_entirely(monkeypatch):
    import bot

    calls = []
    monkeypatch.setattr(bot, "generate_chat_response", lambda *a: calls.append(a) or "")

    class _Client:
        def users_info(self, user):
            raise RuntimeError("no slack in tests")

        def conversations_history(self, channel, limit):
            raise RuntimeError("no slack in tests")

        def chat_postMessage(self, **kw):
            pass

    cid = "C_precheck"
    bot.memory.clear_channel(cid)
    bot.memory.add_message(cid, "Harry", "이전 대화")  # non-empty → no cold sync
    bot.state.mode = "auto"
    bot.state.last_chat_ts.clear()
    bot._mark_chat_message(cid, "100.200")  # a newer message already arrived
    event = {"channel": cid, "user": "UOTHER", "text": "안녕", "ts": "100.100"}
    bot.handle_message(event, _Client(), bot.logger)
    assert calls == []  # stale handler never burns an LLM call
