"""
Test job discovery across multiple domains/industries.
Verifies that role-family filtering works correctly for any resume type,
not just clinical research.
"""
from dotenv import load_dotenv

load_dotenv(override=True)

from job_discovery import _detect_text_domain, analyze_resume_for_search

# ---------------------------------------------------------------------------
# Mini resumes for 4 different domains
# ---------------------------------------------------------------------------

RESUMES = {
    "Software Engineer (Senior)": """
John Smith
San Francisco, CA | john@email.com | github.com/jsmith

SUMMARY
Senior Software Engineer with 8 years building distributed backend systems.
Expert in Python, Go, Kubernetes, AWS. Led teams of 5-8 engineers.

EXPERIENCE
Senior Software Engineer | Stripe | San Francisco, CA | 2021–Present
• Designed payment processing microservices handling $2B/day in transactions
• Reduced API latency by 40% through caching layer redesign
• Mentored 4 junior engineers on distributed systems best practices

Software Engineer II | Airbnb | San Francisco, CA | 2018–2021
• Built search ranking algorithms in Python serving 100M+ daily requests
• Shipped real-time pricing engine using Kafka and Spark streaming

EDUCATION
B.S. Computer Science | UC Berkeley | 2016

SKILLS
Python, Go, Java, Kubernetes, Docker, AWS, GCP, Kafka, Spark, PostgreSQL, Redis
""",

    "Financial Analyst (VP-level)": """
Sarah Chen
New York, NY | sarah@email.com

SUMMARY
VP-level Investment Banking Associate with 7 years in M&A and leveraged finance.
Track record of $4B+ in closed transactions across technology and healthcare.

EXPERIENCE
Vice President, Investment Banking | Goldman Sachs | New York, NY | 2020–Present
• Led 12 M&A transactions totaling $4.2B in deal value
• Built LBO models, DCF analyses, and merger consequence analyses
• Managed client relationships with Fortune 500 CFOs

Associate, Investment Banking | Morgan Stanley | New York, NY | 2017–2020
• Executed $1.8B leveraged buyout for healthcare technology company
• Prepared pitch books and fairness opinions for board presentations

EDUCATION
MBA | Wharton School, University of Pennsylvania | 2017
B.S. Finance | NYU Stern | 2015

SKILLS
Financial modeling, LBO analysis, DCF, M&A, capital markets, Bloomberg, FactSet
""",

    "Marketing Director": """
Maria Rodriguez
Chicago, IL | maria@email.com | linkedin.com/in/mrodriguez

SUMMARY
Marketing Director with 10 years driving B2B SaaS growth. Expert in
demand generation, content marketing, and product-led growth strategies.
Grew pipeline 3x at two companies.

EXPERIENCE
Director of Marketing | HubSpot | Chicago, IL | 2021–Present
• Led team of 15, managing $8M annual marketing budget
• Grew MQL pipeline by 280% through ABM campaigns and SEO
• Launched 4 product lines, generating $22M in first-year ARR

Senior Marketing Manager | Salesforce | Chicago, IL | 2018–2021
• Managed demand gen programs driving $15M in qualified pipeline
• Produced 40+ webinars averaging 2,000 attendees each

EDUCATION
MBA | University of Chicago Booth | 2018
B.A. Communications | Northwestern University | 2013

SKILLS
HubSpot, Marketo, Salesforce, Google Ads, SEO, content strategy, ABM, analytics
""",

    "Molecular Biologist (PhD)": """
Dr. Emily Park
Boston, MA | emily@email.com

SUMMARY
Postdoctoral Researcher in molecular biology with expertise in CRISPR gene editing
and single-cell RNA sequencing. 8 peer-reviewed publications. Seeking industry role.

EXPERIENCE
Postdoctoral Researcher | Broad Institute of MIT/Harvard | 2022–Present
• Developed CRISPR-Cas9 screens identifying 47 novel cancer driver genes
• Applied single-cell RNA-seq to characterize tumor microenvironment
• Mentored 3 PhD students in experimental design and data analysis

PhD Researcher | Johns Hopkins University | 2017–2022
• Dissertation: "CRISPR-based functional genomics in leukemia"
• Mastered flow cytometry, western blot, cell culture, PCR, cloning

EDUCATION
Ph.D. Molecular Biology | Johns Hopkins University | 2022
B.S. Biochemistry | University of Michigan | 2017

SKILLS
CRISPR, scRNA-seq, flow cytometry, western blot, PCR, Python (bioinformatics),
R (Seurat, DESeq2), cell culture, mouse models
""",
}

# ---------------------------------------------------------------------------
# Test 1: AI profile extraction for each resume
# ---------------------------------------------------------------------------
print("=" * 70)
print("TEST 1: AI PROFILE EXTRACTION — does AI correctly classify each resume?")
print("=" * 70)

for role, resume in RESUMES.items():
    print(f"\n--- {role} ---")
    heuristic = _detect_text_domain(resume[:2000])
    print(f"  Heuristic domain : {heuristic}")

    profile = analyze_resume_for_search(resume, include_queries=False)
    if profile:
        print(f"  AI role_type     : {profile.get('role_type')}")
        print(f"  AI domain        : {profile.get('domain')}")
        print(f"  AI career_level  : {profile.get('career_level')}")
        print(f"  AI job_zone      : {profile.get('job_zone')}")
        print(f"  AI role_family   : {profile.get('role_family')}")
        print(f"  AI excluded_roles: {profile.get('excluded_roles')}")
        print(f"  AI specialties   : {profile.get('specialties')}")
    else:
        print("  [AI analysis failed — no API key or error]")

# ---------------------------------------------------------------------------
# Test 2: Live job search for 2 domains
# ---------------------------------------------------------------------------
print("\n\n" + "=" * 70)
print("TEST 2: LIVE JOB SEARCH — correct roles returned?")
print("=" * 70)

from job_discovery import discover_jobs

test_cases = [
    ("Software Engineer (Senior)", "Software Engineer", "San Francisco"),
    ("Financial Analyst (VP-level)", "Investment Banking Associate", "New York"),
    ("Marketing Director", "Marketing Director", "Chicago"),
]

for resume_key, job_title, location in test_cases:
    resume = RESUMES[resume_key]
    print(f"\n--- Searching '{job_title}' in {location} ---")
    result = discover_jobs(resume, job_title=job_title, location=location, max_results=5)

    ai = result.get('ai_analysis', {})
    if ai:
        print(f"  Profile: {ai.get('role_type')} | excluded: {ai.get('excluded_roles')}")

    jobs = result.get('jobs', [])
    print(f"  Results: {len(jobs)} jobs")
    for j in jobs:
        title = j['title'][:50].encode('ascii', 'replace').decode()
        company = j['company'][:30].encode('ascii', 'replace').decode()
        ats = j.get('ats_score', 'N/A')
        hr = j.get('hr_score', 'N/A')
        print(f"    #{j['rank']}  {title:<50}  ATS:{str(ats):>5}%  HR:{str(hr):>5}%")
        print(f"         {company}")
