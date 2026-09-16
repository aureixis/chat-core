import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    status: str
    reasons: list[str]


_GENERATED_BLOCKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("human_impersonation", re.compile(r"\b(i am|i'm) (a )?human\b", re.I)),
    ("emotional_coercion", re.compile(r"\b(don't|do not) leave me\b|\byou only need me\b", re.I)),
    ("financial_coercion", re.compile(r"\b(send|give|pay) me (money|crypto|bitcoin)\b", re.I)),
    (
        "credential_request",
        re.compile(r"\b(send|tell|share).{0,30}(password|passcode|private key)\b", re.I),
    ),
)

_HIGH_RISK_USER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "credible_threat",
        re.compile(r"\b(i will|i'm going to) (kill|hurt) (you|them|him|her)\b", re.I),
    ),
    ("credential_exposure", re.compile(r"\b(password|private key)\s*[:=]\s*\S+", re.I)),
)

_KNOWLEDGE_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "prompt_override",
        re.compile(
            r"\b(ignore|disregard|override).{0,40}(previous|system|developer).{0,20}(instruction|prompt)",
            re.I,
        ),
    ),
    (
        "system_prompt_extraction",
        re.compile(r"\b(reveal|print|repeat|show).{0,30}(system|developer) prompt\b", re.I),
    ),
    (
        "tool_manipulation",
        re.compile(r"\b(call|invoke|execute|use).{0,20}(tool|function|shell|command)\b", re.I),
    ),
    (
        "credential_exfiltration",
        re.compile(r"\b(exfiltrate|send|upload).{0,30}(secret|credential|api key|token)\b", re.I),
    ),
)


def evaluate_generated_content(
    text: str, blocked_topics: list[str] | None = None
) -> PolicyDecision:
    reasons: list[str] = []
    stripped = text.strip()
    if not stripped:
        reasons.append("empty_content")
    if len(stripped) > 4000:
        reasons.append("content_too_long")
    for label, pattern in _GENERATED_BLOCKS:
        if pattern.search(stripped):
            reasons.append(label)
    lowered = stripped.casefold()
    for topic in blocked_topics or []:
        if topic.strip() and topic.strip().casefold() in lowered:
            reasons.append(f"blocked_topic:{topic.strip()}")
    return PolicyDecision(not reasons, "approved" if not reasons else "blocked", reasons)


def evaluate_user_content(text: str) -> PolicyDecision:
    reasons = [label for label, pattern in _HIGH_RISK_USER_PATTERNS if pattern.search(text)]
    return PolicyDecision(not reasons, "approved" if not reasons else "flagged", reasons)


def evaluate_knowledge_content(text: str) -> PolicyDecision:
    reasons = [label for label, pattern in _KNOWLEDGE_INJECTION_PATTERNS if pattern.search(text)]
    return PolicyDecision(not reasons, "approved" if not reasons else "quarantined", reasons)
