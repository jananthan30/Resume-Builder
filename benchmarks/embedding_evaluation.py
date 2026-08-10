"""
Embedding Model Evaluation Benchmark for Resume-JD Matching

Evaluates 3 embedding models on resume-to-job-description similarity tasks:
  1. all-MiniLM-L6-v2       (current baseline in ats_scorer.py)
  2. jjzha/jobbert-base-cased (domain-adapted for labor market data)
  3. BAAI/bge-small-en-v1.5  (high-quality general, small footprint)

Metrics:
  - Mean cosine similarity for matching, non-matching, and partial pairs
  - Separation gap (matching_mean - non_matching_mean)
  - Discriminability (partial placed between matching and non-matching)
  - Inference time per pair (ms)
  - Model size on disk (MB)

Usage:
  python benchmarks/embedding_evaluation.py
"""

import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Test data: resume snippet / JD snippet pairs
# ---------------------------------------------------------------------------

MATCHING_PAIRS: List[Tuple[str, str]] = [
    # Clinical research
    (
        "Clinical research coordinator with 5 years managing Phase II-III oncology trials, "
        "proficient in EDC systems including Medidata Rave, and GCP compliance. Experienced "
        "in IRB submissions, informed consent processes, and adverse event reporting.",
        "Seeking Clinical Research Coordinator for Phase III oncology trials. Must have EDC "
        "experience (Medidata preferred) and GCP certification. IRB submission experience required."
    ),
    # Software engineering
    (
        "Senior software engineer with expertise in Python, React, AWS Lambda, and "
        "microservices architecture. Led migration of monolith to event-driven services "
        "reducing latency by 40%.",
        "Senior Software Engineer needed. Python, React, AWS experience required. "
        "Microservices design and cloud-native architecture. Experience with event-driven "
        "systems preferred."
    ),
    # Finance / investment banking
    (
        "Investment banking analyst with 3 years at Goldman Sachs covering healthcare M&A. "
        "Built financial models, conducted due diligence on $2B+ transactions, and prepared "
        "pitch books for C-suite presentations.",
        "Investment Banking Analyst - Healthcare M&A group. Responsibilities include financial "
        "modeling, due diligence, pitch book preparation. Experience at bulge bracket bank preferred."
    ),
    # Data science
    (
        "Data scientist specializing in NLP and recommendation systems. Built transformer-based "
        "models for product search improving click-through rate by 25%. Proficient in PyTorch, "
        "TensorFlow, and Spark MLlib.",
        "Data Scientist - NLP & Recommendations. Build ML models for search relevance and "
        "personalization. Requires PyTorch or TensorFlow, Spark experience. NLP background essential."
    ),
    # Healthcare / physician
    (
        "Board-certified internal medicine physician with 8 years of clinical experience in "
        "academic medical centers. Published researcher in cardiovascular outcomes. Experienced "
        "in quality improvement initiatives and EMR optimization.",
        "Internal Medicine Physician wanted for academic medical center. Board certification "
        "required. Research experience and quality improvement background preferred. EMR proficiency."
    ),
    # Consulting
    (
        "Management consultant at McKinsey with 4 years in operations practice. Led cost "
        "reduction engagements saving clients $50M+. Expertise in supply chain optimization "
        "and lean manufacturing methodologies.",
        "Management Consultant - Operations Practice. Drive cost reduction and supply chain "
        "transformation for Fortune 500 clients. Top-tier consulting experience required."
    ),
]

NON_MATCHING_PAIRS: List[Tuple[str, str]] = [
    # Clinical research resume vs Software engineering JD
    (
        "Clinical research coordinator with Phase III trial experience in oncology. "
        "Managed regulatory submissions and site monitoring visits.",
        "Senior Software Engineer needed for React and Node.js development. "
        "Must have CI/CD pipeline experience and containerization with Docker."
    ),
    # Software engineer resume vs Finance JD
    (
        "Full-stack developer with 6 years building web applications in Django and React. "
        "Deployed microservices on Kubernetes with automated scaling.",
        "Investment Banking Associate for leveraged finance group. DCF modeling, "
        "LBO analysis, and credit agreement review. CFA preferred."
    ),
    # Finance resume vs Healthcare JD
    (
        "Equity research analyst covering technology sector. Built DCF and comparable "
        "company models. Published 50+ research notes for institutional investors.",
        "Registered Nurse for intensive care unit. BLS/ACLS certification required. "
        "Experience with ventilator management and hemodynamic monitoring."
    ),
    # Healthcare resume vs Data science JD
    (
        "Emergency medicine physician with 10 years of trauma center experience. "
        "Board certified in emergency medicine with advanced airway management skills.",
        "Machine Learning Engineer to build recommendation systems. Requires Python, "
        "TensorFlow, distributed computing. PhD in ML/AI preferred."
    ),
    # Consulting resume vs Clinical research JD
    (
        "Strategy consultant specializing in digital transformation for retail clients. "
        "Led market entry analysis and competitive benchmarking projects.",
        "Clinical Data Manager for Phase I-III trials. Proficiency in SAS, CDISC standards, "
        "and database design. CDMP certification preferred."
    ),
    # Data science resume vs Consulting JD
    (
        "Computer vision researcher with publications in CVPR and NeurIPS. Built real-time "
        "object detection pipelines processing 30 FPS on edge devices.",
        "Senior Management Consultant for healthcare practice. Hospital operations, "
        "revenue cycle management, and payer strategy. MBA from top program required."
    ),
]

PARTIAL_MATCHING_PAIRS: List[Tuple[str, str]] = [
    # Clinical research resume vs Pharma regulatory JD (related but different role)
    (
        "Clinical research coordinator experienced in oncology Phase III trials, GCP "
        "compliance, and EDC data management. Strong regulatory knowledge.",
        "Regulatory Affairs Specialist for pharmaceutical submissions. NDA/BLA filing "
        "experience required. Knowledge of FDA guidance documents and ICH guidelines."
    ),
    # Software engineer resume vs DevOps JD (adjacent domain)
    (
        "Backend engineer proficient in Python, PostgreSQL, and Redis. Built REST APIs "
        "serving 10M requests/day with 99.9% uptime.",
        "DevOps Engineer to manage CI/CD pipelines, Terraform infrastructure, and "
        "Kubernetes clusters. Strong Linux administration and monitoring tools experience."
    ),
    # Finance resume vs FP&A JD (same domain, different specialty)
    (
        "Investment banking analyst with M&A transaction experience. Built financial "
        "models and conducted valuation analysis for healthcare deals.",
        "FP&A Manager to lead budgeting, forecasting, and variance analysis. "
        "Experience with SAP, Hyperion, or Anaplan. CPA preferred."
    ),
    # Data science resume vs Business analyst JD (overlapping skills)
    (
        "Data scientist with expertise in Python, SQL, and statistical modeling. Built "
        "predictive models for customer churn reducing attrition by 15%.",
        "Business Analyst to gather requirements, create dashboards in Tableau, and "
        "perform ad-hoc data analysis. SQL proficiency required. MBA preferred."
    ),
    # Healthcare resume vs Public health JD (related field)
    (
        "Internal medicine physician with quality improvement and outcomes research "
        "experience. Published in peer-reviewed journals on population health.",
        "Epidemiologist for state health department. Design surveillance systems, "
        "analyze disease trends, and publish reports. MPH with biostatistics focus required."
    ),
    # Consulting resume vs Product management JD (transferable skills)
    (
        "Management consultant with experience in market sizing, competitive analysis, "
        "and stakeholder management. Led cross-functional teams of 8-12 people.",
        "Product Manager for B2B SaaS platform. Own product roadmap, conduct user research, "
        "and prioritize features. Agile/Scrum experience required."
    ),
]


# ---------------------------------------------------------------------------
# Models to benchmark
# ---------------------------------------------------------------------------

MODELS = [
    "all-MiniLM-L6-v2",
    "jjzha/jobbert-base-cased",
    "BAAI/bge-small-en-v1.5",
]


# ---------------------------------------------------------------------------
# Evaluation helpers
# ---------------------------------------------------------------------------

def compute_cosine_similarities(
    model, pairs: List[Tuple[str, str]]
) -> Tuple[List[float], float]:
    """Encode pairs and return per-pair cosine similarities and total elapsed ms."""
    from sentence_transformers import util as sbert_util

    texts_a = [p[0] for p in pairs]
    texts_b = [p[1] for p in pairs]

    start = time.perf_counter()
    emb_a = model.encode(texts_a, convert_to_tensor=True, show_progress_bar=False)
    emb_b = model.encode(texts_b, convert_to_tensor=True, show_progress_bar=False)
    elapsed_ms = (time.perf_counter() - start) * 1000

    sims = []
    for i in range(len(pairs)):
        sim = sbert_util.cos_sim(emb_a[i], emb_b[i]).item()
        sims.append(round(sim, 4))

    return sims, elapsed_ms


def get_model_size_mb(model) -> float:
    """Estimate model size from the underlying transformer files on disk."""
    try:
        model_path = Path(model[0].auto_model.config.name_or_path)
        # sentence-transformers caches to ~/.cache/...
        if not model_path.exists():
            # Try the cache directory
            from sentence_transformers import SentenceTransformer
            cache_dir = Path(SentenceTransformer._model_card_vars if hasattr(SentenceTransformer, '_model_card_vars') else "")
            # Fallback: count parameter bytes
            total_params = sum(p.numel() * p.element_size() for p in model.parameters())
            return round(total_params / (1024 * 1024), 1)
        total = sum(f.stat().st_size for f in model_path.rglob("*") if f.is_file())
        return round(total / (1024 * 1024), 1)
    except Exception:
        # Fallback: estimate from parameter count (float32 = 4 bytes per param)
        try:
            total_params = sum(p.numel() * p.element_size() for p in model.parameters())
            return round(total_params / (1024 * 1024), 1)
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def run_evaluation() -> Dict:
    """Run the full benchmark and return results dict."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print("ERROR: sentence_transformers not installed. Run: pip install sentence-transformers")
        sys.exit(1)

    all_pairs = MATCHING_PAIRS + NON_MATCHING_PAIRS + PARTIAL_MATCHING_PAIRS
    total_pairs = len(all_pairs)
    n_match = len(MATCHING_PAIRS)
    n_non = len(NON_MATCHING_PAIRS)
    n_partial = len(PARTIAL_MATCHING_PAIRS)

    print("Embedding Model Evaluation Benchmark")
    print(f"{'=' * 60}")
    print(f"Test pairs: {n_match} matching, {n_non} non-matching, {n_partial} partial")
    print(f"Total pairs: {total_pairs}")
    print()

    results: Dict = {"models": {}, "recommendation": ""}
    best_separation = -1.0
    best_model = ""

    for model_name in MODELS:
        print(f"Loading {model_name} ...", end=" ", flush=True)
        try:
            model = SentenceTransformer(model_name)
        except Exception as e:
            print(f"SKIPPED ({e})")
            results["models"][model_name] = {"error": str(e)}
            continue
        print("OK")

        # Model size
        size_mb = get_model_size_mb(model)

        # Compute similarities for each category
        match_sims, match_ms = compute_cosine_similarities(model, MATCHING_PAIRS)
        non_sims, non_ms = compute_cosine_similarities(model, NON_MATCHING_PAIRS)
        partial_sims, partial_ms = compute_cosine_similarities(model, PARTIAL_MATCHING_PAIRS)

        total_ms = match_ms + non_ms + partial_ms
        avg_ms = total_ms / total_pairs

        match_mean = float(np.mean(match_sims))
        non_mean = float(np.mean(non_sims))
        partial_mean = float(np.mean(partial_sims))
        separation = match_mean - non_mean

        # Discriminability: is partial mean between matching and non-matching?
        partial_ordered = non_mean < partial_mean < match_mean

        model_result = {
            "matching_mean_sim": round(match_mean, 4),
            "matching_sims": match_sims,
            "non_matching_mean_sim": round(non_mean, 4),
            "non_matching_sims": non_sims,
            "partial_matching_mean_sim": round(partial_mean, 4),
            "partial_matching_sims": partial_sims,
            "separation": round(separation, 4),
            "partial_correctly_ordered": partial_ordered,
            "avg_inference_ms": round(avg_ms, 2),
            "total_inference_ms": round(total_ms, 2),
            "model_size_mb": size_mb,
        }
        results["models"][model_name] = model_result

        if separation > best_separation:
            best_separation = separation
            best_model = model_name

        # Print summary for this model
        print(f"  Matching mean:      {match_mean:.4f}  (individual: {match_sims})")
        print(f"  Non-matching mean:  {non_mean:.4f}  (individual: {non_sims})")
        print(f"  Partial mean:       {partial_mean:.4f}  (individual: {partial_sims})")
        print(f"  Separation gap:     {separation:.4f}")
        print(f"  Partial ordered:    {partial_ordered}")
        print(f"  Avg inference:      {avg_ms:.2f} ms/pair")
        print(f"  Model size:         {size_mb} MB")
        print()

        # Free memory
        del model
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
        import gc
        gc.collect()

    # Generate recommendation
    evaluated = {k: v for k, v in results["models"].items() if "error" not in v}
    if evaluated:
        # Rank by separation (primary), then by speed (secondary)
        ranked = sorted(
            evaluated.items(),
            key=lambda x: (-x[1]["separation"], x[1]["avg_inference_ms"]),
        )
        winner = ranked[0]
        name = winner[0]
        sep = winner[1]["separation"]
        ms = winner[1]["avg_inference_ms"]
        ordered = winner[1]["partial_correctly_ordered"]

        recommendation = (
            f"{name} provides best separation ({sep:.4f}) at {ms:.1f} ms/pair. "
            f"Partial-match ordering correct: {ordered}."
        )
        results["recommendation"] = recommendation

        # Also include ranking
        results["ranking"] = [
            {"model": m, "separation": v["separation"], "avg_ms": v["avg_inference_ms"]}
            for m, v in ranked
        ]
    else:
        results["recommendation"] = "No models could be evaluated."

    return results


def main():
    results = run_evaluation()

    # Print final summary
    print("=" * 60)
    print("RECOMMENDATION")
    print(results.get("recommendation", "N/A"))
    print()

    if "ranking" in results:
        print("Ranking (by separation gap, then speed):")
        for i, entry in enumerate(results["ranking"], 1):
            print(f"  {i}. {entry['model']}  separation={entry['separation']:.4f}  avg={entry['avg_ms']:.1f}ms")
        print()

    # Save results
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    output_path = results_dir / "embedding_report.json"

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"Results saved to {output_path}")
    return results


if __name__ == "__main__":
    main()
