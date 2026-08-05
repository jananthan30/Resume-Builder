#!/usr/bin/env bash
# Post-deploy smoke test for the hosted ResumeHQ backend.
#
# Read-only except for one throwaway account it registers (and the free-tier
# score that account spends). Never sends payment, never touches real users.
#
#   ./scripts/smoke_agent_live.sh                     # against production
#   BASE=http://localhost:8100 ./scripts/smoke_agent_live.sh
#
# Exit code 0 = every required check passed.

set -uo pipefail

BASE="${BASE:-https://resume-scorer.fly.dev}"
EMAIL="smoke-$(date +%s)@resumehq-smoke.invalid"
PASS="smoke-password-123"
PASSED=0
FAILED=0

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
check() { # check <label> <actual> <expected>
  if [ "$2" = "$3" ]; then
    printf '  ok    %-42s %s\n' "$1" "$2"; PASSED=$((PASSED+1))
  else
    printf '  FAIL  %-42s got=%s want=%s\n' "$1" "$2" "$3"; FAILED=$((FAILED+1))
  fi
}
code() { curl -s -o /dev/null -w '%{http_code}' -m 60 "$@"; }

say "ResumeHQ backend smoke — $BASE"

# ---------------------------------------------------------------- liveness
HEALTH=$(curl -s -m 90 "$BASE/health")
check "GET /health" "$(printf '%s' "$HEALTH" | grep -c '"status":"ok"')" "1"
printf '        %s\n' "$(printf '%s' "$HEALTH" | cut -c1-160)"

# A revoked Anthropic key is a non-empty, correctly-shaped string that fails
# only when the API is called. That is exactly how the production key stayed
# dead from March to August 2026 unnoticed -- so assert on the live verdict,
# not on the key merely being configured.
AGENT_KEY=$(printf '%s' "$HEALTH" | sed -n 's/.*"agent":{"api_key":"\([^"]*\)".*/\1/p')
check "paid AI features are live" "${AGENT_KEY:-absent}" "ok"
if [ "${AGENT_KEY:-absent}" != "ok" ]; then
  printf '        %s\n' "$(printf '%s' "$HEALTH" | sed -n 's/.*"detail":"\([^"]*\)".*/-> \1/p')"
  printf '        fix: create a key at console.anthropic.com, then\n'
  printf '        flyctl secrets set ANTHROPIC_API_KEY="sk-ant-..." --app resume-scorer\n'
fi

# ------------------------------------------------------------------- auth
say "Account lifecycle (throwaway user)"
REG=$(curl -s -m 60 -X POST "$BASE/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}")
TOKEN=$(printf '%s' "$REG" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
check "POST /auth/register returns a token" "$([ -n "$TOKEN" ] && echo yes || echo no)" "yes"
[ -z "$TOKEN" ] && { printf '\n  cannot continue without a token\n'; exit 1; }
AUTH="Authorization: Bearer $TOKEN"

check "GET /auth/usage" "$(code -H "$AUTH" "$BASE/auth/usage")" "200"

# --------------------------------------------------------------- tracker
say "Tracker on SQL (add -> list -> outcome -> insights)"
ADD=$(curl -s -m 60 -X POST "$BASE/tracker/add" -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"company":"Smoke Co","job_title":"Smoke Role","fit_label":"MEETS","target_tier":"Sr"}')
APP_ID=$(printf '%s' "$ADD" | sed -n 's/.*"id":\([0-9]*\).*/\1/p' | head -1)
check "POST /tracker/add" "$([ -n "$APP_ID" ] && echo yes || echo no)" "yes"
check "GET /tracker" "$(code -H "$AUTH" "$BASE/tracker")" "200"
if [ -n "$APP_ID" ]; then
  check "POST /tracker/{id}/outcome" \
    "$(code -X POST -H "$AUTH" -H 'Content-Type: application/json' \
       -d '{"rejection_reason":"screen_reject","interview_stage":1}' \
       "$BASE/tracker/$APP_ID/outcome")" "200"
  check "invalid rejection reason rejected" \
    "$(code -X POST -H "$AUTH" -H 'Content-Type: application/json' \
       -d '{"rejection_reason":"made_up_reason"}' \
       "$BASE/tracker/$APP_ID/outcome")" "422"
fi
check "GET /insights/pipeline" "$(code -H "$AUTH" "$BASE/insights/pipeline")" "200"

# --------------------------------------------------------- tier gating
say "Tier gating (this account is free — paid features must refuse)"
check "POST /rewrite refuses free tier" \
  "$(code -X POST -H "$AUTH" -H 'Content-Type: application/json' \
     -d '{"jd_text":"Registered nurse wanted for an ICU role."}' "$BASE/rewrite")" "403"
check "POST /agent/tailor refuses free tier" \
  "$(code -X POST -H "$AUTH" -H 'Content-Type: application/json' \
     -d '{"jd_text":"Registered nurse wanted for an ICU role."}' "$BASE/agent/tailor")" "403"
check "POST /agent/cover-letter refuses free tier" \
  "$(code -X POST -H "$AUTH" -H 'Content-Type: application/json' \
     -d '{"jd_text":"Registered nurse wanted for an ICU role."}' "$BASE/agent/cover-letter")" "403"
check "GET /agent/runs/{unknown} is 404" \
  "$(code -H "$AUTH" "$BASE/agent/runs/definitely-not-a-real-run")" "404"

# ------------------------------------------------------------- isolation
say "Isolation and boundaries"
check "unauthenticated /tracker is refused" \
  "$([ "$(code "$BASE/tracker")" = "200" ] && echo leaked || echo refused)" "refused"
check "unauthenticated /insights is refused" \
  "$([ "$(code "$BASE/insights/pipeline")" = "200" ] && echo leaked || echo refused)" "refused"

# ------------------------------------------------------------- scoring
say "Deterministic scoring still works (free tier)"
check "POST /score/both" \
  "$(code -X POST -H "$AUTH" -H 'Content-Type: application/json' \
     -d '{"resume_text":"EXPERIENCE\nStaff nurse, ICU, 5 years. BLS certified.","jd_text":"Seeking an ICU registered nurse with BLS certification and 3+ years of experience."}' \
     "$BASE/score/both")" "200"

# -------------------------------------------------------------- summary
say "Result"
printf '  passed: %s   failed: %s\n' "$PASSED" "$FAILED"
printf '  throwaway account: %s (free tier, safe to leave)\n\n' "$EMAIL"
[ "$FAILED" -eq 0 ] || exit 1
