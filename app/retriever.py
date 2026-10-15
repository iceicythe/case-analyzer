"""Retrieve troubleshooting knowledge for an analyzed event."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.paths import PROJECT_ROOT


KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"
DEFAULT_CATALOG = KNOWLEDGE_ROOT / "catalog.json"


@dataclass(frozen=True)
class KnowledgeMatch:
    knowledge_id: str
    title: str
    source_file: str
    score: float
    content: str


def _tokens(text: str) -> set[str]:
    return set(
        re.findall(
            r"[a-zA-Z_][a-zA-Z0-9_]*|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,}",
            text.lower(),
        )
    )


def retrieve_knowledge(
    chain_status: str,
    object_status: str,
    reason_text: str,
    top_k: int = 2,
    catalog_path: Path = DEFAULT_CATALOG,
) -> list[KnowledgeMatch]:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    query_tokens = _tokens(
        f"{chain_status} {object_status} {reason_text}"
    )
    scored_matches: list[tuple[bool, KnowledgeMatch]] = []

    for entry in catalog:
        content = (KNOWLEDGE_ROOT / entry["source_file"]).read_text(
            encoding="utf-8"
        )
        score = 0.0
        exact_chain_match = chain_status in entry["chain_statuses"]
        if exact_chain_match:
            score += 100.0
        if object_status in entry["object_statuses"]:
            score += 30.0

        searchable_text = " ".join(
            [entry["title"], *entry["keywords"], content]
        )
        score += 2.0 * len(query_tokens.intersection(_tokens(searchable_text)))
        if score <= 0:
            continue

        scored_matches.append(
            (
                exact_chain_match,
                KnowledgeMatch(
                    knowledge_id=entry["knowledge_id"],
                    title=entry["title"],
                    source_file=entry["source_file"],
                    score=score,
                    content=content,
                ),
            )
        )

    # The end-to-end chain status is the strongest routing signal. Object
    # status and free text are fallbacks for chain statuses unknown to the
    # catalog, rather than reasons to mix unrelated troubleshooting manuals.
    if any(exact for exact, _ in scored_matches):
        matches = [match for exact, match in scored_matches if exact]
    else:
        matches = [match for _, match in scored_matches]

    matches.sort(key=lambda match: (-match.score, match.knowledge_id))
    return matches[:top_k]
