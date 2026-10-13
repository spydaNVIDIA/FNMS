"""Deterministic deduplication, ranking, change classification, and rendering."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from .tools import normalize_url
from .types import Development


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:255]


def _match_saved(item: Development, saved: list[dict[str, Any]]) -> str | None:
    for previous in saved:
        if normalize_key(previous["key"]) == item.key:
            return normalize_key(previous["key"])
    title = item.title.lower()
    best: tuple[float, str] = (0, "")
    for previous in saved:
        ratio = SequenceMatcher(None, title, str(previous["title"]).lower()).ratio()
        if ratio > best[0]:
            best = (ratio, normalize_key(previous["key"]))
    return best[1] if best[0] >= 0.9 else None


def prepare_developments(
    raw_items: list[dict[str, Any]],
    state: dict[str, Any],
    valid_source_urls: set[str],
    top_k: int,
) -> list[Development]:
    saved = list(state.get("developments", []))
    previous_top = {normalize_key(value) for value in state.get("previous_top_k", [])}
    merged: dict[str, Development] = {}
    for raw in raw_items:
        sources = sorted({normalize_url(url) for url in raw["source_urls"]})
        if not sources or not all(source in valid_source_urls for source in sources):
            raise ValueError(f"Development {raw.get('title', '')!r} cites an unfetched source")
        item = Development(
            key=normalize_key(str(raw["key"])),
            title=str(raw["title"]).strip()[:500],
            summary=str(raw["summary"]).strip(),
            score=max(0.0, min(100.0, float(raw["score"]))),
            source_urls=sources,
        )
        saved_key = _match_saved(item, saved)
        if saved_key:
            item.key = saved_key
        if not item.key or not item.title or not item.summary:
            raise ValueError("Developments require non-empty keys, titles, and summaries")
        existing = merged.get(item.key)
        if existing is None:
            merged[item.key] = item
        else:
            existing.source_urls = sorted(set(existing.source_urls + item.source_urls))
            if item.score > existing.score:
                existing.title = item.title
                existing.summary = item.summary
                existing.score = item.score

    ranked = sorted(merged.values(), key=lambda value: (-value.score, value.title.lower()))[:top_k]
    for index, item in enumerate(ranked, start=1):
        item.rank = index
        item.bucket = "still" if item.key in previous_top else "new"

    ranked_keys = {item.key for item in ranked}
    by_key = {normalize_key(item["key"]): item for item in saved}
    for dropped_key in sorted(previous_top - ranked_keys):
        previous = by_key.get(dropped_key)
        if previous:
            ranked.append(
                Development(
                    key=dropped_key,
                    title=str(previous["title"]),
                    summary=str(previous["summary"]),
                    score=0,
                    source_urls=list(previous["source_urls"]),
                    bucket="dropped",
                )
            )
    return ranked


def render_report(
    topic: str,
    status: str,
    started_at: str,
    developments: list[Development],
    stop_reason: str | None = None,
) -> str:
    lines = [
        "# Open-source robotics foundation model tracker",
        "",
        f"- Topic: {topic}",
        f"- Run started: {started_at}",
        f"- Status: {status}",
    ]
    if stop_reason:
        lines.append(f"- Stop reason: {stop_reason}")
    sections = (
        ("New since last run", "new"),
        ("Still in top K", "still"),
        ("Dropped", "dropped"),
    )
    for heading, bucket in sections:
        lines.extend(["", f"## {heading}", ""])
        items = [item for item in developments if item.bucket == bucket]
        if not items:
            lines.append("None.")
            continue
        for item in items:
            rank = f"{item.rank}. " if item.rank is not None else ""
            lines.extend([f"### {rank}{item.title}", "", item.summary, "", "Sources:"])
            lines.extend(f"- {url}" for url in item.source_urls)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
