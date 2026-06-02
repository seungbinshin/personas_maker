"""Tests for SeungBin persona anti-loop fixes.

Covers the self-reinforcing loop / flooding / recap pathology fixes:
- output-level dedup against the bot's own recent lines (self-cascade blocker)
- burst cap (enforce the documented 2-3 message pattern)
- RAG no longer force-injects humor examples on dark/aggressive input
- system prompt carries a de-escalation (tone-awareness) instruction
- ChannelMemory helpers used to break the self-echo feedback edge
"""

from __future__ import annotations

import os
from pathlib import Path

# bot.py reads SLACK_USER_ID at import; provide a dummy so it imports in tests.
os.environ.setdefault("SLACK_USER_ID", "UTEST")

from skills.conversation.session_orchestrator import (
    ConversationSessionOrchestrator as Orch,
)
from skills.persona.context_builder import PersonaContextBuilder

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"


# ─── dedupe_lines: the self-repetition blocker ──────────────────────


def test_dedupe_drops_exact_repeat_long_line():
    lines = ["야마자키 마시러 가자 진짜", "야마자키 마시러 가자 진짜"]
    assert Orch.dedupe_lines(lines) == ["야마자키 마시러 가자 진짜"]


def test_dedupe_drops_near_duplicate_against_recent_bot_output():
    recent = ["야마자키 마시러 가자 진짜"]
    lines = ["야마자키나 마시러 가자 진짜"]  # near-identical variant from a prior turn
    assert Orch.dedupe_lines(lines, recent=recent) == []


def test_dedupe_keeps_short_signature_tokens_even_if_repeated():
    # ㅋㅋㅋㅋ / ㄷㄷ / 헉 are persona signatures — repeating them is fine.
    lines = ["ㅋㅋㅋㅋ", "ㅋㅋㅋㅋ", "ㄷㄷ", "헉"]
    assert Orch.dedupe_lines(lines) == ["ㅋㅋㅋㅋ", "ㅋㅋㅋㅋ", "ㄷㄷ", "헉"]


def test_dedupe_keeps_distinct_lines():
    lines = ["오 그거 좋네", "근데 좀 비싸지 않아?"]
    assert Orch.dedupe_lines(lines) == ["오 그거 좋네", "근데 좀 비싸지 않아?"]


# ─── cap_bursts: enforce the 2-3 burst pattern ──────────────────────


def test_cap_bursts_caps_to_three_by_default():
    assert Orch.cap_bursts(["a", "b", "c", "d", "e"]) == ["a", "b", "c"]


def test_cap_bursts_under_limit_unchanged():
    assert Orch.cap_bursts(["a", "b"]) == ["a", "b"]


# ─── retrieve_context: no humor fallback on dark/aggressive input ───


def test_retrieve_context_no_humor_fallback_on_dark_input():
    builder = PersonaContextBuilder(OUTPUT_DIR)
    assert "humor_examples" in builder.chunks  # precondition: humor chunk exists
    out = builder.retrieve_context("죽어 살인 회뜨기 소장 노로")
    assert "# 유머 예시" not in out  # must NOT force humor on dark input
    assert "신승빈" in out or "identity" in out  # core identity still present


def test_retrieve_context_still_returns_matched_chunk_on_topic():
    builder = PersonaContextBuilder(OUTPUT_DIR)
    out = builder.retrieve_context("나스닥 비트코인 매수 타이밍 어때")
    assert "투자" in out  # investment chunk still retrieved when it matches


# ─── build_system_prompt: de-escalation / tone-awareness instruction ─


def test_build_system_prompt_has_deescalation_rule():
    builder = PersonaContextBuilder(OUTPUT_DIR)
    prompt = builder.build_system_prompt("아무 대화", channel_tone="casual")
    assert "공격적으로" in prompt  # tone-awareness instruction marker


# ─── ChannelMemory: break the self-echo feedback edge ───────────────


def test_recent_bot_lines_returns_only_bot_lines_split():
    from bot import ChannelMemory, DISPLAY_NAME

    m = ChannelMemory()
    cid = "C1"
    m.add_message(cid, "Harry", "안녕")
    m.add_message(cid, DISPLAY_NAME, "오 안녕\nㄷㄷ", is_bot=True)
    m.add_message(cid, "Harry", "뭐해")
    assert m.recent_bot_lines(cid) == ["오 안녕", "ㄷㄷ"]


def test_exclude_last_bot_drops_trailing_bot_turn():
    from bot import ChannelMemory, DISPLAY_NAME

    m = ChannelMemory()
    cid = "C1"
    m.add_message(cid, "Harry", "안녕")
    m.add_message(cid, DISPLAY_NAME, "오 안녕", is_bot=True)
    conv = m.get_conversation_exclude_last_bot(cid)
    assert "[승빈] 오 안녕" not in conv
    assert "[Harry] 안녕" in conv


def test_exclude_last_bot_keeps_history_when_human_is_last():
    from bot import ChannelMemory, DISPLAY_NAME

    m = ChannelMemory()
    cid = "C1"
    m.add_message(cid, DISPLAY_NAME, "오 안녕", is_bot=True)
    m.add_message(cid, "Harry", "뭐해")
    conv = m.get_conversation_exclude_last_bot(cid)
    assert "[승빈] 오 안녕" in conv
    assert "[Harry] 뭐해" in conv


# ─── send_response integration: dedup + cap + store-sent ────────────


class _FakeClient:
    def __init__(self):
        self.posted = []

    def chat_postMessage(self, channel, text, thread_ts=None):
        self.posted.append(text)


def test_send_response_caps_burst_to_three():
    import bot

    cid = "C_cap"
    bot.memory.clear_channel(cid)
    client = _FakeClient()
    bot.send_response(client, cid, "한줄\n두줄\n세줄\n네줄\n다섯줄")
    assert client.posted == ["한줄", "두줄", "세줄"]
    # memory stores only what was actually sent
    assert bot.memory.get_conversation(cid) == "[승빈] 한줄\n두줄\n세줄"


def test_send_response_suppresses_pure_self_repeat():
    import bot

    cid = "C_repeat"
    bot.memory.clear_channel(cid)
    # The bot already said this last turn.
    bot.memory.add_message(cid, bot.DISPLAY_NAME, "야마자키 마시러 가자 진짜", is_bot=True)
    client = _FakeClient()
    bot.send_response(client, cid, "야마자키나 마시러 가자 진짜")  # near-verbatim repeat
    assert client.posted == []  # nothing posted — self-cascade blocked


def test_send_response_keeps_fresh_line_drops_repeat():
    import bot

    cid = "C_mix"
    bot.memory.clear_channel(cid)
    bot.memory.add_message(cid, bot.DISPLAY_NAME, "야마자키 마시러 가자 진짜", is_bot=True)
    client = _FakeClient()
    bot.send_response(client, cid, "야마자키 마시러 가자 진짜\n오 그건 진짜 새로운 얘기네")
    assert client.posted == ["오 그건 진짜 새로운 얘기네"]
