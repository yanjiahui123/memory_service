"""Extraction helper logic — compress and parse LLM outputs.

Three-stage pipeline helpers:
  Stage 1 (Structure): build_structure_messages / parse_structured_analysis
  Stage 2 (Atomize):   build_atomize_messages / parse_atomized_facts
  Stage 3 (Gate):      build_gate_messages / parse_gated_facts
"""

import json
import logging

from forum_memory.core.prompts import (
    FACT_EXTRACTION_SYSTEM, FACT_EXTRACTION_USER,
    COMPRESS_SYSTEM, COMPRESS_USER,
    STRUCTURE_SYSTEM, STRUCTURE_USER,
    ATOMIZE_SYSTEM, ATOMIZE_USER,
    GATE_SYSTEM, GATE_USER,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Compression
# ---------------------------------------------------------------------------

def build_compress_messages(title: str, question: str, discussion: str) -> list[dict]:
    """Build messages for the compression LLM call."""
    return [
        {"role": "system", "content": COMPRESS_SYSTEM},
        {"role": "user", "content": COMPRESS_USER.format(title=title, question=question, discussion=discussion)},
    ]


# ---------------------------------------------------------------------------
# Stage 1: Structure
# ---------------------------------------------------------------------------

def build_structure_messages(title: str, question: str, discussion: str) -> list[dict]:
    """Build messages for the structure analysis LLM call."""
    return [
        {"role": "system", "content": STRUCTURE_SYSTEM},
        {"role": "user", "content": STRUCTURE_USER.format(
            title=title, question=question, discussion=discussion
        )},
    ]


def parse_structured_analysis(raw: str) -> dict | None:
    """Parse LLM output into a structured analysis dict. Returns None on failure."""
    text = raw.strip()
    if text.startswith("```"):
        text = _strip_code_fences(text)
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse structured analysis: %s", text[:200])
        return None
    if not isinstance(result, dict):
        logger.warning("Structured analysis is not a dict: %s", text[:200])
        return None
    return result


# ---------------------------------------------------------------------------
# Stage 2: Atomize
# ---------------------------------------------------------------------------

def build_atomize_messages(structured: dict) -> list[dict]:
    """Build messages for the atomization LLM call."""
    structured_text = json.dumps(structured, ensure_ascii=False, indent=2)
    return [
        {"role": "system", "content": ATOMIZE_SYSTEM},
        {"role": "user", "content": ATOMIZE_USER.format(structured=structured_text)},
    ]


def parse_atomized_facts(raw: str) -> list[dict]:
    """Parse LLM output into a list of atomized knowledge point dicts."""
    text = raw.strip()
    if text.startswith("```"):
        text = _strip_code_fences(text)
    try:
        atoms = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse atomized facts: %s", text[:200])
        return []
    if not isinstance(atoms, list):
        return []
    return [a for a in atoms if _is_valid_atom(a)]


def _is_valid_atom(atom: dict) -> bool:
    """Check that an atom has required 'what' and 'when' fields."""
    return isinstance(atom, dict) and bool(atom.get("what")) and bool(atom.get("when"))


# ---------------------------------------------------------------------------
# Stage 3: Gate
# ---------------------------------------------------------------------------

def build_gate_messages(knowledge_points: list[dict]) -> list[dict]:
    """Build messages for the quality gate LLM call."""
    kp_text = json.dumps(knowledge_points, ensure_ascii=False, indent=2)
    return [
        {"role": "system", "content": GATE_SYSTEM},
        {"role": "user", "content": GATE_USER.format(knowledge_points=kp_text)},
    ]


def parse_gated_facts(raw: str) -> list[dict]:
    """Parse gate output; convert atoms to standard fact format.

    Standard fact format:
        {"content": str, "tags": list, "knowledge_type": str,
         "gate_confidence": float, "low_quality": bool}

    Atoms with pass_gate=True 正常入库；pass_gate=False 但 gate_confidence ≥
    low_quality_gate_min 的原子标记 low_quality=True，由下游写入 pending 队列
    供人工评估；低于阈值的直接丢弃。
    """
    from forum_memory.config import get_settings

    text = raw.strip()
    if text.startswith("```"):
        text = _strip_code_fences(text)
    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse gated facts: %s", text[:200])
        return []
    if not isinstance(items, list):
        return []

    low_q_min = get_settings().low_quality_gate_min
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        confidence = _parse_gate_confidence(item)
        passed = bool(item.get("pass_gate"))
        if not passed and confidence < low_q_min:
            logger.debug(
                "Knowledge point dropped (low confidence %.2f): %s — %s",
                confidence,
                str(item.get("what", ""))[:80],
                item.get("gate_reason", ""),
            )
            continue
        results.append({
            "content": _compose_content(item),
            "tags": item.get("tags") or [],
            "knowledge_type": item.get("knowledge_type") or "faq",
            "gate_confidence": confidence,
            "low_quality": not passed,
        })
    return results


def _parse_gate_confidence(item: dict) -> float:
    """Extract and clamp gate_confidence from a gated item.

    Returns 0.5 (neutral) if the field is missing or invalid, ensuring
    backward compatibility with older Gate prompts.
    """
    raw = item.get("gate_confidence")
    if raw is None:
        return 0.5
    try:
        val = float(raw)
        return max(0.0, min(1.0, val))
    except (TypeError, ValueError):
        return 0.5


def _compose_content(atom: dict) -> str:
    """Compose a rich content string from an atomized knowledge point."""
    parts = [atom["what"]]
    if atom.get("when"):
        parts.append(f"适用场景: {atom['when']}")
    if atom.get("how"):
        parts.append(f"操作方法: {atom['how']}")
    if atom.get("why"):
        parts.append(f"原因: {atom['why']}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences from LLM output."""
    lines = text.split("\n")
    if lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Legacy single-stage extraction (kept for compatibility)
# ---------------------------------------------------------------------------

def build_extract_messages(title: str, question: str, discussion: str) -> list[dict]:
    """Build messages for the legacy single-stage fact extraction LLM call."""
    return [
        {"role": "system", "content": FACT_EXTRACTION_SYSTEM},
        {"role": "user", "content": FACT_EXTRACTION_USER.format(
            title=title, question=question, discussion=discussion
        )},
    ]


def parse_extracted_facts(raw: str) -> list[dict]:
    """Parse LLM output into a list of fact dicts (legacy single-stage)."""
    text = raw.strip()
    if text.startswith("```"):
        text = _strip_code_fences(text)
    try:
        facts = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse extraction output: %s", text[:200])
        return []
    if not isinstance(facts, list):
        return []
    return [f for f in facts if _is_valid_fact(f)]


def _is_valid_fact(fact: dict) -> bool:
    """Check that a fact dict has the required 'content' field."""
    return isinstance(fact, dict) and bool(fact.get("content"))
