# Azure Solution Architect daily job agent

Emails you new Azure Solution/Cloud Architect roles every morning at 10:00 AM IST
(India/Mumbai, UK, Germany, Ireland, Netherlands, Canada, Australia, UAE/Dubai).
Abroad roles must mention visa sponsorship / relocation and not say "no sponsorship".
Jobs are ranked by how many of your resume skills they mention and are never sent twice.

## Setup (about 10 minutes)
1. **JSearch API key**: sign up at app.openwebninja.com/api/jsearch (free tier) and copy your key; add it as the secret `OPENWEBNINJA_API_KEY`. (RapidAPI key via `RAPIDAPI_KEY` also works as a fallback.)
2. **Gmail App Password**: Google Account > Security > 2-Step Verification > App passwords.
3. **GitHub**: create a *private* repo, upload these files (keep the `.github/workflows` folder).
4. Repo > Settings > Secrets and variables > Actions > add:
   `RAPIDAPI_KEY`, `SMTP_USER` (your Gmail), `SMTP_PASS` (app password), `MAIL_TO` (where to receive).
5. Actions tab > "Daily Azure job digest" > Run workflow to test. It then runs on its own at 10:00 IST.

## Local test
    pip install -r requirements.txt
    export RAPIDAPI_KEY=... DRY_RUN=1
    python job_agent.py        # writes preview.html instead of emailing

## Tuning
- Skills (now taken from your resume), role titles, countries and visa keywords: top of `job_agent.py`.
- Role titles searched: Azure Solution Architect (Mon/Wed/Fri), Azure Cloud Architect (Tue), and a weekly rotation of Migration / Infrastructure / Technical / Enterprise Cloud / AKS Platform architect titles (Thu). Set env `QUERIES_PER_COUNTRY=2` or `3` to search more titles daily if your API plan allows.
- Free API plans have a monthly request cap; 8 countries x weekdays fits a ~200/month plan. Check your plan before switching to daily.
- GitHub cron can start a few minutes late.

## Match score
Every job gets a score out of 100: resume skills 55 + title fit 20 + visa/relocation 15 + experience fit 10.
Jobs scoring 88 or more are shown first as BEST MATCH. Change the threshold with the `BEST_SCORE` env var.

## Application kits (resume + cover letter for 80+ matches)
`apply_agent.py` runs right after the job digest. For each NEW job scoring 80 or more it writes a resume/CV and cover letter
tailored to the job and formatted to that country's norms (UK/IE/NL/DE CV, Canadian resume, Australian resume, UAE CV, India resume),
then emails them as attachments with the apply link and a checklist. A copy is also kept under Actions > run > Artifacts.

* It never submits applications: you review and click Apply.
* Facts come only from `master_resume.json`. Numbers, skills and certifications in the output are checked against it.
* Needs the secret `ANTHROPIC_API_KEY`. Fill nationality / notice_period / photo_path in `master_resume.json` if you want them shown (UAE, India, Germany, Netherlands).
* Settings: `APPLY_MIN_SCORE` (80), `MAX_KITS_PER_RUN` (5), `CLAUDE_MODEL`, `NEEDS_SPONSORSHIP`.

## Auto-apply agent + application report (`auto_apply_agent.py`)
Runs after the digest and replaces the kit step. For each new job scoring 80+ it prepares the tailored resume and cover letter
(PDF), then:
* **Posting has an application e-mail address** -> sends the application from your Gmail (Cc to you) when AUTO_APPLY is on and all safety checks pass.
* **Online form / portal only** -> NOT automated (those sites forbid bots, CAPTCHAs); the documents are attached to your report with the apply link.
* You always get ONE report: applied, would-apply (preview), ready for you to apply, held back (with the reason), skipped, plus `applications_tracker.csv`.

**Turn it on safely**
1. First runs are PREVIEW: nothing is sent to employers. Check the report and the attached documents.
2. When you're happy: repo > Settings > Secrets and variables > Actions > **Variables** > New variable `AUTO_APPLY` = `true`.
3. Set it back to `false` (or delete it) to stop.

Safety rules: 3 applications per run max, never the same job twice, never the same recipient within 14 days, never your current employer
(`BLOCKLIST_COMPANIES`), sponsorship must be mentioned (except India), full job description required, and no more than 2 requirements your
resume doesn't show (`MAX_GAPS_AUTO`). Anything that fails a check is held back and shown in the report instead.

## Apply helper (`apply_helper.py`) - runs on your own computer
Finishes the jobs that could not be applied to by e-mail. It opens each apply page in Chrome, fills your name, e-mail, phone,
LinkedIn and location, uploads the tailored resume and cover letter, then STOPS so you can review, answer the remaining questions and
click Submit yourself. It never clicks Submit/Next, never ticks consent boxes and never answers work-authorisation, salary or diversity questions.

Setup (once, Windows): `py -m pip install playwright` then `py -m playwright install chromium`
Each time:
1. GitHub > Actions > the latest run > Artifacts > download **application-kits** and unzip it into a folder.
2. Copy `apply_helper.py` and `master_resume.json` into the same folder.
3. Run `py apply_helper.py` (add `--chrome` to use your installed Google Chrome).
4. For each job press Enter to open it, check the form, submit it yourself, then type `a` so it is logged in `applied_manually.csv`.
Log in to Workday/Greenhouse/other sites once in the helper's window; the login is remembered in a separate profile folder.
Put your LinkedIn URL in `master_resume.json` ("linkedin") if you want it filled. LinkedIn Easy Apply is NOT automated (it is against LinkedIn's rules).
