"""Deterministic evidence checks required by the assignment.

These checks establish that a quotation is present. The model still has to judge
whether the quotation supports the claim; substring membership is not entailment.
"""

import html
import re
import unicodedata


def normalize(text: str) -> str:
    """Apply exactly the four normalization steps in the lab prompt."""
    decoded = html.unescape(text)
    normalized = unicodedata.normalize("NFKC", decoded)
    return re.sub(r"\s+", " ", normalized).lower()


def valid_evidence(excerpt: str | None, page_text: str) -> bool:
    """Reject absent, empty, oversized, and invented quotations."""
    if not isinstance(excerpt, str) or not excerpt.strip() or len(excerpt) > 300:
        return False
    normalized_excerpt = normalize(excerpt)
    if not normalized_excerpt.strip():
        return False
    return normalized_excerpt in normalize(page_text)
