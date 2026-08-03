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
2. **Gates candidate fit** — the configured master must score at least 70 against
   the exact JD with zero hard knockouts before resume work begins
3. **Analyzes** passing JDs — extracts keywords, required skills, domain, seniority level
4. **Tailors** your master resume — rewrites bullets, reorders sections, matches terminology
5. **Scores** the result with two independent advisory engines (ATS + HR simulation)
6. **Iterates** automatically until scores hit targets (ATS 75-85%, HR 70%+)
7. **Generates** production-ready DOCX files (resume + cover letter)
8. **Tracks** every application in an Excel spreadsheet

The candidate-fit gate always runs first and cannot be bypassed by ATS/HR scores.
After it passes, safe read/scoring work may run concurrently while authorization,
DOCX generation, and tracker mutation remain ordered.

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

**Step 2: Configure the runtime**

Claude Code exposes the plugin setup command:

```
/resume-builder:setup
```

This walks you through everything:
- Checks if Python is installed (tells you where to download it if not)
- Installs all dependencies automatically (`pip install -r requirements.txt`)
- Creates your `config.json` with your name, email, phone, LinkedIn
- Optionally links a Pro account for unlimited cloud scoring
- Optionally sets up the LLM scorer (Claude API key)

For Codex, install Python 3.10+, run `python -m pip install -r requirements.txt`,
and create `config.json` with a valid `master_resume_path`. The installed Codex
surface exposes the Resume Team as `$resume-team`; it does not expose the
Claude-style `/resume-builder:*` command namespace.

**Step 3: Start building resumes**

Claude Code:

```
/resume-builder:resume [paste a job description here]
```

Codex:

```
$resume-team [paste a job description here]
```

`$resume-team` publishes an authorized, digest-verified `resume.md` draft. It does
not by itself create a DOCX or complete an application package.

Or find jobs first:

```
/resume-builder:find-jobs Senior Data Scientist in New York
```

---

## Claude Code Slash Commands (9)

| Command | What It Does |
|---------|-------------|
| `/resume-builder:setup` | One-time setup wizard (installs Python deps, creates config, links Pro account) |
| `/resume-builder:job-fit [JD]` | Deterministic master-vs-JD gate (>=70 and zero hard knockouts) before tailoring |
| `/resume-builder:resume [JD]` | Full application: tailored resume + cover letter + scoring + DOCX + tracking |
| `/resume-builder:tailor-resume [JD]` | Resume only (no cover letter) |
| `/resume-builder:cover-letter [JD]` | Cover letter only |
| `/resume-builder:find-jobs [title] [location]` | Discover and score matching jobs from live job boards |
| `/resume-builder:batch-resume` | Process multiple job descriptions in parallel |
| `/resume-builder:writing-coach [file]` | Audit and rewrite resume bullets using 10 writing rules |
| `/resume-builder:resume-team [JD]` | Publish an authorized `resume.md` draft through the native Researcher → Writer → Auditor → Editor workflow |

If running Claude Code locally from the cloned repo, use short names: `/resume`,
`/tailor-resume`, `/find-jobs`, etc. In Codex, invoke `$resume-team`.

### What Works Without Setup

Some Claude Code commands can provide prompt-only previews before setup.
Production resume generation through `/resume-builder:resume`,
`/resume-builder:resume-team`, or Codex `$resume-team` requires Python,
`config.json`, the deterministic candidate-fit preflight, and the evidence,
human-voice, and canonical-integrity audit helpers; those gates are never skipped.

| Command | Works immediately? | With setup? |
|---------|-------------------|-------------|
| `/resume-builder:job-fit` | No — requires the configured master and deterministic preflight | Digest-bound score, threshold, and hard-knockout decision |
| `/resume-builder:resume` | No — the native team and deterministic audits require setup | Full audited resume + automated ATS/HR scoring and DOCX output |
| `/resume-builder:resume-team` / `$resume-team` | No — requires macOS/Linux, the configured master resume, and Python audit helpers | Authorized, digest-verified `resume.md` draft; DOCX/tracker finalization is still pending |
| `/resume-builder:cover-letter` | Yes — the assistant writes the letter | + DOCX output |
| `/resume-builder:writing-coach` | Yes — full writing audit | Same |
| `/resume-builder:find-jobs` | Yes — shows results (no score) | + ATS/HR fit scoring per job |
| `/resume-builder:setup` | Yes — runs the setup wizard | N/A |
| MCP scoring tools | No — needs Python | `score_resume`, `score_ats`, `score_hr`, `score_with_llm`, `explain_score`, `extract_text`, `discover_jobs` |

---

## MCP Tools (7 production-supported surfaces)

After running `/resume-builder:setup`, the MCP scorer auto-starts and provides these tools that Claude Code or Codex can call natively:

| Tool | What It Does |
|------|-------------|
| `score_resume` | Full ATS + HR analysis in one call (recommended) |
| `score_ats` | ATS keyword + semantic scoring (8 components) |
| `score_hr` | HR recruiter simulation (6 factors + F-pattern) |
| `score_with_llm` | LLM-augmented rubric scoring (requires ANTHROPIC_API_KEY) |
| `explain_score` | Actionable improvement suggestions with missing keywords |
| `extract_text` | Extract text from DOCX/PDF/MD/TXT files |
| `discover_jobs` | Search live job boards and score each job against your resume |

All listed MCP tools support **cloud-first scoring** — they try the cloud API first and fall back to local scoring automatically. Legacy direct rewrite endpoints or functions are not production-authorized tailoring paths. The capability-isolated native Resume Team is the sole production rewrite and draft-publication path.

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

Each resume command follows a gated workflow:
- **Phase 0:** Deterministic candidate-fit preflight against the configured master
  and exact JD (>=70, zero hard knockouts). Rejected JDs create no output.
- **Phase 1:** Read-only master/JD planning; prior tailored resumes are not inputs.
- **Phase 2:** Native Researcher → Writer → Auditor → bounded Editor workflow.
- **Phase 3:** Advisory ATS/HR scoring and cover-letter generation where requested.
- **Phase 4:** Evidence, human-voice, and canonical-integrity authorization votes.
- **Phase 5:** Ordered resume DOCX → cover-letter DOCX → tracker finalization.
- **Phase 6:** Artifact verification, cleanup, and report.

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
├── agents/                     # Claude plugin-installed Researcher/Writer/Auditor/Editor definitions
├── .codex/agents/              # Native Codex Researcher/Writer/Auditor/Editor definitions
├── .claude/agents/             # Native Claude Code equivalents
├── .claude-plugin/             # Plugin manifest
│   └── plugin.json             # Plugin metadata (name, version, author)
├── .codex-plugin/              # Codex plugin manifest
│   └── plugin.json             # Codex metadata and install-surface copy
├── .agents/plugins/
│   └── marketplace.json        # Local Codex marketplace entry
├── skills/resume-team/         # Installable Codex Resume Team entrypoint
├── commands/                   # Slash commands (plugin format)
│   ├── setup.md                # One-time setup wizard
│   ├── job-fit.md              # Deterministic master-vs-JD candidate-fit gate
│   ├── resume.md               # Full application (native four-role team)
│   ├── resume-team.md          # Shared fail-closed coordinator protocol
│   ├── tailor-resume.md        # Resume only
│   ├── cover-letter.md         # Cover letter only
│   ├── find-jobs.md            # Job discovery + scoring
│   ├── batch-resume.md         # Batch processing
│   └── writing-coach.md        # Human Voice + Impact rules (0-16)
├── hooks/                      # Plugin hooks
│   └── hooks.json              # SessionStart: checks if scoring is ready
├── .mcp.json                   # MCP server config (auto-starts scorer)
├── .codex.mcp.json             # Codex MCP server config
├── mcp_scorer.py               # MCP scoring server (7 production-supported surfaces)
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
├── multi_agent_team.py         # Vendor-neutral, offline, fail-closed team controller
├── candidate_fit_preflight.py  # Deterministic >=70/no-knockout first gate
├── native_resume_team.py       # Hardened Codex/Claude CLI adapter and draft publisher
├── schemas/
│   ├── resume-team-handoff.schema.json # Strict public role handoff contract
│   ├── resume-team-authorization.schema.json # Three-vote authorization contract
│   ├── resume-team-final-receipt.schema.json # Durable draft-authorization sidecar contract
│   └── resume-team-result.schema.json # Draft-stage runtime result contract
├── tracker_utils.py            # Excel application tracker utilities
├── resume_builder.py           # Retired direct-rewrite CLI; native-team migration guard
├── requirements.txt            # Python dependencies
├── config.example.json         # Config template
├── .env.example                # Environment variable template
├── AGENTS.md                   # Project context for Codex
├── CLAUDE.md                   # Project context for Claude Code
├── LICENSE                     # MIT License
└── README.md                   # You are here
```

---

## Native Resume Team

Codex and Claude Code use the same `resume-team/v2` control flow without API
keys or a third-party orchestration framework. Project custom-agent role files
omit model pins and follow their host's inheritance rules. The hardened runtime
does not inherit transient parent-session or user configuration: by default its
managed CLI model/reasoning selection is unknown and must not be described as a
specific model, profile, or Ultra setting.

- In an installed Claude Code plugin, run `/resume-builder:resume-team [JD]`;
  the four roles load from the plugin-root `agents/` directory.
- In Codex, run `$resume-team [JD]`. The skill uses
  `python native_resume_team.py --host codex` as the authoritative production
  path; each role runs from an empty temporary working directory with tool
  surfaces disabled. A project checkout also registers the four read-only
  custom roles from `.codex/agents/` for interactive inspection, but those
  manual roles are not the capability-isolated publication path.
- The hardened production runtime requires macOS or Linux, Python 3.10+,
  `config.json`, the configured master resume, `candidate_fit_preflight.py`, and
  the local deterministic audit helpers. Windows preflight fails closed with
  `POSIX_RUNTIME_REQUIRED`. No
  external model API key is required for the role agents.
- Codex model selection is unpinned by default. Only when the user explicitly
  requests it may the runtime receive `--model <exact-model>` and/or
  `--reasoning-effort ultra`; it has no profile option. These Codex-only flags
  must not be passed to Claude.

0. Before any role/team invocation or output creation, the coordinator runs
   `candidate_fit_preflight.py` against the exact JD and only the configured
   master resume—never a prior tailored resume. The canonical
   `candidate-fit-policy-v2` report must score at least 70 with trustworthy
   extraction, zero hard knockouts, `passed: true`, and no codes. Scores below 70
   (including 60–69) or hard knockouts return `REJECTED:CANDIDATE_FIT`;
   unavailable, malformed, stale, or mismatched reports return
   `FAILED:CANDIDATE_FIT_PREFLIGHT`. No automatic or manual workflow bypass exists.
1. The coordinator sends the Researcher only the job description.
2. The coordinator sends the Writer only the master resume and validated research artifact.
3. The read-only Auditor checks the exact Writer draft and cannot edit it.
4. The Editor is invoked only for named failures, with at most two corrections
   and a fresh audit after each edit.
5. Draft-stage publication requires the final Auditor PASS plus independent
   evidence, human-voice, and canonical-integrity votes on the same draft digest.

Malformed, stale, replayed, ambiguous, timed-out, unavailable, side-effecting,
or partially published runs fail closed. A runtime `resume-team-result/v2`
`PUBLISHED` result means only
that an authorized, digest-verified `resume.md` draft was atomically written and
read back. It does not mean DOCX generation, tracker update, cleanup, or package
completion. `/resume-builder:resume` and `/resume-builder:tailor-resume` must
complete their ordered DOCX, tracker, artifact-verification, cleanup, and report
gates before claiming package success; a score cannot override an authenticity
gate.

Every `PUBLISHED` result includes the independently reproducible
`candidate_fit_report` and `candidate_fit_report_digest`, plus an inline
`resume-team-final-receipt/v2` `authorization_receipt`, its canonical
`authorization_receipt_digest`, and a durable `authorization_receipt_path`.
The sidecar conforms to `schemas/resume-team-final-receipt.schema.json`.
Downstream finalization resolves the path against the output directory when
relative, requires its resolved parent to be that directory, reads only a regular
non-symlink JSON sidecar, and matches its canonical digest, run/case IDs, exact
passing candidate-fit report/digest, and draft and verified-target digests against
the result, configured master, exact JD, and independently hashed `resume.md`.
It also recomputes the master `source_digest` from `config.json`, recomputes
`job_description_digest` from the fixed sibling `job_description.txt`, and requires
a SHA-256 Researcher artifact plus distinct same-host native Researcher/Auditor IDs.
The receipt must also carry a same-draft PASS `auditor_attestation` and the complete
passing `authorization_report`: no codes, exactly three ordered named PASS votes on
the same draft with distinct IDs, `canonical_digest(report) ==
authorization_digest`, and an identical ordered `vote_invocation_ids` list. The
same check is repeated immediately before DOCX generation. Cleanup preserves the
receipt as durable audit evidence.

Finalization is code-bound: callers retain the captured runtime result, invoke
`final_receipt_verifier.py` with its exact receipt path, digest, and config, and use only
`create_resume_from_md_authorized`, `create_cover_letter_from_md_authorized`, and
`add_application_authorized`. Each wrapper revalidates authorization at the
side-effect boundary; tracker success requires a literal `True` return.

The constructive-provenance experiment established that a self-consistent
model-supplied evidence ledger is not a trust root. Such a ledger is accepted only
when its digest is independently attested. Production therefore anchors every
changed line directly to coordinator-attested, same-role master-resume spans and
applies the closed lexical verifier. `constructive_provenance.py` is a conditional
checker and test artifact, not an alternative publication path.

Claude role definitions and the native runtime use an explicit zero-tool allowlist, so they cannot
actively inspect workspace files; Claude Code may still supply its normal
project startup instructions and basic environment context. Codex custom agents
use a read-only sandbox, which prevents writes but is not a filesystem-read
isolation boundary. In both cases, scoped payloads describe coordinator data
flow rather than every byte of host-provided context. Codex currently has no
documented per-custom-agent built-in-tool denylist, so manual Codex role instructions
prohibit unrelated reads and must not be represented as capability isolation.

## Writing Coach — Rules 0-16

The `/writing-coach` command applies human-voice and impact rules to every bullet point. Core rules include:

1. **Plain Verb Start** — Use direct verbs such as Led, Built, Wrote, Cut, Reviewed, or Directed; AI-cliché openers are banned
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
