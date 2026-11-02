# Azure Solution Architect daily job agent

Emails you new Azure Solution/Cloud Architect roles every morning at 10:00 AM IST
(India/Mumbai, UK, Germany, Ireland, Netherlands, Canada, Australia, UAE/Dubai).
Abroad roles must mention visa sponsorship / relocation and not say "no sponsorship".
Jobs are ranked by how many of your resume skills they mention and are never sent twice.

## Setup (about 10 minutes)
1. **JSearch API key**: sign up at rapidapi.com, subscribe to "JSearch" (free plan), copy the key.
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
