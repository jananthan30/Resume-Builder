"""Quick local test of job discovery with master resume."""
import os, sys
from dotenv import load_dotenv
load_dotenv(override=True)

import json
_cfg = json.load(open('config.json'))
with open(_cfg.get('master_resume_path', 'master_resume.md'), encoding='utf-8') as f:
    resume_text = f.read()

from job_discovery import discover_jobs, _detect_text_domain

# --- Check heuristic domain detection ---
detected = _detect_text_domain(resume_text[:2000])
print(f"Heuristic domain from resume: {detected}\n")


def show_results(result):
    ai = result.get('ai_analysis')
    if ai:
        print(f"  AI role_type    : {ai.get('role_type')}")
        print(f"  AI domain       : {ai.get('domain')}")
        print(f"  AI career_level : {ai.get('career_level')}")
        print(f"  AI role_family  : {ai.get('role_family')}")
        print(f"  AI excluded     : {ai.get('excluded_roles')}")
        print(f"  AI specialties  : {ai.get('specialties')}")
        print(f"  Queries used    : {ai.get('search_queries_used')}")
        print()

    jobs = result.get('jobs', [])
    print(f"  Jobs returned: {len(jobs)}\n")
    for j in jobs:
        ats = j.get('ats_score', 'N/A')
        hr  = j.get('hr_score', 'N/A')
        smin = j.get('salary_min')
        smax = j.get('salary_max')
        sal = f"${smin//1000}k-${smax//1000}k" if smin and smax else (f"${smin//1000}k+" if smin else "Not listed")
        title = j['title'][:45].encode('ascii', 'replace').decode()
        company = j['company'][:35].encode('ascii', 'replace').decode()
        print(f"  #{j['rank']:2d}  {title:<45}  ATS:{str(ats):>5}%  HR:{str(hr):>5}%  {sal}")
        print(f"       {company}  |  {j['location']}")
        kw = j.get('ats_detail', {}).get('matched_keywords', [])[:5]
        if kw:
            print(f"       Matched: {kw}")
        print()
    print(f"  Attribution: {result.get('attribution')}\n")


# --- Test 1: AI picks search (no job title) ---
print('=== Test 1: No job title — AI picks search queries ===\n')
r1 = discover_jobs(resume_text, job_title='', location='New York', max_results=5)
show_results(r1)

# --- Test 2: Specific title ---
print('=== Test 2: Clinical Research Associate, New York ===\n')
r2 = discover_jobs(resume_text, job_title='Clinical Research Associate', location='New York', max_results=5)
show_results(r2)

# --- Test 3: Domain filter test — should not return data scientist jobs ---
print('=== Test 3: Physician — domain filter should block data scientist / software jobs ===\n')
r3 = discover_jobs(resume_text, job_title='Physician', location='New York', max_results=5)
show_results(r3)
