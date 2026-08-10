---
name: cover-letter
description: Create a compelling one-page, human-voice cover letter for a job description and generate the final DOCX. Use when the user wants a cover letter only (no resume), pastes a JD and asks for a letter, or needs a letter to accompany an already-tailored resume. Runs the mandatory human_voice_audit before producing the DOCX.
---

# Generate Cover Letter Only

Create a compelling one-page cover letter for a job application.

## Job Description

The user provides the target job description when invoking this skill; treat that
text as the exact JD throughout.

## Instructions

You are the cover-letter coordinator. The user has provided a job description.

**Your task:**

### Phase 1: Setup

1. **Extract company name and job title** from the job description

2. **Search for a similar existing resume** in the `applications/` folder to understand what was already tailored:
   - List all subfolders in `applications/`
   - Compare folder job titles against the NEW job description's title and requirements
   - **If a similar resume is found**: Read that resume to understand the applicant's tailored background for this type of role
   - **If NO similar resume is found**: Read the master resume (path from `config.json` → `master_resume_path`, or glob for `*MASTER*RESUME*.md`) to understand the applicant's background
   - Always also read the master resume for canonical details
   - Treat an existing tailored resume as eligible evidence only if `resume_integrity_audit.py --config config.json --tailored <resume>` exits 0. Otherwise use the master resume.
   - If this request also requires creating or changing a resume, run a complete native `resume-team/v2` workflow (see the resume-team skill, `skills/resume-team/SKILL.md`) under a fresh `run_id`; never draft or patch a resume inside this skill. After that run authorizes an exact resume digest, cover-letter work must not alter the resume bytes or reuse stale authorization.

3. **Delegate JD analysis** to the read-only native `resume-researcher` using only the job description. Validate its `resume-team-handoff/v1` response. Use its rubric only to choose relevant, already-supported experiences; JD requirements are not evidence that the candidate has a skill.

4. **Create output folder** at `applications/{CompanyName} - {JobTitle}/` (if not exists)

5. **Save the job description** as `job_description.txt` (if not exists)

### Phase 2: Cover Letter Generation

6. **Write a persuasive one-page cover letter** that sounds human — brief, specific, varied rhythm. Follow the writing-coach skill (`skills/writing-coach/SKILL.md`) Rules 0, 11–16 for letters.

   **Opening Hook (1 short paragraph):**
   - Lead with a concrete match or proof — not "I am writing to express my interest" or "I am excited to apply"
   - Show you understand the role in plain language

   **Value Proposition (2 short paragraphs):**
   - Connect 2–3 specific experiences to key requirements (real stories, not slogan stacks)
   - Brief STAR; include real metrics where they exist
   - Keywords only where natural — never force lists of three adjectives

   **Company Connection (1 short paragraph):**
   - One specific, true detail about the company (product, trial, mission)
   - No generic "impressed by your commitment to excellence"

   **Strong Close (1 short paragraph):**
   - Clear ask; thank them; no "Moreover" / "In conclusion" / "passionate about"

7. **Format + human-voice requirements:**
   - Maximum ONE page (≤ 400 words)
   - Professional but personable — human ≠ slang, human ≠ corporate poetry
   - Ban AI lexicon (`data/ai_tells.json`): delve, leverage, robust, seamless, tapestry, furthermore, etc.
   - Varied sentence lengths; short paragraphs
   - NO placeholder text like [Your Address]
   - Ready to send immediately

### Phase 3: DOCX Creation & Cleanup

8. **Save cover letter** as `cover_letter.md` first

9. **Human voice audit (mandatory before DOCX):**
```bash
python human_voice_audit.py "applications/{folder}/cover_letter.md" --mode cover_letter
```
- Exit 0: proceed to DOCX.
- Exit 1: rewrite failures (banned phrases, fluff transitions, length) then re-run. Max 2 rounds.
- Do NOT create DOCX until exit 0.

10. **Create DOCX**:
   - `{Name}_Cover_Letter_{Company}.docx` - Professional formatting
   - Never call `create_ats_cover_letter()` directly from this skill.
   - For cover-letter-only work without a native resume receipt, call `create_cover_letter_from_md()`; it re-audits the exact in-memory Markdown and performs verified sibling-temp + atomic replacement.
   - When this application package has an authorized native resume and receipt, call `create_cover_letter_from_md_authorized()` with the exact resume path, receipt sidecar, receipt digest, and `config.json`; it must revalidate the resume before the same atomic cover-letter path.
   - Treat any exception, empty/unopenable DOCX, content-parity failure, or non-literal returned output path as failure. Preserve any pre-existing final DOCX bytes.

11. **Delete `cover_letter.md`** only after successful DOCX creation and open/readback verification

### Phase 4: Final Output

12. **Display the full cover letter** text for review

13. **List generated files**:
    - `{Name}_Cover_Letter_{Company}.docx`
    - `job_description.txt`

After completion, display word count (≤ 400) and confirm human_voice_audit passed.
