# syntax=docker/dockerfile:1
#
# ResumeHQ backend (resume-scorer) — reconstructed deployment image.
#
# The original Dockerfile for this app was lost (not in this repo, not on
# this machine, not on the Fly host — see
# docs/superpowers/plans/private-layer-notes.md, finding A). This is a fresh
# reconstruction based on:
#   - `flyctl config show -a resume-scorer`   (internal_port 8100, /data mount)
#   - `flyctl ssh console -a resume-scorer -C "python3 --version; ls /app; \
#      pip freeze"` on the live machine (2026-08-05): Python 3.11.15, a flat
#      /app layout, and the exact production dependency set
#   - reading scorer_server.py's own `if __name__ == "__main__":` block
#
# It does NOT bake any secrets. Every value in fly.toml's [env] block is
# non-secret app config; real secrets (STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET,
# STRIPE_PRICE_ID[_ULTRA], SCORER_JWT_SECRET, SCORER_ADMIN_SECRET,
# ANTHROPIC_API_KEY, ADZUNA_APP_ID/KEY) must be provided via `fly secrets set`
# BEFORE the first deploy with this image — see the checklist in
# docs/superpowers/plans/private-layer-notes.md. The currently-running image
# has these baked in from its lost build context instead; that is what this
# rebuild intentionally stops doing.

FROM python:3.11-slim
# Live machine reported Python 3.11.15 (flyctl ssh console, 2026-08-05).
# python:3.11-slim tracks the latest 3.11.x patch, which satisfies that.

WORKDIR /app

# No apt packages are installed. Every package in the production `pip freeze`
# (torch, numpy, scipy, pillow, lxml, cryptography, spacy/thinc/blis/
# murmurhash/cymem/preshed, etc.) ships manylinux wheels for this base image's
# glibc/arch, so nothing here needs a compiler or system headers. Revisit if
# a future dependency bump introduces a source-only package.

# --- Python dependencies -------------------------------------------------
# Install PyTorch's CPU wheel explicitly, before everything else.
# sentence-transformers (in requirements.txt) depends on torch transitively,
# and PyPI's default Linux wheel for a plain `torch` install resolves to a
# CUDA-enabled build dragging in several GB of nvidia-*/cu* packages that
# this shared-cpu-1x / 1GB Fly machine can't use and shouldn't have to
# download. `pip freeze` on the live machine confirms production actually
# runs torch==2.10.0+cpu — a build that only exists on PyTorch's dedicated
# CPU wheel index — so install that exact build first, before requirements
# resolution, so a plain `pip install -r requirements.txt` can't silently
# substitute the CUDA build.
RUN pip install --no-cache-dir torch==2.10.0+cpu \
    --index-url https://download.pytorch.org/whl/cpu

# Copy dependency manifests before app code so this layer caches across
# code-only changes. requirements.txt is the public plugin's dependency
# list; requirements-cloud.txt is the private/cloud-only layer (stripe,
# PyJWT, anthropic — see that file for exact pins and why).
COPY requirements.txt requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-cloud.txt

# --- Application code ------------------------------------------------------
# Copies the app tree (see .dockerignore for exclusions — read its trailing
# notes before changing it). This intentionally INCLUDES cloud/ (private
# billing/auth/db/quotas layer — gitignored from the public repo, but
# imported directly by scorer_server.py and agent/tools.py, so it must ship)
# and agent/ (native team adapter + tool registry), alongside the scorer/
# audit modules they both pull in (ats_scorer, hr_scorer, llm_scorer,
# job_fit_scorer, candidate_fit_preflight, evidence_audit, human_voice_audit,
# resume_integrity_audit, multi_agent_team, claim_provenance_audit, data/,
# taxonomy/, schemas/, skills/).
COPY . .

EXPOSE 8100

# scorer_server.py's `if __name__ == "__main__":` block is not a thin
# `uvicorn scorer_server:app` wrapper — it parses --host/--port/
# --cors-origins/--rate-limit itself, attaches CORS middleware, applies them
# to the module's `_config`, and only THEN calls
# `uvicorn.run(app, host=args.host, port=args.port, log_level="info")`.
# Invoking through the `uvicorn` CLI directly would skip all of that (the
# `__main__` guard never executes on import), so the entrypoint must run the
# script itself, not `uvicorn scorer_server:app`.
#
# argparse defaults --host to 127.0.0.1 (loopback — unreachable from Fly's
# proxy and the /health check), so host/port are passed explicitly here.
# SCORER_HOST/SCORER_PORT (set in fly.toml's [env]) are honored if present,
# otherwise this falls back to the 0.0.0.0:8100 that fly.toml's
# http_service.internal_port and checks.health both require.
# --require-auth is intentionally NOT passed: `_config["require_auth"]`
# already reads cloud/config.py's settings object, which is driven by the
# SCORER_REQUIRE_AUTH env var fly.toml sets.
CMD ["sh", "-c", "python3 scorer_server.py --host \"${SCORER_HOST:-0.0.0.0}\" --port \"${SCORER_PORT:-8100}\""]
