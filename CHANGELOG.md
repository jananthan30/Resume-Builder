# Changelog

All notable changes to ResumeHQ (engine, plugin, MCP server) are documented
here. Versioning starts at v1.2.0 — earlier history exists in git but predates
release tagging.

## [1.2.0] — 2026-08-09

### Added
- Password reset flow: `/auth/forgot` + `/auth/reset` with single-use,
  hash-bound tokens and transactional email
- Custom domain: the hosted app moved to https://getresumehq.com
- Demo GIF and comparison table in the README
- Native four-role Resume Team runtime with fail-closed authorization gates
- Job discovery with candidate-fit gating; universal resume format support
  (DOCX/PDF/MD/TXT, scanned-PDF OCR)

### Fixed
- HR skills factor no longer collapses to 0 on fragment-style JD requirement
  bullets (compound lines are atomized before matching), and the skills
  ceiling is calibrated to action-context demonstration
- Candidate-fit gate false rejections measured and fixed across 61 real JDs
- Stripe webhooks fail loudly on signature errors; production refuses to
  serve on development defaults
- `mcp_scorer` usage-limit paths called an undefined helper and crashed
  instead of returning the friendly limit message

### Changed
- CI runs on `master` (was pointed at a non-existent `main`); Fly deploys are
  manual-only by design (the deploy kit is private)
- Lint debt cleared to zero with legacy-pattern suppressions documented in
  `pyproject.toml`
