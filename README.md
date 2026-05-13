# ResumeHQ — AI-Powered Resume Builder with ATS & HR Scoring

The only resume tool that **finds jobs, scores your fit, and tailors your resume** — all in one workflow. Works as a [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin, Codex plugin, or standalone web app.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-Plugin-blueviolet)](https://docs.anthropic.com/en/docs/claude-code)
[![Codex](https://img.shields.io/badge/Codex-Plugin-111111)](https://developers.openai.com/codex/plugins/build)

---

## Why ResumeHQ?

Most resume tools only score the resume you bring to them. ResumeHQ goes further:

| Feature | Jobscan | Rezi | Teal | **ResumeHQ** |
|---------|---------|------|------|--------------|
| ATS keyword scoring | ✅ | ✅ | ✅ | ✅ |
| HR / recruiter simulation | ❌ | ❌ | ❌ | ✅ |
| Discover matching jobs | ❌ | ❌ | ❌ | ✅ |
| Score jobs against your resume | ❌ | ❌ | ❌ | ✅ |
| Auto-tailor resume to JD | ✅ | ✅ | ❌ | ✅ |
| ATS-compliant DOCX output | ❌ | ✅ | ❌ | ✅ |
| Application tracker | ❌ | ❌ | ✅ | ✅ |
| Works in Claude Code / claude.ai / Codex | ❌ | ❌ | ❌ | ✅ |
| Open source | ❌ | ❌ | ❌ | ✅ |

---

## What This Does

You paste a job description (or search for jobs). The system:

1. **Discovers** matching jobs from live job boards — scored and ranked by fit with your resume
2. **Analyzes** the JD — extracts keywords, required skills, domain, seniority level
3. **Tailors** your master resume — rewrites bullets, reorders sections, matches terminology
4. **Scores** the result with two independent engines (ATS + HR simulation)
5. **Iterates** automatically until scores hit targets (ATS 75-85%, HR 70%+)
6. **Generates** production-ready DOCX files (resume + cover letter)
7. **Tracks** every application in an Excel spreadsheet

All of this runs in **parallel** — scoring happens in the background while the resume is being written, cover letters generate simultaneously, and DOCX files are created in parallel. ~50% faster than sequential execution.

---

## Quick Start — 3 Steps

Works with **Claude Code** (CLI/IDE), **Codex** (CLI/app/IDE), and **claude.ai** (web/Projects).

**Step 1: Install the plugin**

Claude Code:

```bash
/plugin marketplace add jananthan30/Resume-Builder
/plugin install resume-builder
```

Codex from a local checkout:

```bash
codex plugin marketplace add .
```

Then restart Codex and install **Resume Builder** from the **Resume Builder Local** marketplace.

**Step 2: Run the setup wizard**

```
/resume-builder:setup
```

This walks you through everything:
- Checks if Python is installed (tells you where to download it if not)
- Installs all dependencies automatically (`pip install -r requirements.txt`)
- Creates your `config.json` with your name, email, phone, LinkedIn
- Optionally links a Pro account for unlimited cloud scoring
- Optionally sets up the LLM scorer (Claude API key)

**Step 3: Start building resumes**

```
/resume-builder:resume [paste a job description here]
```

Or find jobs first:

```
/resume-builder:find-jobs Senior Data Scientist in New York
```

---

## Slash Commands (8)

| Command | What It Does |
|---------|-------------|
| `/resume-builder:setup` | One-time setup wizard (installs Python deps, creates config, links Pro account) |
| `/resume-builder:job-fit [JD]` | Quick GO/NO-GO job fit score before tailoring |
| `/resume-builder:resume [JD]` | Full application: tailored resume + cover letter + scoring + DOCX + tracking |
| `/resume-builder:tailor-resume [JD]` | Resume only (no cover letter) |
| `/resume-builder:cover-letter [JD]` | Cover letter only |
| `/resume-builder:find-jobs [title] [location]` | Discover and score matching jobs from live job boards |
| `/resume-builder:batch-resume` | Process multiple job descriptions in parallel |
| `/resume-builder:writing-coach [file]` | Audit and rewrite resume bullets using 10 writing rules |

If running locally (cloned repo), use short names: `/resume`, `/tailor-resume`, `/find-jobs`, etc.

### What Works Without Setup

Even before running `/resume-builder:setup`, these commands work immediately (they're prompt-driven workflows — no Python needed):

| Command | Works immediately? | With setup? |
|---------|-------------------|-------------|
| `/resume-builder:job-fit` | Yes — heuristic fit analysis | + scorer-backed knockout detection |
| `/resume-builder:resume` | Yes — the assistant writes the resume | + automated ATS/HR scoring and DOCX output |
| `/resume-builder:cover-letter` | Yes — the assistant writes the letter | + DOCX output |
| `/resume-builder:writing-coach` | Yes — full writing audit | Same |
| `/resume-builder:find-jobs` | Yes — shows results (no score) | + ATS/HR fit scoring per job |
| `/resume-builder:setup` | Yes — runs the setup wizard | N/A |
| MCP scoring tools | No — needs Python | `score_resume`, `score_ats`, `score_hr`, `score_with_llm`, `rewrite_resume`, `explain_score`, `extract_text`, `discover_jobs` |

---

## MCP Tools (8)

After running `/resume-builder:setup`, the MCP scorer auto-starts and provides these tools that Claude Code or Codex can call natively:

| Tool | What It Does |
|------|-------------|
| `score_resume` | Full ATS + HR analysis in one call (recommended) |
| `score_ats` | ATS keyword + semantic scoring (8 components) |
| `score_hr` | HR recruiter simulation (6 factors + F-pattern) |
| `score_with_llm` | LLM-augmented rubric scoring (requires ANTHROPIC_API_KEY) |
| `rewrite_resume` | AI-powered resume tailoring to match a JD (requires ANTHROPIC_API_KEY) |
| `explain_score` | Actionable improvement suggestions with missing keywords |
| `extract_text` | Extract text from DOCX/PDF/MD/TXT files |
| `discover_jobs` | Search live job boards and score each job against your resume |

All MCP tools support **cloud-first scoring** — they try the cloud API first and fall back to local scoring automatically.

---

## Job Discovery

The `/find-jobs` command and `discover_jobs` MCP tool search live job boards and rank results by how well each job matches your resume — answering "which jobs should I actually apply to?" with data.

```
/resume-builder:find-jobs Senior Product Manager in San Francisco
/resume-builder:find-jobs Data Scientist remote
```

**How it works:**
1. Searches Adzuna (16 countries, salary data) + Remotive (remote jobs)
2. Pre-filters top 20 results by title relevance
3. Lightweight scores all 20 candidates (keyword + phrase + BM25 — fast)
4. Full ATS + HR scores top 10 finalists
5. Returns ranked list with scores, salary range, and apply links

**Sample output:**

```
Rank  Title                        Company        ATS   HR    Salary
────  ───────────────────────────  ─────────────  ────  ────  ──────────────
#1    Senior Data Scientist        Pfizer          82%   74%  $120k–$150k
#2    Data Scientist II            Goldman Sachs   79%   71%  $110k–$140k
#3    ML Engineer – NLP            Microsoft       74%   68%  $130k–$160k
```

**API keys required for job search:**
- **Adzuna** (free): Register at [developer.adzuna.com](https://developer.adzuna.com/) — add `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` to your `.env`
- **Remotive**: No key needed (remote jobs only, included automatically)

---

## Dual Scoring System

### ATS Scorer — 8 Weighted Components

Simulates how Applicant Tracking Systems filter resumes before a human ever sees them.

| Component | Weight | What It Measures |
|-----------|--------|------------------|
| Phrase Match | 25% | Multi-word industry phrases (10.6x callback increase for exact matches) |
| Keyword Match | 20% | Lemmatized keywords with synonym expansion |
| Weighted Industry Terms | 15% | Domain-specific terminology with recency decay |
| Semantic Similarity | 10% | SBERT vector cosine similarity between resume and JD |
| BM25 Score | 10% | Probabilistic relevance ranking (BM25Plus) |
| Job Title Match | 10% | Exact JD title in resume header/summary |
| Graph Centrality | 5% | Infers missing skills from related skills via NetworkX |
| Skill Recency | 5% | Exponential decay — recent experience weighted higher |

**Additional checks:** Hidden text detection, readability analysis (Flesch-Kincaid Grade 10-12 optimal), format risk assessment.

### HR Scorer — 6 Factors + Visual Analysis

Simulates how a human recruiter evaluates a resume in their typical 7-second scan.

| Factor | Weight | What It Measures |
|--------|--------|------------------|
| Experience Fit | 30% | Years of experience vs. JD requirements, Goldilocks zone |
| Skills Match | 20% | Demonstrated skills (action verbs) vs. listed skills |
| Career Trajectory | 20% | Title progression via linear regression slope |
| Impact Signals | 20% | Metrics density + Bloom's Taxonomy verb power levels |
| Competitive Edge | 10% | Company/university prestige signals |
| F-Pattern Visual | +/-5pts | Eye-tracking compliance (golden triangle, left-rail alignment) |

**Risk penalties:** Job hopping (-8 to -15 pts), unexplained gaps (-5 to -15 pts), recent instability.

### LLM Scorer (Optional)

Claude-powered rubric evaluation that catches nuances the algorithmic scorers miss — tone, coherence, storytelling quality.

---

## Pricing

| Tier | Price | What You Get |
|------|-------|-------------|
| **Free** | $0 | 5 cloud scores, then automatic local scoring fallback |
| **Pro** | $12/mo | Unlimited cloud scoring — ideal for Claude Code / claude.ai users |
| **Ultra** | $29/mo | Unlimited scoring + AI resume rewriting via web app |

**Note for Claude Code / claude.ai users:** Your Anthropic subscription already handles resume writing via Claude. The scorer server only does ATS + HR scoring, so **Pro is all you need** — you do not need Ultra.

Sign up at [resume-scorer-web.streamlit.app](https://resume-scorer-web.streamlit.app). After signing up, run `/resume-builder:setup` to link your Pro account in one step.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│             Claude Code / claude.ai                          │
│  /resume  /tailor-resume  /cover-letter  /find-jobs  /setup  │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │  ATS     │  │  HR      │  │  LLM     │  │  Writing  │  │
│  │  Scorer  │  │  Scorer  │  │  Scorer  │  │  Coach    │  │
│  │ (8-comp) │  │ (6-fact) │  │ (Claude) │  │ (10 rules)│  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └─────┬─────┘  │
│       └──────────────┴─────────────┘               │        │
│                      │                             │        │
│              ┌───────┴───────┐              ┌──────┴─────┐  │
│              │  MCP Server   │              │   DOCX     │  │
│              │  (FastMCP 3)  │              │ Generator  │  │
│              │  Cloud-first  │              │ (Workday)  │  │
│              └───────┬───────┘              └────────────┘  │
│                      │                                      │
│            ┌─────────┴──────────┐                           │
│            │  Cloud API         │                           │
│            │  resume-scorer     │                           │
│            │  .fly.dev          │                           │
│            │  (JWT + API key)   │                           │
│            └────────────────────┘                           │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│  Job Discovery: Adzuna + Remotive → lightweight score →     │
│  full ATS+HR score → ranked results                         │
├─────────────────────────────────────────────────────────────┤
│  Orchestration State (state.json) — Multi-Agent DAG        │
│  Application Tracker (Excel) — Auto-updated per run        │
└─────────────────────────────────────────────────────────────┘
```

The MCP server operates in **thin client** mode: it tries the cloud API first for scoring, and falls back to local scoring if the cloud is unavailable or not configured. LLM scoring always runs locally using your own API key (BYOK).

---

## Workflow

```
1. /resume-builder:setup            One-time setup (install deps, create config, link Pro)
2. Create your master resume         YOUR_MASTER_RESUME.md with full work history
3. /resume-builder:find-jobs [JD]   Optional — discover matching jobs scored by fit
4. /resume-builder:resume [JD]      Paste a job description — get a full application
5. /resume-builder:writing-coach    Optional — audit and improve writing quality
```

Each command runs a multi-phase parallel workflow:
- **Phase 1:** Parallel research (reads master resume, finds best match, sets up output folder)
- **Phase 2:** Background scoring + resume writing (non-blocking)
- **Phase 3:** Parallel scoring + cover letter generation
- **Phase 4:** Auto-iteration if scores < threshold (max 2 rounds)
- **Phase 5:** Parallel DOCX creation + tracker update
- **Phase 6:** Cleanup + score report

---

## Alternative: Clone & Run Locally

If you prefer not to use the plugin system:

```bash
git clone https://github.com/jananthan30/Resume-Builder.git
cd Resume-Builder

pip install -r requirements.txt

# Download NLTK data (one-time)
python -c "import nltk; nltk.download('wordnet'); nltk.download('punkt_tab')"

cp .env.example .env
cp config.example.json config.json
```

Then edit `.env` (API keys) and `config.json` (your info), and use commands without the `resume-builder:` prefix (e.g., `/resume` instead of `/resume-builder:resume`).

---

## Cloud Scoring API

The scoring API is hosted at `https://resume-scorer.fly.dev`. Free users get **5 scored resumes**, then local scoring activates automatically. Sign up or upgrade at [resume-scorer-web.streamlit.app](https://resume-scorer-web.streamlit.app).

The easiest way to link your account is via the setup wizard:

```
/resume-builder:setup
```

Or manually add to your `.env`:

```bash
SCORER_CLOUD_URL=https://resume-scorer.fly.dev
SCORER_CLOUD_API_KEY=rb_your_api_key_here
```

### MCP Configuration

The `.mcp.json` file configures the MCP server to auto-start with Claude Code:

```json
{
  "mcpServers": {
    "ai-resume-tuner": {
      "command": "python",
      "args": ["mcp_scorer.py"],
      "cwd": "/path/to/Resume-Builder",
      "env": {
        "SCORER_CLOUD_URL": "https://resume-scorer.fly.dev"
      }
    }
  }
}
```

**Environment variables:**

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SCORER_CLOUD_URL` | No | `https://resume-scorer.fly.dev` | Cloud scoring API URL |
| `SCORER_CLOUD_API_KEY` | No | — | Your cloud API key (`rb_...`). Anonymous scoring (5 free) works without this. |
| `ANTHROPIC_API_KEY` | No | — | For LLM scoring (always runs locally with your key) |
| `ANTHROPIC_MODEL` | No | `claude-sonnet-4-6` | Claude model for LLM scoring |
| `ADZUNA_APP_ID` | No | — | For job discovery (free at [developer.adzuna.com](https://developer.adzuna.com/)) |
| `ADZUNA_APP_KEY` | No | — | For job discovery |

---

## Your Master Resume

Create a file with your complete work history. Supported formats: `.docx`, `.pdf`, `.md`, or `.txt`. This is the single source of truth — all tailored resumes are generated from it. DOCX is recommended since most people already have their resume in that format.

```
FULL NAME, CREDENTIALS
City, State ZIP | Phone | Email | LinkedIn

PROFESSIONAL SUMMARY
[Your comprehensive summary with all skills and experience]

PROFESSIONAL EXPERIENCE

JOB TITLE | COMPANY NAME | City, State
Month Year – Month Year

• Achievement with quantified impact
• Another achievement with metrics

EDUCATION

Degree Name
University Name, City, State | Year – Year

CERTIFICATIONS
• Certification Name – Issuing Body
```

Set the path to this file in your `config.json` as `master_resume_path`.

---

## Scoring Reference

### ATS Score

| Score | Rating | Meaning |
|-------|--------|---------|
| 80-100% | Excellent | Top candidate — likely to pass all ATS filters |
| 65-79% | Good | Strong match — will pass most filters |
| 50-64% | Fair | Competitive — may need optimization |
| 35-49% | Low | Below average — significant gaps |
| 0-34% | Poor | Unlikely to pass automated screening |

### HR Score

| Score | Recommendation | Meaning |
|-------|---------------|---------|
| 85%+ | STRONG INTERVIEW | Top candidate |
| 70-84% | INTERVIEW | Competitive |
| 55-69% | MAYBE | Marginal — depends on candidate pool |
| <55% | PASS | Weak match |

---

## API Reference

The scoring API runs locally (`python scorer_server.py --port 8100`) or is hosted at `https://resume-scorer.fly.dev`.

### Scoring Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/health` | GET | No | Server health and version info |
| `/score/ats` | POST | Yes | ATS scoring (8 weighted components) |
| `/score/hr` | POST | Yes | HR recruiter simulation |
| `/score/both` | POST | Yes | ATS + HR combined in one call (JSON by default, SSE with `Accept: text/event-stream`) |
| `/score/llm` | POST | Yes | LLM scoring via Claude |
| `/score/combined` | POST | Yes | All 3 blended (70% rules / 30% LLM) |
| `/score/batch` | POST | Yes | Score multiple resume/JD pairs |
| `/explain` | POST | Yes | Detailed score explanation |
| `/jobs/discover` | POST | Yes | Search jobs + score against resume |

### Auth & Billing Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/auth/register` | POST | Create account (email + password) |
| `/auth/login` | POST | Login and get JWT token |
| `/auth/api-key` | POST | Create an API key (requires JWT) |
| `/auth/usage` | GET | Check usage stats and remaining scores |
| `/billing/checkout` | POST | Start Stripe checkout for Pro upgrade |
| `/billing/portal` | POST | Stripe customer portal |

### Authentication

- **JWT Bearer token:** `Authorization: Bearer <token>` (from `/auth/login`)
- **API key:** `X-API-Key: rb_...` (from `/auth/api-key` or web dashboard)

### Example: Score a Resume

```bash
curl -X POST https://resume-scorer.fly.dev/score/ats \
  -H "X-API-Key: rb_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"resume_text": "Your resume text...", "jd_text": "Job description text..."}'
```

### Example: Discover Jobs

```bash
curl -X POST https://resume-scorer.fly.dev/jobs/discover \
  -H "X-API-Key: rb_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"resume_text": "Your resume...", "job_title": "Data Scientist", "location": "New York", "max_results": 10}'
```

---

## Domain-Specific Scoring

The ATS scorer auto-detects the job domain and applies domain-specific adjustments:

| Domain | Detection Method | Key Adjustments |
|--------|------------------|-----------------|
| **Clinical Research** | SBERT prototype embeddings | Publications bonus, transferable skills mapping |
| **Pharma/Biotech** | Keyword + semantic hybrid | Regulatory terminology weighting, pipeline experience |
| **Technology** | Keyword + semantic hybrid | Portfolio links bonus, 1.3x skill recency weight |
| **Finance** | Keyword + semantic hybrid | Deal artifacts required, 1.5x prestige weight |
| **Consulting** | Keyword + semantic hybrid | Impact metrics required, 1.4x prestige weight |
| **Healthcare** | Keyword + semantic hybrid | Certifications required, quality improvement focus |

---

## Supported Professions

Works for **any profession**. The scorer auto-detects domain and applies appropriate weights:

| Domain | Example Roles |
|--------|---------------|
| **Clinical Research** | CRA, Medical Monitor, Study Director, Clinical Operations |
| **Pharma/Biotech** | Regulatory Affairs, Medical Science Liaison, Drug Safety |
| **Technology** | Software Engineer, Product Manager, Data Scientist, ML Engineer |
| **Finance** | Investment Analyst, Financial Controller, Risk Manager |
| **Consulting** | Management Consultant, Strategy Analyst, Business Advisor |
| **Healthcare** | Nurse Manager, Quality Director, Health Administrator |
| **General** | Any role not matching a specific domain — uses universal scoring |

---

## ATS-Compliant DOCX Output

The DOCX generator produces files optimized for Applicant Tracking Systems (Workday, Taleo, Greenhouse, Lever):

- **No tables, text boxes, columns, or graphics** (ATS parsers can't read these)
- **Heading styles** for section detection (Workday XML parsing)
- **Safe fonts**: Calibri, Arial, Times New Roman (10-12pt body)
- **Clean structure**: Contact info in body (not headers/footers)
- **Bold metrics** for visual impact during human review

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| AI Agent Framework | [Claude Code](https://docs.anthropic.com/en/docs/claude-code) / [claude.ai](https://claude.ai) |
| LLM | [Claude](https://www.anthropic.com/claude) (Anthropic) |
| MCP Server | [FastMCP 3.0](https://gofastmcp.com/) (auto-starts with plugin, cloud-first thin client) |
| Embeddings | [Sentence Transformers](https://sbert.net/) (all-MiniLM-L6-v2) |
| NLP | NLTK (lemmatization), TextStat (readability) |
| Search | BM25Plus (rank-bm25), NetworkX (skill graphs) |
| Job Discovery | Adzuna API + Remotive API |
| API Server | FastAPI + Uvicorn |
| Cloud Hosting | [Fly.io](https://fly.io) (auto-stop/start, persistent volume) |
| Auth | JWT (PyJWT) + SQLite-backed API keys |
| Billing | [Stripe](https://stripe.com) (subscription management) |
| Document Generation | python-docx |
| PDF Parsing | pdfplumber |
| Tracking | openpyxl (Excel) |

---

## Project Structure

```
Resume-Builder/
├── .claude-plugin/             # Plugin manifest
│   └── plugin.json             # Plugin metadata (name, version, author)
├── .codex-plugin/              # Codex plugin manifest
│   └── plugin.json             # Codex metadata and install-surface copy
├── .agents/plugins/
│   └── marketplace.json        # Local Codex marketplace entry
├── commands/                   # Slash commands (plugin format)
│   ├── setup.md                # One-time setup wizard
│   ├── job-fit.md              # Quick GO/NO-GO job fit pre-screen
│   ├── resume.md               # Full application (Swarm v3.0)
│   ├── tailor-resume.md        # Resume only
│   ├── cover-letter.md         # Cover letter only
│   ├── find-jobs.md            # Job discovery + scoring
│   ├── batch-resume.md         # Batch processing
│   └── writing-coach.md        # Writing enhancement (10 rules)
├── hooks/                      # Plugin hooks
│   └── hooks.json              # SessionStart: checks if scoring is ready
├── .mcp.json                   # MCP server config (auto-starts scorer)
├── .codex.mcp.json             # Codex MCP server config
├── mcp_scorer.py               # MCP scoring server (8 tools, cloud-first thin client)
├── job_discovery.py            # Job search + two-tier scoring (Adzuna + Remotive)
├── data/                       # Reference databases for scoring
│   ├── keywords_*.json         # Domain-specific keyword databases (6 domains)
│   ├── skill_taxonomy.json     # Skill categories with decay constants
│   ├── company_prestige.json   # Company prestige scoring
│   ├── university_rankings.json# University prestige scores
│   ├── acronyms.json           # Industry acronym expansion
│   └── action_verbs.json       # Verb power classifications
├── ats_scorer.py               # ATS scoring engine (2,800+ lines)
├── hr_scorer.py                # HR scoring engine (2,900+ lines)
├── llm_scorer.py               # LLM-powered rubric scorer
├── scorer_server.py            # FastAPI REST API (v3.0 — auth, usage, billing)
├── pii_redactor.py             # PII redaction via Presidio (pre-LLM API calls)
├── docx_generator.py           # ATS/Workday-compliant DOCX generator
├── orchestration_state.py      # Multi-agent state management (DAG)
├── tracker_utils.py            # Excel application tracker utilities
├── resume_builder.py           # CLI entry point
├── requirements.txt            # Python dependencies
├── config.example.json         # Config template
├── .env.example                # Environment variable template
├── AGENTS.md                   # Project context for Codex
├── CLAUDE.md                   # Project context for Claude Code
├── LICENSE                     # MIT License
└── README.md                   # You are here
```

---

## Writing Coach — 10 Rules Engine

The `/writing-coach` command applies these writing rules to every bullet point:

1. **Power Verb Start** — Every bullet begins with a strong action verb (Led, Directed, Spearheaded)
2. **Quantified Impact** — 40%+ of bullets must contain metrics (%, $, numbers)
3. **So-What Test** — Every bullet answers "why does this matter?"
4. **Jargon Calibration** — Match terminology level to the target role
5. **Tense Consistency** — Past tense for past roles, present for current
6. **Parallel Structure** — Consistent grammatical patterns within sections
7. **Length Optimization** — 1-2 lines per bullet, no walls of text
8. **Keyword Integration** — Natural placement, never forced
9. **Achievement vs. Duty** — Frame responsibilities as accomplishments
10. **Readability** — Flesch-Kincaid Grade 10-12 target

---

## Contributing

Contributions are welcome! Some ideas:

- **New domain profiles** — add keyword databases for law, marketing, academia, etc.
- **Additional job boards** — integrate Indeed, LinkedIn, or regional boards
- **Additional ATS parsers** — test against more ATS systems (Taleo, iCIMS, Greenhouse)
- **Resume templates** — add more DOCX template styles
- **Internationalization** — support for non-English resumes and job markets

```bash
git checkout -b feature/your-feature
# ... make changes ...
git commit -m "Add your feature"
git push origin feature/your-feature
```

---

## License

MIT License — see the [LICENSE](LICENSE) file for details.

---

## Acknowledgments

- Built with [Claude Code](https://docs.anthropic.com/en/docs/claude-code) by [Anthropic](https://www.anthropic.com/)
- ATS scoring research based on real-world Applicant Tracking System behavior
- HR scoring model informed by eye-tracking research on recruiter behavior
- Domain keyword databases curated from thousands of real job descriptions
- Job search powered by [Adzuna](https://www.adzuna.com/) and [Remotive](https://remotive.com/)

---

**If this project helps you land interviews, give it a star ⭐**
