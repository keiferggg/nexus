"""Heuristic detection of text that addresses AI agents (possible prompt injection).

This is a *signal*, not a defense on its own: it cannot catch every injection and may flag
benign text (e.g. an article about prompt injection). NEXUS uses it only to make things
stricter - an attempt that read flagged content loses unattended write privileges - never to
grant anything.
"""

from __future__ import annotations

import re

PATTERNS: list[tuple[str, str, int]] = [
    ("override", r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|your)\b[^.\n]{0,30}"
                 r"\b(instructions?|prompts?|rules?|directions?|guidelines?)", 3),
    ("role_reassign", r"\b(you are now|from now on,? you|act as|pretend to be|your new (role|task|instructions?))\b", 2),
    ("system_claim", r"(^|\n|\s)(system|developer|admin|administrator)\s*(override|prompt|message|instruction|mode)\s*[:\-]", 3),
    ("authority_claim", r"\b(you are (now )?(authorized|permitted|allowed)|this is (an? )?(authorized|official) (request|instruction))\b", 2),
    ("agent_address", r"\b(ai|llm|language model|assistant|agent|chatbot)s?\b[^.\n]{0,30}\b(must|should|need to|are instructed to|will)\b", 1),
    ("exfiltration", r"(?<![.\w])(send|post|upload|forward|email|exfiltrate|transmit|leak)\b(?!\s*[=,:(.)\]])[^.\n=(){}\[\];]{0,60}\b(api[_ ]?keys?|credentials?|passwords?|"
                     r"secrets?|tokens?|environment variables|\.env|conversation|system prompt|private)", 3),
    ("policy_tamper", r"\b(raise|increase|disable|bypass|turn off|grant (yourself|you))\b[^.\n]{0,40}\b(budget|limit|permissions?|"
                      r"capabilit(y|ies)|approval|guard|safety|filter)", 3),
    ("hidden_markup", r"<!--[^>]{0,400}\b(instruction|assistant|agent|ignore)\b|\[\s*(system|INST)\s*\]|<\|im_start\|>", 2),
    ("self_accept", r"\b(mark|set)\b[^.\n]{0,30}\b(task|this)\b[^.\n]{0,20}\b(as )?(accepted|complete|approved)\b", 2),
]
THRESHOLD = 3


def scan(text: str) -> dict:
    """Returns {"score": int, "flagged": bool, "matches": [{"kind", "excerpt"}]}."""
    if not text:
        return {"score": 0, "flagged": False, "matches": []}
    sample = text[:400_000]
    matches, score = [], 0
    for kind, pattern, weight in PATTERNS:
        m = re.search(pattern, sample, re.I)
        if m:
            score += weight
            a, b = max(0, m.start() - 40), min(len(sample), m.end() + 40)
            matches.append({"kind": kind, "excerpt": " ".join(sample[a:b].split())[:200]})
    return {"score": score, "flagged": score >= THRESHOLD, "matches": matches}
