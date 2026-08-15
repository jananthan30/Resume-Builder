# evidence_engine/evidence/relationships.py
"""Approved concept-relationship map and judge relationship enforcement.

The judge prompt asks the model to use only approved synonyms and
abbreviations, but a prompt rule is not an enforcement mechanism. This module
re-checks every claimed relationship against the curated map and downgrades
whatever the map does not support, so a hallucinated equivalence cannot buy
the credit that a real one earns.

Direction is the point. A specific child supports a broad parent
(PyTorch -> deep-learning framework), while the reverse never holds
(deep learning -/-> PyTorch). Inverted and explicitly unsupported pairs are
forced to UNSUPPORTED_INFERENCE, which scores 0.00.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Optional

from evidence_engine.models import Relationship

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# Relationships whose credit the curated map is responsible for authorising.
# Anything above CONTEXTUAL_IMPLICATION must be backed by the map.
_GATED = {
    Relationship.EQUIVALENT_SYNONYM,
    Relationship.ABBREVIATION_EQUIVALENT,
    Relationship.CHILD_SUPPORTS_PARENT,
}

_DASHES = re.compile(r"[‐-―−]")
_WS = re.compile(r"\s+")
# Short tokens ("ml", "ind", "crf") must match on word boundaries; a substring
# test would fire on "html", "index" and "scrf".
_SHORT_TOKEN_CHARS = 5


def normalize_concept(text: str) -> str:
    """Lowercase, unify dash characters, collapse whitespace."""
    return _WS.sub(" ", _DASHES.sub("-", text or "").lower()).strip()


class RelationMap:
    """Curated concept relations with transitive child_of closure."""

    def __init__(self, payload: dict):
        self.raw: dict = payload
        self.version: str = payload.get("version", "unknown")
        self._synonym_class: dict[str, int] = {}
        self._abbrev: dict[str, set[str]] = {}
        self._parents: dict[str, set[str]] = {}
        self._adjacent: dict[str, set[str]] = {}
        self._unsupported: set[tuple[str, str]] = set()
        self._concepts: set[str] = set()

        synonym_pairs: list[tuple[str, str]] = []
        for entry in payload.get("approved", []):
            src = normalize_concept(entry.get("from", ""))
            dst = normalize_concept(entry.get("to", ""))
            relation = (entry.get("relation") or "").strip().lower()
            if not src or not dst:
                continue
            self._concepts.update((src, dst))
            if relation == "synonym":
                synonym_pairs.append((src, dst))
            elif relation == "abbreviation":
                self._abbrev.setdefault(src, set()).add(dst)
                self._abbrev.setdefault(dst, set()).add(src)
            elif relation == "child_of":
                self._parents.setdefault(src, set()).add(dst)
            elif relation == "adjacent":
                self._adjacent.setdefault(src, set()).add(dst)
                self._adjacent.setdefault(dst, set()).add(src)
            elif relation == "unsupported":
                self._unsupported.add((src, dst))

        self._build_synonym_classes(synonym_pairs)
        self._ancestors = self._build_ancestors()
        # Longest first so "clinical protocol authoring" wins over "protocol".
        self._ordered = sorted(self._concepts, key=len, reverse=True)

    # -- construction ----------------------------------------------------
    def _build_synonym_classes(self, pairs: list[tuple[str, str]]) -> None:
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a, b in pairs:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
        roots: dict[str, int] = {}
        for concept in parent:
            root = find(concept)
            self._synonym_class[concept] = roots.setdefault(root, len(roots))

    def _build_ancestors(self) -> dict[str, set[str]]:
        """Transitive closure of child_of, so PyTorch reaches deep learning."""
        resolved: dict[str, set[str]] = {}

        def walk(node: str, seen: frozenset[str]) -> set[str]:
            if node in resolved:
                return resolved[node]
            if node in seen:  # cyclic map entry — stop rather than recurse
                return set()
            out: set[str] = set()
            for parent in self._parents.get(node, ()):
                out.add(parent)
                out |= walk(parent, seen | {node})
            resolved[node] = out
            return out

        for node in list(self._parents):
            walk(node, frozenset())
        return resolved

    # -- queries ---------------------------------------------------------
    def concepts_in(self, text: str) -> set[str]:
        """Curated concepts mentioned in text, longest match first."""
        haystack = normalize_concept(text)
        if not haystack:
            return set()
        found: set[str] = set()
        for concept in self._ordered:
            if len(concept) <= _SHORT_TOKEN_CHARS and " " not in concept:
                if re.search(rf"(?<![a-z0-9]){re.escape(concept)}(?![a-z0-9])",
                             haystack):
                    found.add(concept)
            elif concept in haystack:
                found.add(concept)
        return found

    def are_synonyms(self, a: str, b: str) -> bool:
        ca = self._synonym_class.get(normalize_concept(a))
        cb = self._synonym_class.get(normalize_concept(b))
        return ca is not None and ca == cb

    def are_abbreviations(self, a: str, b: str) -> bool:
        na, nb = normalize_concept(a), normalize_concept(b)
        return nb in self._abbrev.get(na, set())

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """True when descendant child_of ... ancestor (transitively)."""
        return normalize_concept(ancestor) in self._ancestors.get(
            normalize_concept(descendant), set())

    def are_adjacent(self, a: str, b: str) -> bool:
        na, nb = normalize_concept(a), normalize_concept(b)
        if nb in self._adjacent.get(na, set()):
            return True
        # Siblings under a shared parent are adjacent, not equivalent.
        shared = self._ancestors.get(na, set()) & self._ancestors.get(nb, set())
        return bool(shared) and na != nb

    def is_explicitly_unsupported(self, source: str, target: str) -> bool:
        return (normalize_concept(source), normalize_concept(target)) \
            in self._unsupported


def load_relation_map(path: Optional[str] = None) -> RelationMap:
    p = Path(path) if path else _CONFIG_DIR / "relationships_v0_1.json"
    return RelationMap(json.loads(p.read_text()))


def _requirement_terms(requirement) -> list[str]:
    terms = [requirement.normalized_text, *requirement.concepts]
    return [t for t in terms if t]


def _covers(rc: str, span_concepts: Iterable[str], rmap: RelationMap) -> bool:
    """True when some span concept is at least as specific as rc.

    A bullet that names both a specific concept and its broader category
    ("oncology and immunology therapeutic areas") is covered by the specific
    one, so the breadth of its neighbours must not disqualify it.
    """
    for sc in span_concepts:
        if sc == rc or rmap.are_synonyms(sc, rc) or rmap.are_abbreviations(sc, rc):
            return True
        if rmap.is_ancestor(rc, sc):  # sc is a descendant of rc
            return True
    return False


def _inverted_or_forbidden(
    req_concepts: Iterable[str], span_concepts: Iterable[str], rmap: RelationMap
) -> Optional[str]:
    """Reason string when span concepts cannot support the requirement at all.

    Only fires for a requirement concept nothing in the span covers; otherwise
    a broad neighbouring term would veto evidence that is genuinely present.
    """
    span_concepts = list(span_concepts)
    for rc in req_concepts:
        if _covers(rc, span_concepts, rmap):
            continue
        for sc in span_concepts:
            if rmap.is_explicitly_unsupported(sc, rc):
                return f"'{sc}' is an explicitly unsupported inference for '{rc}'"
            # Requirement is the specific child; the span only has the parent.
            if rmap.is_ancestor(sc, rc):
                return (f"'{sc}' is broader than the required '{rc}'; a parent "
                        "concept does not establish a child requirement")
    return None


def _sibling_substitution(
    req_concepts: Iterable[str], span_concepts: Iterable[str], rmap: RelationMap
) -> Optional[str]:
    """Reason string when the span offers a sibling instead of what was asked.

    Two concepts under a shared parent — Phase II and Phase III, Medidata Rave
    and Oracle InForm — are adjacent work, not interchangeable work. The
    candidate has done something genuinely related, which is worth partial
    credit and no more.

    Skipped entirely when the requirement concept is itself covered, so a span
    naming both ("Led Phase II and Phase III studies") keeps full credit.
    """
    span_concepts = list(span_concepts)
    for rc in req_concepts:
        if _covers(rc, span_concepts, rmap):
            continue
        for sc in span_concepts:
            if rmap.are_adjacent(sc, rc):
                return (f"'{sc}' is adjacent to the required '{rc}', not the "
                        "same requirement; capped at transferable evidence")
    return None


def verify_relationship(
    relationship: Relationship,
    requirement,
    span_text: str,
    rmap: RelationMap,
) -> tuple[Relationship, Optional[str]]:
    """Re-check a claimed relationship against the curated map.

    Returns the enforced relationship and a note when it was changed. The map
    is authoritative for the three high-credit relationships; anything it does
    not support is downgraded rather than dropped, so an incomplete map costs
    credit instead of erasing genuine evidence.
    """
    if relationship in (Relationship.UNDETERMINED, Relationship.CONTRADICTORY,
                        Relationship.NONE, Relationship.UNSUPPORTED_INFERENCE):
        return relationship, None

    req_terms = _requirement_terms(requirement)
    req_concepts = set()
    for term in req_terms:
        req_concepts |= rmap.concepts_in(term)
    span_concepts = rmap.concepts_in(span_text)

    forbidden = _inverted_or_forbidden(req_concepts, span_concepts, rmap)
    if forbidden:
        return Relationship.UNSUPPORTED_INFERENCE, forbidden

    # Sibling concepts are related work, not the same work. Phase II experience
    # is real evidence toward a Phase III requirement, but it is not that
    # requirement met. This runs for every relationship, not only the gated
    # ones: a claimed EXACT_EXPLICIT was previously never checked against the
    # map at all, so sibling confusion passed at full credit.
    sibling = _sibling_substitution(req_concepts, span_concepts, rmap)
    if sibling and relationship != Relationship.ADJACENT_TRANSFERABLE:
        return Relationship.ADJACENT_TRANSFERABLE, sibling

    if relationship not in _GATED:
        return relationship, None

    span_norm = normalize_concept(span_text)
    approved = False
    for rc in req_concepts:
        for sc in span_concepts:
            if relationship == Relationship.ABBREVIATION_EQUIVALENT:
                approved = rmap.are_abbreviations(sc, rc)
            elif relationship == Relationship.EQUIVALENT_SYNONYM:
                approved = rmap.are_synonyms(sc, rc)
            else:  # CHILD_SUPPORTS_PARENT: span concept is the specific child
                approved = rmap.is_ancestor(rc, sc)
            if approved:
                return relationship, None

    # Exact restatement needs no map entry — the words themselves are proof.
    if relationship != Relationship.ABBREVIATION_EQUIVALENT and any(
            normalize_concept(t) and normalize_concept(t) in span_norm
            for t in req_terms):
        return relationship, None

    if relationship == Relationship.CHILD_SUPPORTS_PARENT:
        return (Relationship.ADJACENT_TRANSFERABLE,
                "no approved child_of path; downgraded to adjacent")
    return (Relationship.CONTEXTUAL_IMPLICATION,
            f"no approved {relationship.value.lower()} entry; "
            "downgraded to contextual implication")
