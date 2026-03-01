# Generate Cover Letter Only

Create a compelling one-page cover letter for a job application.

## Job Description
$ARGUMENTS

## Instructions

You are an expert career coach and professional writer. The user has provided a job description above.

**Your task:**

### Phase 1: Setup

1. **Extract company name and job title** from the job description

2. **Search for a similar existing resume** in the `applications/` folder to understand what was already tailored:
   - List all subfolders in `applications/`
   - Compare folder job titles against the NEW job description's title and requirements
   - **If a similar resume is found**: Read that resume to understand the applicant's tailored background for this type of role
   - **If NO similar resume is found**: Read the master resume (path from `config.json` → `master_resume_path`, or glob for `*MASTER*RESUME*.md`) to understand the applicant's background
   - Always also read the master resume for canonical details

3. **Create output folder** at `applications/{CompanyName} - {JobTitle}/` (if not exists)

4. **Save the job description** as `job_description.txt` (if not exists)

### Phase 2: Cover Letter Generation

5. **Write a persuasive one-page cover letter** following this structure:

   **Opening Hook (1 paragraph):**
   - Start with genuine enthusiasm
   - Immediately highlight strongest qualification match
   - Show you understand the role

   **Value Proposition (2 paragraphs):**
   - Connect 3-4 specific experiences to key requirements
   - Use brief STAR format (Situation, Task, Action, Result)
   - Include quantifiable achievements where possible
   - Use keywords from the job description

   **Company Connection (1 paragraph):**
   - Reference something specific about the company
   - Show research and genuine interest
   - Align with company values/mission

   **Strong Close (1 paragraph):**
   - Express confidence
   - Clear call to action
   - Thank them for consideration

6. **Format requirements:**
   - Maximum ONE page (350-400 words)
   - Professional but personable tone
   - NO placeholder text like [Your Address]
   - Ready to send immediately

### Phase 3: DOCX Creation & Cleanup

7. **Save cover letter** as `cover_letter.md` first

8. **Create DOCX**:
   - `{Name}_Cover_Letter_{Company}.docx` - Professional formatting

9. **Delete `cover_letter.md`** after successful DOCX creation

### Phase 4: Final Output

10. **Display the full cover letter** text for review

11. **List generated files**:
    - `{Name}_Cover_Letter_{Company}.docx`
    - `job_description.txt`

After completion, display word count and confirm it's within the 400-word limit.
