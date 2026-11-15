"""Lightweight search utility over curated Tableau performance & design references."""

from __future__ import annotations

import re
from pathlib import Path
from app.config import REFERENCES_DIR, SKILLS_DIR


def search_tableau_docs(query: str, max_results: int = 5) -> list[dict]:
    """Search curated Tableau reference markdown guides by keyword relevance."""
    tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9_]+", query) if len(t) >= 2]
    if not tokens:
        return []

    md_files: list[Path] = []
    skill_md = SKILLS_DIR / "tableau-performance-auditor" / "SKILL.md"
    if skill_md.exists():
        md_files.append(skill_md)
    if REFERENCES_DIR.exists():
        md_files.extend(sorted(REFERENCES_DIR.glob("*.md")))

    scored_chunks: list[tuple[int, dict]] = []
    for path in md_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        # Split on markdown headers (## or #)
        sections = re.split(r"(?m)^(?=#{1,3}\s+)", text)
        for sec in sections:
            sec_clean = sec.strip()
            if not sec_clean:
                continue
            lower = sec_clean.lower()
            score = sum(lower.count(tok) for tok in tokens)
            # Bonus if all tokens appear
            if all(tok in lower for tok in tokens):
                score += 5
            if score > 0:
                first_line = sec_clean.splitlines()[0].lstrip("# ").strip()
                scored_chunks.append(
                    (
                        score,
                        {
                            "source": path.name,
                            "section": first_line,
                            "excerpt": sec_clean[:1200],
                            "relevance_score": score,
                        },
                    )
                )

    scored_chunks.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored_chunks[:max_results]]
