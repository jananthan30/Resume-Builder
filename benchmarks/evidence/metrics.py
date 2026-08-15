# benchmarks/evidence/metrics.py
"""Ranking and requirement-level metrics for the evidence benchmark.

Implemented directly rather than pulled from scipy/sklearn so the benchmark
runs anywhere the engine runs, and so tie handling and empty-input behaviour
are explicit rather than inherited.

Ranking metrics take graded relevance labels (0 = no fit ... 4 = strong fit),
matching the spec's annotation scale.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

# A pair is "genuinely strong" at 3 (good) or above, per the label scale.
STRONG_LABEL = 3


def _ordered_labels(labels: Sequence[float], scores: Sequence[float]) -> list[float]:
    """Labels sorted by descending predicted score; ties broken by lower label.

    Pessimistic tie-breaking keeps a system from being flattered by producing
    identical scores for everything.
    """
    paired = sorted(zip(scores, labels), key=lambda t: (-t[0], t[1]))
    return [label for _, label in paired]


def dcg(labels: Sequence[float], k: int) -> float:
    return sum((2.0 ** label - 1.0) / math.log2(rank + 2)
               for rank, label in enumerate(labels[:k]))


def ndcg_at_k(labels: Sequence[float], scores: Sequence[float], k: int) -> float:
    """Normalized discounted cumulative gain. 0.0 when no positive label exists."""
    if not labels:
        return 0.0
    ideal = dcg(sorted(labels, reverse=True), k)
    if ideal == 0:
        return 0.0
    return dcg(_ordered_labels(labels, scores), k) / ideal


def precision_at_k(labels: Sequence[float], scores: Sequence[float], k: int,
                   threshold: float = STRONG_LABEL) -> float:
    """Share of the top k that is genuinely strong."""
    if not labels or k <= 0:
        return 0.0
    top = _ordered_labels(labels, scores)[:k]
    return sum(1 for label in top if label >= threshold) / len(top)


def recall_at_k(labels: Sequence[float], scores: Sequence[float], k: int,
                threshold: float = STRONG_LABEL) -> float:
    """Share of all strong candidates recovered in the top k."""
    total_strong = sum(1 for label in labels if label >= threshold)
    if total_strong == 0:
        return 0.0
    top = _ordered_labels(labels, scores)[:k]
    return sum(1 for label in top if label >= threshold) / total_strong


def pairwise_ranking_accuracy(labels: Sequence[float],
                              scores: Sequence[float]) -> float:
    """Share of genuinely-ordered pairs the system orders correctly.

    NDCG uses exponential gain, so one correctly-ranked top candidate can carry
    a short list to ~0.95 while a keyword-stuffed resume still outranks a good
    one further down. This metric weighs every comparable pair equally and is
    therefore the one that actually detects that failure. Ties score 0.5.
    """
    comparable = 0
    concordant = 0.0
    for i in range(len(labels)):
        for j in range(len(labels)):
            if labels[i] <= labels[j]:
                continue
            comparable += 1
            if scores[i] > scores[j]:
                concordant += 1.0
            elif scores[i] == scores[j]:
                concordant += 0.5
    return concordant / comparable if comparable else 0.0


def adversarial_inversions(labels: Sequence[float], scores: Sequence[float],
                           weak_at_most: float = 1,
                           strong_at_least: float = STRONG_LABEL) -> int:
    """Count weak resumes that outrank strong ones.

    The spec's counterfactual tests demand A >> B, where B is keyword stuffing.
    Any inversion here is a direct instance of the failure the evidence engine
    exists to prevent, so the benchmark counts them rather than averaging them
    away.
    """
    return sum(
        1
        for i in range(len(labels)) if labels[i] <= weak_at_most
        for j in range(len(labels))
        if labels[j] >= strong_at_least and scores[i] >= scores[j]
    )


def _average_ranks(values: Sequence[float]) -> list[float]:
    """Ranks with ties averaged, as Spearman requires."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for position in range(i, j + 1):
            ranks[order[position]] = shared
        i = j + 1
    return ranks


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    """Rank correlation. 0.0 when either side is constant (undefined)."""
    if len(a) != len(b) or len(a) < 2:
        return 0.0
    ra, rb = _average_ranks(a), _average_ranks(b)
    mean_a, mean_b = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - mean_a) ** 2 for x in ra)
                    * sum((y - mean_b) ** 2 for y in rb))
    return num / den if den else 0.0


def precision_recall_f1(
    predicted: Iterable, gold: Iterable
) -> tuple[float, float, float]:
    """Set-based precision, recall and F1. Empty predictions score 0."""
    predicted, gold = set(predicted), set(gold)
    if not predicted and not gold:
        return 1.0, 1.0, 1.0
    true_positive = len(predicted & gold)
    precision = true_positive / len(predicted) if predicted else 0.0
    recall = true_positive / len(gold) if gold else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if precision + recall else 0.0)
    return precision, recall, f1


def macro_f1(predicted: Sequence[str], gold: Sequence[str]) -> float:
    """Unweighted mean per-class F1 over every class present in either list."""
    if len(predicted) != len(gold) or not gold:
        return 0.0
    scores = []
    for label in sorted(set(gold) | set(predicted)):
        tp = sum(1 for p, g in zip(predicted, gold) if p == g == label)
        fp = sum(1 for p, g in zip(predicted, gold) if p == label != g)
        fn = sum(1 for p, g in zip(predicted, gold) if g == label != p)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        scores.append(2 * precision * recall / (precision + recall)
                      if precision + recall else 0.0)
    return sum(scores) / len(scores)


def label_precision(predicted: Sequence[str], gold: Sequence[str],
                    label: str) -> float:
    """Precision for one class. Used for FAIL, where false positives are costly.

    Returns 1.0 when the label is never predicted: a system that never claims
    ineligibility is never wrong about it.
    """
    claimed = [(p, g) for p, g in zip(predicted, gold) if p == label]
    if not claimed:
        return 1.0
    return sum(1 for _, g in claimed if g == label) / len(claimed)


def label_recall(predicted: Sequence[str], gold: Sequence[str],
                 label: str) -> float:
    actual = [(p, g) for p, g in zip(predicted, gold) if g == label]
    if not actual:
        return 1.0
    return sum(1 for p, _ in actual if p == label) / len(actual)


def token_f1(predicted_text: str, gold_text: str) -> float:
    """Token-level F1 between two spans, for partial-credit span scoring."""
    predicted = predicted_text.lower().split()
    gold = gold_text.lower().split()
    if not predicted and not gold:
        return 1.0
    if not predicted or not gold:
        return 0.0
    overlap = 0
    remaining = list(gold)
    for token in predicted:
        if token in remaining:
            remaining.remove(token)
            overlap += 1
    if overlap == 0:
        return 0.0
    precision = overlap / len(predicted)
    recall = overlap / len(gold)
    return 2 * precision * recall / (precision + recall)
