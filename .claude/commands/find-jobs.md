---
description: Search live job boards for roles that match the master resume, then rank them by fit.
---

# Find Jobs — Discover & Score

Search live job boards for roles that match your resume, scored and ranked by fit.

## Input
$ARGUMENTS

## Instructions

You are helping the user discover relevant job opportunities scored against their resume using the `discover_jobs` MCP tool. Follow this workflow precisely.

### Step 1: Read Master Resume

Read `config.json` to get `master_resume_path`. Extract the full resume text:
- `.docx` files: use the `extract_text` MCP tool
- `.pdf`, `.md`, `.txt` files: read directly

If `config.json` is missing or `master_resume_path` is not set, ask the user to paste their resume text directly.

### Step 2: Parse the Search Query

From `$ARGUMENTS`, extract:
- **Job title**: the role to search for (e.g., "Senior Data Scientist", "Clinical Research Associate")
- **Location**: city, state, or country if mentioned (e.g., "New York", "Boston", "remote")
- **Remote preference**: if the user said "remote" or "remote only"

If the user provided no arguments at all, do NOT ask. Proceed with empty job_title — the AI will analyze the resume and suggest search terms automatically.

Examples of valid inputs:
- `Senior Data Scientist in New York`
- `Clinical Research Associate Boston`
- `Software Engineer remote`
- `Medical Director` (no location)
- _(blank)_ — AI analyzes resume and picks the best search

### Step 3: Call discover_jobs

Call the `discover_jobs` MCP tool with:
- `resume_text`: full resume text from Step 1
- `job_title`: extracted from Step 2 (empty string if none given)
- `location`: extracted from Step 2 (empty string if none)
- `remote_only`: true if user said remote only, false otherwise
- `max_results`: 10

### Step 4: Handle Errors

**If `setup_required` is true** (no API keys):
Show this message exactly:
```
Job discovery needs API keys to search live job boards.

Option 1 — Cloud (easiest, no setup):
  Sign up at https://resume-scorer-web.streamlit.app
  Then run /resume-builder:setup to link your account.

Option 2 — Local (free):
  Get free Adzuna keys at https://developer.adzuna.com/
  Add to your .env file:
    ADZUNA_APP_ID=your_app_id
    ADZUNA_APP_KEY=your_app_key
  Remotive (remote jobs only) works without any key.
```

**If `jobs` is empty**: Tell the user no results were found and suggest trying a broader job title or different location.

### Step 5: Display Results

If the response includes `ai_analysis`, show it first:
```
Analyzed your resume:
  Recent role:   [recent_title]
  Career level:  [career_level]
  Domain:        [domain]
  Searched for:  [search_queries_used joined with ", "]
```

Then display the ranked jobs table:

```
╔═══╦══════════════════════════════════╦══════════════════╦═════╦═════╦════════════════════╗
║ # ║ Job Title                        ║ Company          ║ ATS ║ HR  ║ Salary             ║
╠═══╬══════════════════════════════════╬══════════════════╬═════╬═════╬════════════════════╣
║ 1 ║ Senior Data Scientist            ║ Pfizer           ║ 82% ║ 74% ║ $120k–$150k        ║
║ 2 ║ Data Scientist II                ║ Goldman Sachs    ║ 79% ║ 71% ║ $110k–$140k        ║
╚═══╩══════════════════════════════════╩══════════════════╩═════╩═════╩════════════════════╝
```

For salary: show "$Xk–$Yk" if both min and max exist, "$Xk+" if only min, "Not listed" if neither.

For each job also show:
- Location
- Posted date (if available)
- Apply link: `[Apply →](url)`
- Top matched keywords (from `ats_detail.matched_keywords`, first 4)
- Missing keywords (from `ats_detail.missing_keywords`, first 3) — show as "gaps"

Format each job card like this:

```
#1  Senior Data Scientist — Pfizer  (New York, NY)
    ATS: 82%  |  HR: 74%  |  $120k–$150k  |  Posted: 2026-02-28
    ✅ Matched: Python, SQL, Machine Learning, Clinical Trials
    ⚠️  Gaps: Spark, Databricks, AWS
    [Apply →](https://...)
```

Show attribution line at the bottom: `Powered by Adzuna` or `Powered by Adzuna & Remotive`

### Step 6: Offer Next Steps

After showing results, offer:

```
What would you like to do next?

  A) Tailor my resume for job #1 → /resume-builder:tailor-resume [paste JD]
  B) Generate a cover letter    → /resume-builder:cover-letter [paste JD]
  C) Search again with different title or location
  D) Show full details for a specific job
```

If the user picks A or B, remind them to paste the full job description from the apply link since the search results only contain summaries.

### Notes for Codex

- Never make up job listings. Only show results returned by the tool.
- If a job has `scoring_tier: "none"` (no description available), show ATS/HR as "N/A".
- If HR score is 0 and there's an error in `hr_detail`, show HR as "N/A" rather than 0%.
- The `attribution` field must always be shown — it's a legal requirement of the Adzuna API terms.
- Do not show internal fields like `_light_score`, `source`, `id`, or `_combined` to the user.
