# Resume Builder - Codex Context

## Overview

Resume Builder creates tailored resumes, cover letters, job-fit reports, DOCX application packages, and tracker updates from job descriptions.

Core targets:
- ATS score: 75-85%
- HR score: 70%+
- Authenticity: never invent experience, metrics, titles, dates, education, publications, certifications, or memberships
- Output: ATS/Workday-compatible DOCX files

## Codex Plugin Surface

Codex uses:
- `.codex-plugin/plugin.json` for plugin metadata
- `.agents/plugins/marketplace.json` for the local marketplace entry
- `commands/` for plugin slash commands
- `.codex.mcp.json` for the Codex MCP scorer server

Available commands:
- `/resume-builder:setup`
- `/resume-builder:job-fit [job description]`
- `/resume-builder:resume [job description]`
- `/resume-builder:tailor-resume [job description]`
- `/resume-builder:cover-letter [job description]`
- `/resume-builder:find-jobs [title] [location]`
- `/resume-builder:batch-resume`
- `/resume-builder:writing-coach [file]`

## Operating Rules

- Read `config.json` for `master_resume_path` before tailoring or scoring.
- For `.docx` resumes, use the MCP `extract_text` tool when available, or Python with `python-docx`.
- Use `rg --files` or `find` for file discovery.
- Start the scorer server with `python scorer_server.py --port 8100` when REST scoring is needed and `http://localhost:8100/health` is not already healthy.
- Preserve canonical resume facts exactly: job titles, company names, dates, education, publications, certifications, and memberships.
- Every master resume role must remain in tailored resumes unless the user explicitly asks otherwise.
- Generated `resume.md` and `cover_letter.md` must not use markdown bold markers; DOCX generation handles formatting.
- Delete transient markdown/state files only after the corresponding DOCX files and tracker updates are verified.
