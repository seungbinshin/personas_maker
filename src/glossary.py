"""Auto-maintained glossary of HyperAccel-internal terms.

The glossary is a single markdown file with an auto-maintained block and
a manual block. Only the auto block is rewritten by this module; manual
entries are always preserved and take precedence (auto upsert skips any
term that already appears in the manual block).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

AUTO_BEGIN = "<!-- BEGIN-AUTO -->"
AUTO_END = "<!-- END-AUTO -->"
MANUAL_BEGIN = "<!-- BEGIN-MANUAL -->"
MANUAL_END = "<!-- END-MANUAL -->"

STOPWORDS = {
    "a", "an", "and", "the", "or", "but", "if", "of", "in", "on", "at",
    "to", "for", "by", "with", "from", "as", "is", "are", "was", "were",
    "be", "been", "being", "has", "have", "had", "do", "does", "did",
    "this", "that", "these", "those", "it", "its", "they", "them",
    "what", "which", "who", "how", "why", "when", "where",
    "not", "no", "yes", "also", "than", "then", "so", "such",
    "will", "would", "should", "could", "may", "might",
    "md", "html", "json", "eg", "ie", "etc",
}

SHORT_TERM_MIN_COUNT = 10
DEFAULT_MIN_COUNT = 3


def is_candidate_term(term: str, count: int) -> bool:
    t = term.strip()
    if not t:
        return False
    if t.lower() in STOPWORDS:
        return False
    if len(t) <= 2:
        return count >= SHORT_TERM_MIN_COUNT
    return count >= DEFAULT_MIN_COUNT


@dataclass
class GlossaryEntry:
    term: str
    count: int
    breakdown: dict[str, int]
    sample_context: str
    first_seen: str

    def render(self) -> str:
        breakdown_str = ", ".join(f"{k}: {v}" for k, v in sorted(self.breakdown.items()))
        safe_sample = (self.sample_context or "").strip().replace("\n", " ")
        if len(safe_sample) > 200:
            safe_sample = safe_sample[:200] + "..."
        return (
            f"## {self.term}\n"
            f"- **Occurrences**: {self.count}"
            + (f" ({breakdown_str})" if breakdown_str else "")
            + "\n"
            f"- **First seen**: {self.first_seen}\n"
            f"- **Sample context**: {safe_sample}\n"
        )


class GlossaryManager:
    """Reads and writes the auto block of `knowledge/glossary.md`."""

    def __init__(self, bot_dir: str | Path):
        self.bot_dir = Path(bot_dir)
        self.path = self.bot_dir / "knowledge" / "glossary.md"

    def upsert(
        self,
        term: str,
        count: int,
        breakdown: dict[str, int],
        sample_context: str,
        first_seen: str,
    ) -> None:
        """Insert or update a term in the auto block. Skips if term is in manual block."""
        auto_block, manual_block = self._read_blocks()

        if self._term_in_block(manual_block, term):
            logger.debug("Skipping '%s' — already present in manual block", term)
            return

        entries = self._parse_entries(auto_block)
        entries[term] = GlossaryEntry(
            term=term,
            count=count,
            breakdown=breakdown,
            sample_context=sample_context,
            first_seen=first_seen,
        )

        self._write(entries, manual_block)

    def load_auto_text(self, max_entries: int = 50) -> str:
        """Return the auto block content (sans markers), capped at max_entries."""
        if not self.path.exists():
            return ""
        auto_block, _ = self._read_blocks()
        entries = self._parse_entries(auto_block)
        if not entries:
            return ""
        sorted_terms = sorted(
            entries.values(),
            key=lambda e: (-e.count, e.term.lower()),
        )[:max_entries]
        return "\n".join(e.render() for e in sorted_terms).strip()

    def grep_vault(self, term: str) -> tuple[int, dict[str, int], str]:
        """Return (total_count, per-area breakdown, sample context line) for term.

        Areas: context, knowledge, reports.
        """
        areas = {
            "context": self.bot_dir / "context",
            "knowledge": self.bot_dir / "knowledge",
            "reports": self.bot_dir / "reports",
        }
        total = 0
        breakdown: dict[str, int] = {}
        sample: str = ""
        for area, area_path in areas.items():
            if not area_path.exists():
                continue
            count, first_hit = self._count_in_dir(area_path, term)
            if count:
                breakdown[area] = count
                total += count
                if not sample and first_hit:
                    sample = first_hit
        return total, breakdown, sample

    def refresh_candidates(self, candidates: set[str]) -> None:
        """Verify each candidate via grep and upsert those passing filters."""
        today = str(date.today())
        for term in candidates:
            try:
                count, breakdown, sample = self.grep_vault(term)
            except Exception as e:
                logger.warning("grep_vault failed for '%s': %s", term, e)
                continue
            if is_candidate_term(term, count):
                self.upsert(
                    term=term,
                    count=count,
                    breakdown=breakdown,
                    sample_context=sample,
                    first_seen=today,
                )

    # ── Internal helpers ──

    @staticmethod
    def _count_in_dir(root: Path, term: str) -> tuple[int, str]:
        """Count occurrences of term inside .md files under root. Return (count, first_match_line)."""
        rg = shutil.which("rg")
        if rg:
            try:
                proc = subprocess.run(
                    [rg, "--no-messages", "-F", "-c", term, "--glob", "*.md", str(root)],
                    capture_output=True, text=True, timeout=30, check=False,
                )
                if proc.returncode not in (0, 1):
                    logger.warning("rg failed for %s: %s", term, proc.stderr[:200])
                    return 0, ""
                total = 0
                for line in proc.stdout.splitlines():
                    if ":" in line:
                        try:
                            total += int(line.rsplit(":", 1)[1])
                        except ValueError:
                            pass
                sample = ""
                sample_proc = subprocess.run(
                    [rg, "--no-messages", "-F", "-N", "-m", "1", term, "--glob", "*.md", str(root)],
                    capture_output=True, text=True, timeout=30, check=False,
                )
                if sample_proc.returncode == 0:
                    for line in sample_proc.stdout.splitlines():
                        if ":" in line:
                            sample = line.split(":", 1)[1].strip()
                            break
                return total, sample
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                logger.warning("rg invocation failed: %s", e)

        # Fallback: pure Python
        total = 0
        sample = ""
        for md in root.rglob("*.md"):
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            hits = text.count(term)
            if hits:
                total += hits
                if not sample:
                    for line in text.splitlines():
                        if term in line:
                            sample = line.strip()
                            break
        return total, sample

    def _read_blocks(self) -> tuple[str, str]:
        """Return (auto_block_content, manual_block_content). Creates file if missing."""
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(self._scaffold(), encoding="utf-8")
        text = self.path.read_text("utf-8")
        auto = self._extract_between(text, AUTO_BEGIN, AUTO_END)
        manual = self._extract_between(text, MANUAL_BEGIN, MANUAL_END)
        return auto, manual

    @staticmethod
    def _extract_between(text: str, start: str, end: str) -> str:
        try:
            s = text.index(start) + len(start)
            e = text.index(end, s)
            return text[s:e].strip()
        except ValueError:
            return ""

    @staticmethod
    def _scaffold() -> str:
        return (
            "# HyperAccel 내부 용어 glossary\n\n"
            "<!-- auto-maintained by discourse_engagement — do not edit between markers -->\n"
            f"{AUTO_BEGIN}\n{AUTO_END}\n\n"
            f"{MANUAL_BEGIN}\n{MANUAL_END}\n"
        )

    @staticmethod
    def _term_in_block(block: str, term: str) -> bool:
        pattern = re.compile(rf"^##\s+{re.escape(term)}\s*$", re.MULTILINE)
        return bool(pattern.search(block))

    @staticmethod
    def _parse_entries(block: str) -> dict[str, GlossaryEntry]:
        """Parse existing auto block back into entries. Keeps unknown fields best-effort."""
        entries: dict[str, GlossaryEntry] = {}
        if not block.strip():
            return entries
        sections = re.split(r"^## ", block, flags=re.MULTILINE)
        for sect in sections:
            sect = sect.strip()
            if not sect:
                continue
            lines = sect.splitlines()
            term = lines[0].strip()
            count_match = re.search(r"\*\*Occurrences\*\*:\s*(\d+)", sect)
            first_match = re.search(r"\*\*First seen\*\*:\s*(\S+)", sect)
            sample_match = re.search(r"\*\*Sample context\*\*:\s*(.+)", sect)
            breakdown: dict[str, int] = {}
            if count_match:
                paren = re.search(r"\((.+)\)", count_match.group(0))
                if paren:
                    for part in paren.group(1).split(","):
                        if ":" in part:
                            k, v = part.split(":", 1)
                            try:
                                breakdown[k.strip()] = int(v.strip())
                            except ValueError:
                                pass
            entries[term] = GlossaryEntry(
                term=term,
                count=int(count_match.group(1)) if count_match else 0,
                breakdown=breakdown,
                sample_context=sample_match.group(1).strip() if sample_match else "",
                first_seen=first_match.group(1).strip() if first_match else str(date.today()),
            )
        return entries

    def _write(self, entries: dict[str, GlossaryEntry], manual_block: str) -> None:
        sorted_entries = sorted(
            entries.values(),
            key=lambda e: (-e.count, e.term.lower()),
        )
        auto_body = "\n".join(e.render() for e in sorted_entries).rstrip()

        text = (
            "# HyperAccel 내부 용어 glossary\n\n"
            "<!-- auto-maintained by discourse_engagement — do not edit between markers -->\n"
            f"{AUTO_BEGIN}\n"
            f"{auto_body}\n"
            f"{AUTO_END}\n\n"
            f"{MANUAL_BEGIN}\n"
            f"{manual_block.strip()}\n"
            f"{MANUAL_END}\n"
        )
        self.path.write_text(text, encoding="utf-8")


def _seed_from_vault(bot_dir: Path) -> int:
    """One-shot pass over the vault: extract candidate proper nouns and upsert."""
    candidates: set[str] = set()
    areas = [bot_dir / "context", bot_dir / "knowledge", bot_dir / "reports"]
    pattern = re.compile(r"\b([A-Z][A-Za-z]{2,}|[A-Z]{2,})\b")
    for area in areas:
        if not area.exists():
            continue
        for md in area.rglob("*.md"):
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in pattern.findall(text):
                candidates.add(match)
    logger.info("Seed: %d candidate terms found", len(candidates))

    gm = GlossaryManager(bot_dir)
    gm.refresh_candidates(candidates)
    return len(candidates)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["seed"])
    ap.add_argument("--bot-dir", required=True, type=Path)
    args = ap.parse_args()
    if args.command == "seed":
        n = _seed_from_vault(args.bot_dir)
        print(f"Seeded glossary from {n} candidates → {args.bot_dir}/knowledge/glossary.md")
