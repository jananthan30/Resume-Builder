---
description: One-time setup for Resume Builder dependencies, config, cloud scoring, and optional LLM features.
---

# Resume Builder — One-Time Setup

Run this command once after installing the plugin to get started.

## Input
$ARGUMENTS

## Instructions

You are helping the user set up the Resume Builder plugin. Guide them through the steps below.

### Step 1: Check Python

Run `python --version` (or `python3 --version` on Mac/Linux).

- If Python 3.10+ is installed, proceed to Step 2.
- If Python is NOT installed, tell the user:

```
The scoring engine requires Python 3.10 or later.

Download it from: https://www.python.org/downloads/

During installation:
- CHECK "Add Python to PATH" (Windows)
- Mac/Linux: Use your package manager (brew install python3)

After installing Python, run /resume-builder:setup again.
```

Stop here if Python is not installed.

### Step 2: Choose Install Type

Ask the user which setup they prefer:

**Option A: Quick Setup (Recommended for most users)**
- Cloud-based scoring — no heavy downloads
- Installs only 2 small packages (~5 seconds)
- Scoring happens on our cloud server (5 free scores, then Pro $12/month for unlimited)
- AI rewriting available with your own Anthropic API key

**Option B: Full Local Setup (For developers / offline use)**
- Installs all scoring engines locally (~500MB, takes 2-3 minutes)
- Scoring runs on your machine — no cloud needed, no limits
- Requires more disk space and RAM

### Step 3A: Quick Setup (Cloud)

```bash
pip install -r requirements-plugin.txt
```

If `pip` is not found, try `pip3` or `python -m pip`. If needed, use the `requirements-plugin.txt` file from the installed plugin root.

This installs `fastmcp` (MCP protocol) and `anthropic` (for LLM features). All ATS/HR scoring goes through the cloud API automatically.

### Step 3B: Full Local Setup

```bash
pip install -r requirements.txt
```

Use the `requirements.txt` file from the installed plugin root if needed. This takes 1-3 minutes (downloads sentence-transformers model ~80MB).

Then download NLTK data:

```bash
python -c "import nltk; nltk.download('wordnet'); nltk.download('punkt_tab')"
```

### Step 4: Set Up Configuration

Check if the user has a `config.json` in their project. If not, create one from `config.example.json`:

1. Read `config.example.json` from the plugin directory
2. Ask the user for their details:
   - Full name
   - Credentials (e.g., M.D., MBA, CPA — or leave blank)
   - Email
   - Phone
   - LinkedIn URL
   - Path to their master resume file (DOCX, PDF, or Markdown with their full work history)
3. Create `config.json` with their answers

### Step 5: (Optional) LLM Features Setup

Ask the user if they want to enable AI-powered scoring and resume rewriting.

If yes:
1. They need an Anthropic API key from https://console.anthropic.com/
2. Create a `.env` file with: `ANTHROPIC_API_KEY=sk-ant-...`
3. This enables the `score_with_llm` and `rewrite_resume` tools

If no, skip this step. The ATS and HR scorers work without an API key.

### Step 6: Verify

For Quick Setup, run:
```bash
python -c "import fastmcp; print('Plugin ready! Cloud scoring active.')"
```

For Full Setup, run:
```bash
python -c "import ats_scorer; import hr_scorer; print('Full scoring engine ready!')"
```

If successful, tell the user:

```
Setup complete! You can now use:

  /resume-builder:resume [paste job description]    — Full resume + cover letter package
  /resume-builder:tailor-resume [paste JD]          — Resume only
  /resume-builder:cover-letter [paste JD]           — Cover letter only
  /resume-builder:writing-coach [resume file]       — Improve resume writing quality

The scoring engine is active and will automatically score your resumes.
Cloud scoring: 5 free scores included. Upgrade at https://resume-scorer-web.streamlit.app
```

If it fails, show the error and suggest fixes.
