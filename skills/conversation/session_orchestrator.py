"""Shared conversation/session orchestration skill."""

from __future__ import annotations

import difflib
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Callable

from skills.types import LLMRunRequest
from tools.claude_runtime import ClaudeRuntimeClient

# NOTE: 졌 needs a hangul lookbehind — bare 졌 substring-matched the extremely
# common -아/어지다 conjugation (빠졌니, 좋아졌어, 터졌다 …) and misrouted
# casual chat to the 120s research path with a spurious "잠시만 검색좀" interim.
RESEARCH_KEYWORDS = re.compile(
    r"(시세|시가|종가|주가|얼마|몇\s?달러|몇\s?원|환율|금리|금값"
    r"|나스닥|코스피|코스닥|다우|S&P|s&p|비트코인|이더리움|BTC|ETH|솔라나"
    r"|뉴스|소식|최근|최신|요즘|오늘|어제|이번\s?주|실적|발표"
    r"|날씨|기온|비\s?오|미세먼지"
    r"|찾아봐|검색해|알아봐|조사해|확인해|서치|search"
    r"|출시|업데이트|패치|언제\s?나|런칭|발매"
    r"|경기|스코어|순위|몇\s?대\s?몇|이겼|(?<![가-힣])졌)",
    re.IGNORECASE,
)

# Word tokens for topic-perseveration detection (>=2 chars; hangul/latin/digit).
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")


@dataclass(slots=True)
class ConversationMessage:
    role: str
    sender: str
    text: str
    ts: float


class ChannelMemory:
    """Per-channel memory buffer with prompt cache."""

    def __init__(self, max_messages: int = 30, session_timeout: int = 1800):
        self.history: dict[str, list[ConversationMessage]] = defaultdict(list)
        self.cached_prompts: dict[str, dict[str, object]] = {}
        self.max_messages = max_messages
        self.session_timeout = session_timeout

    def add_message(
        self,
        channel_id: str,
        sender: str,
        text: str,
        role: str = "user",
    ) -> None:
        self.history[channel_id].append(
            ConversationMessage(role=role, sender=sender, text=text, ts=time.time())
        )
        if len(self.history[channel_id]) > self.max_messages:
            self.history[channel_id] = self.history[channel_id][-self.max_messages :]

    def get_conversation(self, channel_id: str) -> str:
        cutoff = time.time() - self.session_timeout
        recent = [m for m in self.history.get(channel_id, []) if m.ts > cutoff]
        return "\n".join(f"[{m.sender}] {m.text}" for m in recent)

    def get_cached_prompt(self, channel_id: str, ttl_sec: int = 300) -> str | None:
        cached = self.cached_prompts.get(channel_id)
        if cached and (time.time() - float(cached["ts"])) < ttl_sec:
            return str(cached["prompt"])
        return None

    def cache_prompt(self, channel_id: str, prompt: str) -> None:
        self.cached_prompts[channel_id] = {"prompt": prompt, "ts": time.time()}

    def clear_channel(self, channel_id: str) -> None:
        self.history.pop(channel_id, None)
        self.cached_prompts.pop(channel_id, None)


class ConversationSessionOrchestrator:
    """Reusable conversation orchestration skill."""

    def __init__(
        self,
        runtime: ClaudeRuntimeClient,
        display_name: str,
        memory: ChannelMemory | None = None,
    ):
        self.runtime = runtime
        self.display_name = display_name
        self.memory = memory or ChannelMemory()

    @staticmethod
    def classify_question(text: str) -> str:
        return "research" if RESEARCH_KEYWORDS.search(text) else "chat"

    @staticmethod
    def split_response(text: str) -> list[str]:
        text = text.replace("\\n", "\n")
        return [line.strip() for line in text.split("\n") if line.strip()]

    @staticmethod
    def _normalize(s: str) -> str:
        """Whitespace/punctuation-insensitive key for repetition comparison."""
        return re.sub(r"[\s.,!?~…]+", "", s).lower()

    @staticmethod
    def dedupe_lines(
        lines: list[str],
        recent: list[str] | None = None,
        min_len: int = 7,
        threshold: float = 0.85,
    ) -> list[str]:
        """Drop lines that repeat earlier lines in this burst or the bot's recent
        output. Breaks the self-cascade where the bot replays the same phrase
        (e.g. "야마자키 마시러 가자") turn after turn.

        Short reaction tokens (< min_len chars, e.g. ㅋㅋㅋㅋ, ㄷㄷ, 헉) are signature
        phrases and are never deduped — repeating them is in-character.
        """
        recent_norm = [
            ConversationSessionOrchestrator._normalize(r)
            for r in (recent or [])
            if len(r.strip()) >= min_len
        ]
        kept: list[str] = []
        kept_norm: list[str] = []
        for line in lines:
            if len(line.strip()) < min_len:
                kept.append(line)
                continue
            norm = ConversationSessionOrchestrator._normalize(line)
            is_dup = any(
                norm == prev
                or difflib.SequenceMatcher(None, norm, prev).ratio() >= threshold
                for prev in kept_norm + recent_norm
            )
            if is_dup:
                continue
            kept.append(line)
            kept_norm.append(norm)
        return kept

    # ─── Convergence guard (anti self-replication, phase 2) ─────────
    #
    # The verbatim guards (dedupe_lines / exclude-last-bot) killed exact
    # repetition, after which the attractor moved one abstraction level up:
    # opener lock-in (흠 …), topic perseveration (멤도 across 10+ turns), and
    # a fixed line skeleton. Hardcoded token bans only RELOCATE the attractor
    # (banning ㅋ-openers produced the 흠 lock), so everything below is
    # content-agnostic: it measures whatever pattern the bot's own recent
    # output converged on and names that live pattern in the prompt.

    OPENER_WINDOW = 5     # turns examined for opener/closer lock-in
    OPENER_FLAG_MIN = 4   # 4-of-5: a 3-of-5 cluster is normal signature flavor
    TOPIC_WINDOW = 8      # recent bot turns for topic document-frequency
    TOPIC_MIN_DF = 4      # stem must recur in >=4 of the last 8 bot turns
    TOPIC_PRIOR_MAX_DF = 1  # …but NOT in the prior window (bursty topics only —
    #                         constant glue words like 근데 stay unflagged)
    TOPIC_HUMAN_MARGIN = 2  # bot must out-mention humans by >=2 turns
    TOPIC_HUMAN_WINDOW = 12  # human turns used as the self-calibrating baseline
    STRUCTURE_WINDOW = 4  # turns examined for a locked line-count skeleton
    MASKABLE_LEN = 2      # only short interjection tokens get masked/stripped

    @staticmethod
    def _collapse_runs(token: str) -> str:
        """ㅋㅋㅋㅋ → ㅋ: character-run collapse so spam variants of the same
        interjection count as one opener/closer."""
        return re.sub(r"(.)\1+", r"\1", token)

    @staticmethod
    def _stem_match(token: str, stem: str) -> bool:
        """token belongs to stem if equal, or stem + exactly one trailing char
        (absorbs single-char Korean particles: 멤도/멤도가/멤도랑 → 멤도)."""
        return token == stem or (len(token) == len(stem) + 1 and token.startswith(stem))

    @classmethod
    def detect_convergence(
        cls, bot_turns: list[str], human_turns: list[str]
    ) -> dict:
        """Detect emergent self-repetition attractors in the bot's recent turns.

        Pure function over plain lists (no memory handle) so any bot can call
        it. Returns {"opener", "opener_count", "closer", "closer_count",
        "topics", "line_count"} with None/[] meaning not flagged.
        """
        report: dict = {
            "opener": None, "opener_count": 0,
            "closer": None, "closer_count": 0,
            "topics": [], "line_count": None,
        }
        turns = [t.strip() for t in bot_turns if t and t.strip()]

        # OPENER / CLOSER lock-in: modal first/last token of recent turns.
        if len(turns) >= cls.OPENER_WINDOW:
            openers, closers = [], []
            for t in turns[-cls.OPENER_WINDOW:]:
                lines = [ln for ln in t.split("\n") if ln.strip()]
                first, last = lines[0].split(), lines[-1].split()
                if first:
                    openers.append(cls._collapse_runs(first[0]))
                if last:
                    closers.append(cls._collapse_runs(last[-1]))
            for key, items in (("opener", openers), ("closer", closers)):
                if items:
                    tok, cnt = Counter(items).most_common(1)[0]
                    if cnt >= cls.OPENER_FLAG_MIN:
                        report[key] = tok
                        report[f"{key}_count"] = cnt

        # TOPIC perseveration: stems the bot keeps reintroducing that (a) are
        # bursty (absent from the prior window — filters discourse glue),
        # (b) out-mention the humans' own usage, (c) aren't in the latest
        # human message (responding to what was just said is legitimate).
        recent = turns[-cls.TOPIC_WINDOW:]
        if len(recent) >= cls.TOPIC_MIN_DF:
            prior = turns[-2 * cls.TOPIC_WINDOW:-cls.TOPIC_WINDOW]
            recent_sets = [set(_TOKEN_RE.findall(t)) for t in recent]
            prior_sets = [set(_TOKEN_RE.findall(t)) for t in prior]
            human_sets = [
                set(_TOKEN_RE.findall(t))
                for t in human_turns[-cls.TOPIC_HUMAN_WINDOW:]
            ]
            latest_human = human_turns[-1] if human_turns else ""

            candidates: set[str] = set()
            for s in recent_sets:
                candidates |= s
                candidates |= {t[:-1] for t in s if len(t) >= 3}  # particle-stripped

            def df(stem: str, sets: list[set[str]]) -> int:
                return sum(
                    any(cls._stem_match(tok, stem) for tok in s) for s in sets
                )

            scored = []
            for stem in candidates:
                bot_df = df(stem, recent_sets)
                if bot_df < cls.TOPIC_MIN_DF:
                    continue
                if prior_sets and df(stem, prior_sets) > cls.TOPIC_PRIOR_MAX_DF:
                    continue
                human_df = df(stem, human_sets)
                if bot_df - human_df < cls.TOPIC_HUMAN_MARGIN:
                    continue
                if stem in latest_human:
                    continue
                scored.append((bot_df - human_df, bot_df, stem))
            scored.sort(key=lambda x: (-x[0], -x[1], len(x[2]), x[2]))
            kept: list[str] = []
            for _, _, stem in scored:
                if any(stem.startswith(k) or k.startswith(stem) for k in kept):
                    continue  # nested variants of an already-kept stem
                kept.append(stem)
                if len(kept) >= 3:
                    break
            report["topics"] = kept

        # STRUCTURE lock-in: identical multi-line count across recent turns.
        if len(turns) >= cls.STRUCTURE_WINDOW:
            counts = [
                len([ln for ln in t.split("\n") if ln.strip()])
                for t in turns[-cls.STRUCTURE_WINDOW:]
            ]
            if len(set(counts)) == 1 and counts[0] >= 2:
                report["line_count"] = counts[0]

        return report

    @staticmethod
    def has_convergence_flags(report: dict) -> bool:
        return bool(
            report.get("opener") or report.get("closer")
            or report.get("topics") or report.get("line_count")
        )

    @classmethod
    def render_convergence_note(cls, report: dict, streak: int = 1) -> str:
        """Render the DETECTED patterns as a dynamic corrective instruction.
        Empty string when nothing is flagged — zero noise on healthy convos."""
        items = []
        if report.get("opener"):
            items.append(
                f"최근 답변 {cls.OPENER_WINDOW}개 중 {report['opener_count']}개를 "
                f"'{report['opener']}'(으)로 시작했어. 이번엔 전혀 다르게 시작해."
            )
        if report.get("closer"):
            items.append(
                f"답변을 계속 '{report['closer']}'(으)로 끝내고 있어. 끝맺음을 바꿔."
            )
        if report.get("topics"):
            topics = ", ".join(f"'{t}'" for t in report["topics"])
            items.append(
                f"{topics} 얘기(표현)를 여러 턴째 반복해서 끌고 가는 중이야. "
                f"상대가 다시 꺼내기 전엔 쓰지 마."
            )
        if report.get("line_count"):
            items.append(
                f"매번 똑같이 {report['line_count']}줄로 답하고 있어. 줄 수를 바꿔."
            )
        if not items:
            return ""
        head = "[반복 감지] 네 최근 답변들이 똑같은 패턴에 갇혔어:"
        if streak >= 2:
            head = "[반복 경고] 직전 턴에도 지적했는데 계속 같은 패턴이야. 이번엔 반드시 깨:"
        return (
            head + "\n" + "\n".join(f"- {it}" for it in items)
            + "\n패턴만 피하고, 마지막 메시지에 자연스럽게 새로 반응해."
        )

    @classmethod
    def convergence_violations(cls, text: str, report: dict) -> list[str]:
        """Which flagged patterns does this candidate response still hit?
        (opener/closer/topic only — line structure gets prompt pressure, not
        enforcement.)"""
        violations: list[str] = []
        if not text:
            return violations
        lines = [ln for ln in text.split("\n") if ln.strip()]
        if not lines:
            return violations
        first, last = lines[0].split(), lines[-1].split()
        if report.get("opener") and first and (
            cls._collapse_runs(first[0]) == cls._collapse_runs(report["opener"])
        ):
            violations.append(f"또 '{report['opener']}'(으)로 시작함")
        if report.get("closer") and last and (
            cls._collapse_runs(last[-1]) == cls._collapse_runs(report["closer"])
        ):
            violations.append(f"또 '{report['closer']}'(으)로 끝남")
        tokens = set(_TOKEN_RE.findall(text))
        for stem in report.get("topics", []):
            if any(cls._stem_match(tok, stem) for tok in tokens):
                violations.append(f"'{stem}' 화제를 또 언급함")
        return violations

    @classmethod
    def strip_flagged_opener(cls, text: str, report: dict) -> str:
        """Deterministic backstop: if the final response STILL starts with the
        flagged opener after a retry, drop just that leading token (it is a
        short interjection — whatever token the detector flagged, not a
        hardcoded one). Guarantees the visible streak breaks even when the
        LLM disobeys twice. Never empties the message."""
        opener = report.get("opener")
        if not text or not opener:
            return text
        collapsed = cls._collapse_runs(opener)
        if len(collapsed) > cls.MASKABLE_LEN:
            return text  # only strip filler-sized interjections
        lines = text.split("\n")
        parts = lines[0].split()
        if not parts or cls._collapse_runs(parts[0]) != collapsed:
            return text
        rest_first = " ".join(parts[1:])
        new_lines = ([rest_first] if rest_first else []) + lines[1:]
        stripped = "\n".join(ln for ln in new_lines if ln.strip())
        return stripped if stripped.strip() else text

    @classmethod
    def mask_attractor_tokens(cls, text: str, tokens: tuple | list) -> str:
        """Remove flagged opener/closer interjections from a RENDERED context
        turn so in-context induction loses its fuel (the model stops seeing 14
        copies of its own locked opener). Memory itself is untouched."""
        masked = {
            cls._collapse_runs(t)
            for t in tokens
            if t and len(cls._collapse_runs(t)) <= cls.MASKABLE_LEN
        }
        if not masked:
            return text
        out = []
        for line in text.split("\n"):
            parts = line.split()
            if parts and cls._collapse_runs(parts[0]) in masked:
                parts = parts[1:]
            if parts and cls._collapse_runs(parts[-1]) in masked:
                parts = parts[:-1]
            if parts:
                out.append(" ".join(parts))
        return "\n".join(out)

    def build_system_prompt(
        self,
        channel_id: str,
        channel_tone: str,
        builder: Callable[[str, str], str],
    ) -> str:
        cached = self.memory.get_cached_prompt(channel_id)
        if cached:
            return cached
        conversation = self.memory.get_conversation(channel_id)
        prompt = builder(conversation, channel_tone)
        self.memory.cache_prompt(channel_id, prompt)
        return prompt

    def generate_response(
        self,
        *,
        channel_id: str,
        system_prompt: str,
        question_type: str,
        timeout_ms: int,
        research_instruction: str,
        chat_instruction: str,
        session_id: str | None = None,
    ) -> str:
        conversation = self.memory.get_conversation(channel_id)
        if not conversation:
            return ""

        instruction = research_instruction if question_type == "research" else chat_instruction
        prompt = f"{system_prompt}\n\n---\n\n{conversation}\n\n{instruction}"
        result = self.runtime.run(
            LLMRunRequest(prompt=prompt, timeout_ms=timeout_ms, session_id=session_id)
        )
        if not result.success:
            return ""
        return result.output if result.output != "[SKIP]" else ""

