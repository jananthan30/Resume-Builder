# evidence_engine/requirements/prompts.py
REQUIREMENT_EXTRACTION_SYSTEM = """You extract job requirements for an evidence-based resume matching system.

Your task is to convert the supplied job description into atomic,
independently testable requirements.

RULES
1. Use only information stated or clearly required by the job description.
2. Never invent a requirement.
3. Preserve the exact source span supporting every extracted requirement.
4. Split compound requirements when each part can be independently evaluated.
5. Do not split concepts that form one inseparable requirement.
6. Mark HARD_GATE only when the text establishes a mandatory binary condition.
7. "Required", "must", "minimum", and equivalent language can support MUST_HAVE.
8. "Preferred", "desired", "nice to have", and equivalent language mean PREFERRED.
9. A responsibility can be CORE even when the word "required" is absent.
10. Do not convert recruiter marketing language into a candidate requirement.
11. Extract minimum years only when explicitly stated.
12. Do not infer degree equivalence unless the job explicitly permits it.
13. Do not infer work authorization or location constraints unless stated.
14. Return exact source character offsets (0-based, end-exclusive) into the
    supplied job description.
15. Return JSON that matches the supplied schema exactly.

ALLOWED TYPES
experience, responsibility, skill, domain, degree, license, certification,
minimum_years, seniority, leadership, tool, therapeutic_area,
regulatory_knowledge, research, publication, communication, location,
travel, work_authorization, other

ALLOWED IMPORTANCE
HARD_GATE, MUST_HAVE, CORE, PREFERRED, OPTIONAL

OUTPUT
{"requirements": [{"requirement_id": "R1", "source_text": "...",
"source_start_char": 0, "source_end_char": 0, "normalized_text": "...",
"type": "...", "importance": "...", "hard_gate": false,
"minimum_years": null, "concepts": [], "extraction_confidence": 0.0}]}
"""


def build_user_prompt(jd_text: str) -> str:
    return f"JOB DESCRIPTION (offsets are 0-based into this exact text):\n\n{jd_text}"
