"""Prompts for the Discourse engagement pipeline."""

COMMENT_CLASSIFY_PROMPT = """You are classifying a comment on a technical research report posted to an internal Discourse forum.

The report was written by a research bot. You must decide how the bot should respond.

Report title: {report_title}

Comment author: {comment_author}
Comment (HTML):
{comment_html}

Previous posts in this thread (for context):
{thread_context}

Classify this comment into exactly ONE of these categories:

- "question": The author is asking a technical question about the report content, methodology, or applicability. They want information.
- "correction": The author is pointing out an error, disagreement, or factual issue with the report. They are providing counter-evidence or noting something wrong.
- "discussion": The author is replying to another human commenter, having a side conversation, or making a general remark not directed at the report/bot. The bot should NOT interject.
- "skip": The comment is praise, acknowledgment, emoji-only, off-topic, or doesn't warrant a response.

Return ONLY valid JSON:
{{
  "classification": "question|correction|discussion|skip",
  "reason": "One-line explanation in Korean of why you chose this classification",
  "key_topic": "The core technical topic or question being raised (empty string if skip/discussion)"
}}
"""

SEARCH_AND_DRAFT_PROMPT = """You are responding to a comment on an internal technical research report.
Your role is to provide a well-researched, factual answer with references.

{scope}

Report title: {report_title}
Report summary (first 2000 chars):
{report_excerpt}

Comment by {comment_author}:
{comment_text}

Comment type: {comment_type} (question or correction)

Internal knowledge (Confluence + Discourse):
{internal_context}

Instructions:
1. Use WebSearch to find 2-3 relevant external sources (papers, docs, benchmarks) related to the comment's topic.
2. Cross-reference the comment against the internal knowledge provided above.
3. Write a response in Korean that:
   - Directly addresses the commenter's point
   - Cites specific sources (with URLs) for factual claims
   - If the commenter found an error, acknowledge it honestly
   - If the commenter asked a question, answer it concisely with evidence
   - Keep the response under 500 words
4. End with a "참고 자료" section listing the sources you used.

Return ONLY the response text in markdown format. Do not wrap in JSON or code blocks.
"""

FACT_CHECK_PROMPT = """You are a fact-checker reviewing a draft response before it is posted publicly on an internal Discourse forum.

The response was drafted by an AI assistant to answer a comment on a research report.

Original comment by {comment_author}:
{comment_text}

Draft response:
{draft_response}

Internal knowledge (Confluence + Discourse):
{internal_context}

Verified internal-term glossary (HyperAccel-specific nouns known to be real):
{glossary}

Check the draft for:
1. **Factual accuracy**: Are the claims supported by the cited sources? Does anything contradict internal documents?
2. **Source quality**: Are the cited URLs real and relevant? Are any sources fabricated or questionable?
3. **Tone**: Is the response professional, helpful, and not dismissive of the commenter's point?
4. **Completeness**: Does it actually answer the question / address the correction?
5. **Overconfidence**: Does it claim certainty where uncertainty exists?

Guardrails when judging:
- Proper nouns that look like HyperAccel-specific internal components (HyperDex, LPU, SMA, MPU, VPU, LMU, ESL, BERTHA, HyperAccel, etc.) are likely REAL even if missing from internal_context. Do NOT declare them fabricated. If you still suspect misuse, use decision="revise" with guidance "cite or clarify this term" — never reject on this basis alone.
- External URLs that you cannot personally verify are "unverified", not "fabricated". If questionable, use decision="revise" with guidance "replace with a verifiable source (arXiv ID or DOI preferred) or remove".
- Claims of conference acceptance ("accepted at ICLR 2026" etc.) that you cannot confirm must be softened, not rejected outright. Use decision="revise" with guidance "describe as arXiv preprint unless acceptance is confirmed".
- Reserve decision="reject" for drafts that are unsafe to post even after revision — factually dangerous, clearly hostile, or off-topic. Everything else should be "approve" or "revise".

Return ONLY valid JSON:
{{
  "decision": "approve|revise|reject",
  "issues": [
    "Specific issue found (empty array if none)"
  ],
  "revision_guidance": "What to fix if decision is revise (empty string if approve)",
  "reason": "Overall assessment in Korean (1-2 lines)"
}}
"""

REVISE_DRAFT_PROMPT = """You are revising a draft response based on fact-checker feedback.

Original comment by {comment_author}:
{comment_text}

Previous draft:
{draft_response}

Fact-checker feedback:
{fact_check_feedback}

Internal knowledge:
{internal_context}

Instructions:
1. Fix the specific issues identified by the fact-checker.
2. If sources were questionable, use WebSearch to find better ones.
3. Keep the response concise (under 500 words).
4. Maintain a professional, helpful tone.

Return ONLY the revised response text in markdown format.
"""
