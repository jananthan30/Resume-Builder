# benchmarks/evidence/runner.py
"""Run the evidence benchmark and check the spec's launch gates.

The spec is explicit that the engine should not ship because its output looks
smarter — it should beat a keyword baseline on a labelled benchmark. This
module produces that comparison, and refuses to call an improvement meaningful
without a paired significance test.

Usage:
    python -m benchmarks.evidence.runner                    # offline systems
    python -m benchmarks.evidence.runner --with-llm         # adds E and F
    python -m benchmarks.evidence.runner --dataset my.json --json out.json
"""
from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from benchmarks.evidence.baselines import (
    Corpus, REFERENCE_SYSTEM, build_systems,
)
from benchmarks.evidence.dataset import BenchmarkDataset, load_dataset
from benchmarks.evidence.metrics import (
    adversarial_inversions, label_precision, macro_f1, ndcg_at_k,
    pairwise_ranking_accuracy, precision_at_k, precision_recall_f1,
    recall_at_k, spearman, token_f1,
)

# Spec §"Success thresholds for the MVP". Engineering targets, not industry
# benchmarks — they are expected to move after the first labelled dataset.
LAUNCH_GATES = {
    "evidence_span_precision": 0.90,
    "must_have_fail_precision": 0.95,
    "requirement_classification_f1": 0.85,
}
BOOTSTRAP_SAMPLES = 2000
SIGNIFICANCE_ALPHA = 0.05


@dataclass
class RankingResult:
    system: str
    ndcg_at_5: float = 0.0
    ndcg_at_10: float = 0.0
    precision_at_5: float = 0.0
    recall_at_10: float = 0.0
    spearman: float = 0.0
    pairwise_accuracy: float = 0.0
    adversarial_inversions: int = 0
    per_job_ndcg_at_10: list[float] = field(default_factory=list)
    per_job_pairwise_accuracy: list[float] = field(default_factory=list)


@dataclass
class RequirementResult:
    """Requirement-level quality. Only computed over annotated pairs."""
    annotated_jobs: int = 0
    annotated_pairs: int = 0
    requirement_classification_f1: float = 0.0
    must_have_precision: float = 0.0
    must_have_recall: float = 0.0
    must_have_fail_precision: float = 0.0
    hard_gate_accuracy: float = 0.0
    evidence_span_precision: float = 0.0
    evidence_span_recall: float = 0.0
    evidence_span_token_f1: float = 0.0


def _score_pairs(dataset: BenchmarkDataset,
                 scorer: Callable[..., float]) -> dict[tuple[str, str], float]:
    scores: dict[tuple[str, str], float] = {}
    for job in dataset.jobs:
        pairs = dataset.pairs_for(job.job_id)
        corpus = Corpus([dataset.resume(p.resume_id).text for p in pairs])
        for pair in pairs:
            resume_text = dataset.resume(pair.resume_id).text
            scores[(pair.job_id, pair.resume_id)] = scorer(
                resume_text, job.text, corpus)
    return scores


def evaluate_ranking(dataset: BenchmarkDataset, system: str,
                     scores: dict[tuple[str, str], float]) -> RankingResult:
    per_job = {"ndcg5": [], "ndcg10": [], "p5": [], "r10": [], "rho": [],
               "pairwise": []}
    per_job_ndcg10: list[float] = []
    inversions = 0
    for job in dataset.jobs:
        pairs = dataset.pairs_for(job.job_id)
        labels = [p.label for p in pairs]
        predicted = [scores[(p.job_id, p.resume_id)] for p in pairs]
        ndcg10 = ndcg_at_k(labels, predicted, 10)
        per_job["ndcg5"].append(ndcg_at_k(labels, predicted, 5))
        per_job["ndcg10"].append(ndcg10)
        per_job["p5"].append(precision_at_k(labels, predicted, 5))
        per_job["r10"].append(recall_at_k(labels, predicted, 10))
        per_job["rho"].append(spearman(predicted, labels))
        per_job["pairwise"].append(pairwise_ranking_accuracy(labels, predicted))
        per_job_ndcg10.append(ndcg10)
        inversions += adversarial_inversions(labels, predicted)

    def mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    return RankingResult(
        system=system,
        ndcg_at_5=mean(per_job["ndcg5"]),
        ndcg_at_10=mean(per_job["ndcg10"]),
        precision_at_5=mean(per_job["p5"]),
        recall_at_10=mean(per_job["r10"]),
        spearman=mean(per_job["rho"]),
        pairwise_accuracy=mean(per_job["pairwise"]),
        adversarial_inversions=inversions,
        per_job_ndcg_at_10=per_job_ndcg10,
        per_job_pairwise_accuracy=per_job["pairwise"],
    )


def paired_bootstrap(treatment: list[float], control: list[float],
                     samples: int = BOOTSTRAP_SAMPLES,
                     seed: int = 20260814) -> dict:
    """Paired bootstrap over per-job scores.

    Reported instead of a bare difference because a handful of jobs can easily
    produce a large-looking gap that would not survive resampling.
    """
    if len(treatment) != len(control) or not treatment:
        return {"observed_difference": 0.0, "p_value": 1.0,
                "significant": False, "jobs": 0}
    differences = [t - c for t, c in zip(treatment, control)]
    observed = sum(differences) / len(differences)
    rng = random.Random(seed)
    n = len(differences)
    centred = [d - observed for d in differences]
    at_least_as_extreme = 0
    for _ in range(samples):
        resampled = [centred[rng.randrange(n)] for _ in range(n)]
        if abs(sum(resampled) / n) >= abs(observed):
            at_least_as_extreme += 1
    p_value = (at_least_as_extreme + 1) / (samples + 1)
    return {
        "observed_difference": observed,
        "p_value": p_value,
        "significant": bool(p_value < SIGNIFICANCE_ALPHA and observed > 0),
        "jobs": n,
    }


def evaluate_requirements(dataset: BenchmarkDataset, llm,
                          now_index: Optional[int] = None,
                          cache=None) -> RequirementResult:
    """Requirement-, gate- and span-level quality against gold annotations."""
    import tempfile

    from evidence_engine.engine import match_resume_to_job
    from evidence_engine.parsing.dates import NOW_INDEX

    predicted_importance: list[str] = []
    gold_importance: list[str] = []
    predicted_eligibility: list[str] = []
    gold_eligibility: list[str] = []
    span_precisions: list[float] = []
    span_recalls: list[float] = []
    span_token_f1s: list[float] = []
    annotated_pairs = 0
    annotated_jobs = {job.job_id for job in dataset.jobs if job.gold_requirements}

    for pair in dataset.pairs:
        job = dataset.job(pair.job_id)
        if not job.gold_requirements and not pair.gold_evidence:
            continue
        resume_text = dataset.resume(pair.resume_id).text
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resume.md"
            path.write_text(resume_text, encoding="utf-8")
            result = match_resume_to_job(
                str(path), job.text, llm=llm,
                now_index=now_index if now_index is not None else NOW_INDEX,
                cache=cache)
        if result.status != "OK":
            continue
        annotated_pairs += 1

        # Requirement importance: align gold to prediction by nearest text.
        for gold in job.gold_requirements:
            best = max(
                result.requirements,
                key=lambda r: token_f1(r.normalized_text, gold.normalized_text),
                default=None)
            if best is None:
                continue
            predicted_importance.append(best.importance.value)
            gold_importance.append(gold.importance)

        if pair.gold_eligibility:
            predicted_eligibility.append(result.eligibility.status.value)
            gold_eligibility.append(pair.gold_eligibility)

        # Evidence spans: did the engine cite what a reviewer would cite?
        spans_by_id = {s.evidence_span_id: s for s in result.evidence_spans}
        for gold_evidence in pair.gold_evidence:
            match = max(
                result.matches,
                key=lambda m: token_f1(
                    next((r.normalized_text for r in result.requirements
                          if r.requirement_id == m.requirement_id), ""),
                    gold_evidence.requirement_text),
                default=None)
            if match is None:
                continue
            cited = [spans_by_id[sid].text for sid in match.evidence_span_ids
                     if sid in spans_by_id]
            expected = gold_evidence.span_texts
            hits = [c for c in cited
                    if any(token_f1(c, e) >= 0.6 for e in expected)]
            precision, recall, _ = precision_recall_f1(
                range(len(hits)), range(len(expected)))
            span_precisions.append(
                len(hits) / len(cited) if cited else (1.0 if not expected else 0.0))
            span_recalls.append(recall if expected else 1.0)
            span_token_f1s.append(
                max((token_f1(c, e) for c in cited for e in expected),
                    default=1.0 if not expected and not cited else 0.0))

    def mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    return RequirementResult(
        annotated_jobs=len(annotated_jobs),
        annotated_pairs=annotated_pairs,
        requirement_classification_f1=macro_f1(predicted_importance,
                                               gold_importance),
        must_have_precision=label_precision(predicted_importance,
                                            gold_importance, "MUST_HAVE"),
        must_have_recall=_label_recall(predicted_importance, gold_importance,
                                       "MUST_HAVE"),
        must_have_fail_precision=label_precision(predicted_eligibility,
                                                 gold_eligibility, "FAIL"),
        hard_gate_accuracy=_accuracy(predicted_eligibility, gold_eligibility),
        evidence_span_precision=mean(span_precisions),
        evidence_span_recall=mean(span_recalls),
        evidence_span_token_f1=mean(span_token_f1s),
    )


def _label_recall(predicted: list[str], gold: list[str], label: str) -> float:
    from benchmarks.evidence.metrics import label_recall
    return label_recall(predicted, gold, label)


def _accuracy(predicted: list[str], gold: list[str]) -> float:
    if not gold:
        return 0.0
    return sum(1 for p, g in zip(predicted, gold) if p == g) / len(gold)


def check_gates(requirements: RequirementResult, significance: dict,
                rankings: Optional[list[RankingResult]] = None) -> dict[str, dict]:
    """Evaluate the launch gates. A gate with no data is 'no data', not a pass."""
    measured = {
        "evidence_span_precision": (
            requirements.evidence_span_precision,
            requirements.annotated_pairs > 0),
        "must_have_fail_precision": (
            requirements.must_have_fail_precision,
            requirements.annotated_pairs > 0),
        "requirement_classification_f1": (
            requirements.requirement_classification_f1,
            requirements.annotated_jobs > 0),
    }
    gates = {
        name: {
            "value": value,
            "threshold": LAUNCH_GATES[name],
            "status": ("PASS" if value >= LAUNCH_GATES[name] else "FAIL")
            if has_data else "NO DATA",
        }
        for name, (value, has_data) in measured.items()
    }
    gates["ndcg_at_10_beats_keywords"] = {
        "value": significance.get("observed_difference", 0.0),
        "threshold": "significant improvement (p < 0.05)",
        "status": "PASS" if significance.get("significant") else (
            "NO DATA" if not significance.get("jobs") else "FAIL"),
    }

    # Keyword-stuffing invariance, stated as a ranking outcome: no weak resume
    # may outrank a strong one. This is the gate NDCG is too blunt to enforce.
    engine = next((r for r in (rankings or [])
                   if r.system == "F_evidence_engine"), None)
    gates["no_adversarial_inversions"] = {
        "value": float(engine.adversarial_inversions) if engine else 0.0,
        "threshold": "0 weak resumes outranking strong ones",
        "status": ("PASS" if engine.adversarial_inversions == 0 else "FAIL")
        if engine else "NO DATA",
    }
    return gates


def render_report(dataset: BenchmarkDataset, rankings: list[RankingResult],
                  requirements: RequirementResult, significance: dict,
                  gates: dict) -> str:
    lines = [
        f"EVIDENCE BENCHMARK — {dataset.name} v{dataset.version}",
        f"{len(dataset.jobs)} jobs, {len(dataset.resumes)} resumes, "
        f"{len(dataset.pairs)} labelled pairs",
        "",
        f"{'system':<24}{'NDCG@5':>9}{'NDCG@10':>9}{'P@5':>8}{'R@10':>8}"
        f"{'rho':>8}{'pair-acc':>10}{'inversions':>12}",
        "-" * 88,
    ]
    for r in rankings:
        lines.append(
            f"{r.system:<24}{r.ndcg_at_5:>9.3f}{r.ndcg_at_10:>9.3f}"
            f"{r.precision_at_5:>8.3f}{r.recall_at_10:>8.3f}{r.spearman:>8.3f}"
            f"{r.pairwise_accuracy:>10.3f}{r.adversarial_inversions:>12d}")
    lines += [
        "",
        "  pair-acc   share of genuinely-ordered resume pairs ranked correctly",
        "  inversions weak resumes (label <= 1) outranking strong ones (>= 3);",
        "             every one of these is a keyword-stuffing style failure",
    ]
    if all(len(dataset.pairs_for(j.job_id)) < 8 for j in dataset.jobs):
        lines.append("  note: NDCG saturates on short candidate lists — read "
                     "pair-acc and inversions first")

    lines += ["", "REQUIREMENT-LEVEL QUALITY"]
    if requirements.annotated_pairs == 0:
        lines.append("  no requirement-level annotations in this dataset")
    else:
        lines += [
            f"  annotated: {requirements.annotated_jobs} jobs, "
            f"{requirements.annotated_pairs} pairs",
            f"  requirement classification macro-F1  "
            f"{requirements.requirement_classification_f1:.3f}",
            f"  must-have precision / recall         "
            f"{requirements.must_have_precision:.3f} / "
            f"{requirements.must_have_recall:.3f}",
            f"  hard-gate accuracy                   "
            f"{requirements.hard_gate_accuracy:.3f}",
            f"  evidence-span precision / recall     "
            f"{requirements.evidence_span_precision:.3f} / "
            f"{requirements.evidence_span_recall:.3f}",
            f"  evidence-span token F1               "
            f"{requirements.evidence_span_token_f1:.3f}",
        ]

    lines += ["", "NDCG@10 VERSUS THE KEYWORD BASELINE"]
    if significance.get("jobs"):
        lines.append(
            f"  difference {significance['observed_difference']:+.3f} over "
            f"{significance['jobs']} jobs, p = {significance['p_value']:.3f} "
            f"({'significant' if significance['significant'] else 'not significant'})")
    else:
        lines.append("  the evidence engine was not run (use --with-llm)")

    lines += ["", "LAUNCH GATES"]
    for name, gate in gates.items():
        threshold = gate["threshold"]
        shown = threshold if isinstance(threshold, str) else f">= {threshold}"
        lines.append(f"  [{gate['status']:>7}] {name:<34}"
                     f"{gate['value']:.3f}  ({shown})")
    lines += ["", "Scores measure evidence coverage. They are not a probability "
                  "of being hired."]
    return "\n".join(lines)


def run(dataset: BenchmarkDataset, llm=None, now_index: Optional[int] = None,
        cache=None) -> dict:
    problems = dataset.validate_integrity()
    if problems:
        raise ValueError("dataset integrity errors: " + "; ".join(problems))

    systems = build_systems(llm, now_index, cache)
    rankings: list[RankingResult] = []
    for name, scorer in systems.items():
        rankings.append(evaluate_ranking(dataset, name,
                                         _score_pairs(dataset, scorer)))

    reference = next(r for r in rankings if r.system == REFERENCE_SYSTEM)
    engine = next((r for r in rankings if r.system == "F_evidence_engine"), None)
    significance = (paired_bootstrap(engine.per_job_ndcg_at_10,
                                     reference.per_job_ndcg_at_10)
                    if engine else {"observed_difference": 0.0, "p_value": 1.0,
                                    "significant": False, "jobs": 0})

    requirements = (evaluate_requirements(dataset, llm, now_index, cache)
                    if llm is not None else RequirementResult())
    gates = check_gates(requirements, significance, rankings)
    return {
        "dataset": {"name": dataset.name, "version": dataset.version,
                    "jobs": len(dataset.jobs), "resumes": len(dataset.resumes),
                    "pairs": len(dataset.pairs)},
        "rankings": [asdict(r) for r in rankings],
        "requirements": asdict(requirements),
        "significance": significance,
        "gates": gates,
        "report": render_report(dataset, rankings, requirements, significance,
                                gates),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Evidence matching benchmark")
    ap.add_argument("--dataset", help="Path to a benchmark dataset JSON")
    ap.add_argument("--with-llm", action="store_true",
                    help="Include systems E and F (requires an API key)")
    ap.add_argument("--json", dest="json_out", help="Write full results as JSON")
    ap.add_argument("--fail-on-gate", action="store_true",
                    help="Exit non-zero when any launch gate fails")
    ap.add_argument("--cache-dir",
                    help="Reuse engine results across runs (content-addressed, "
                         "so a policy or prompt change is still a miss)")
    args = ap.parse_args()

    llm = None
    if args.with_llm:
        from evidence_engine.llm import AnthropicClient
        llm = AnthropicClient()

    cache = None
    if args.cache_dir:
        from evidence_engine.audit import ResultCache
        cache = ResultCache(args.cache_dir)
    outcome = run(load_dataset(args.dataset), llm=llm, cache=cache)
    print(outcome["report"])

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(outcome, indent=2),
                                       encoding="utf-8")
    if args.fail_on_gate and any(g["status"] == "FAIL"
                                 for g in outcome["gates"].values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
