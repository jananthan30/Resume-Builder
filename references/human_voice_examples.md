# Human Voice Examples — Resume & Cover Letter

Use these as the gold standard when drafting or rewriting. Prefer the **HUMAN** column. Shared banned lexicon: `data/ai_tells.json`.

---

## Bullets (same facts)

### 1. Multi-site trial ops

**HUMAN**
> Led medical research and clinical data review across 8 concurrent Phase III trials for Pfizer, J&J, AbbVie, and Merck.

**AI (avoid)**
> Spearheaded cross-functional biomedical and scientific data review initiatives across 8 concurrent Phase III pharmaceutical programs for Pfizer, J&J, AbbVie, and Merck, ensuring technical accuracy and scientific integrity for diverse internal and external stakeholders.

---

### 2. Punch metric

**HUMAN**
> Zero protocol deviations across 8 concurrent studies.

**AI (avoid)**
> Ensured seamless protocol compliance and robust quality oversight, delivering zero protocol deviations across 8 concurrent late-stage programs through rigorous content review and scientific alignment.

---

### 3. Analytics / RWE

**HUMAN**
> Built a MIMIC-IV analysis pipeline on 11,300+ ICU stays (91% sensitivity).

**AI (avoid)**
> Leveraged cutting-edge real-world data assets from MIMIC-IV covering 11,300+ ICU stays, translating complex biomedical and scientific data into actionable clinical insights with 91% sensitivity for multidisciplinary partners.

---

### 4. Clinical volume

**HUMAN**
> ~100 patient encounters per month across gastroenterology and internal medicine.

**AI (avoid)**
> Facilitated approximately 100 patient encounters per month, synthesizing clinical and scientific evidence into evidence-based treatment decisions and improved patient outcomes across diverse care settings.

---

### 5. Writing / publications

**HUMAN**
> Co-authored case reports for peer-reviewed publication.

**AI (avoid)**
> Collaborated on manuscript development and literature review to deliver high-impact peer-reviewed publications, demonstrating strong written communication for diverse medical audiences.

---

## Professional Summary

**HUMAN**
> Physician and medical communications professional with 10+ years across clinical care and research. Published 6 peer-reviewed articles; ran operations across 8 concurrent Phase III programs for Pfizer, J&J, AbbVie, and Merck with zero protocol deviations. Yale Executive MPH (2026, 4.00 GPA).

**AI (avoid)**
> Results-driven medical information professional with 10+ years of combined clinical and pharmaceutical research experience delivering scientific response documents, medical review of promotional materials, and cross-functional MI support to HCPs, patients, and internal stakeholders across multiple therapeutic areas while advancing patient safety and improved patient outcomes through scientific rigor.

---

## Cover letter paragraph

**HUMAN**
> At a regional medical center I ran medical review across eight Phase III programs for four global sponsors. We closed those studies with zero protocol deviations. That same discipline — read the source, check the claim, document the decision — is how I would support your medical information team.

**AI (avoid)**
> I am excited to apply for this opportunity. Moreover, I bring a proven track record of leveraging robust, seamless processes to deliver best-in-class scientific support. It is not just experience — it is a passion for elevating cross-functional collaboration in today's fast-paced pharmaceutical landscape.

---

## Quick checklist before DOCX

- [ ] No banned openers (spearheaded, leveraged, orchestrated, …)
- [ ] Mean bullet length ≤ 22 words; mix short + medium
- [ ] Summary ≤ 3 sentences, no "Results-driven"
- [ ] Keywords mainly in Core Competencies
- [ ] `python human_voice_audit.py resume.md` exits 0
- [ ] `python evidence_audit.py resume.md` exits 0
