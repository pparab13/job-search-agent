"""
Azure Solution Architect daily job-alert agent.

Searches Google-for-Jobs-style aggregated listings (via the JSearch API on RapidAPI)
for each target country, keeps only roles that mention visa sponsorship / relocation
support (except India), ranks them by how many of your resume skills they mention,
removes jobs already emailed before, and emails you an HTML digest.

Env vars (set as GitHub Actions secrets or in a local .env export):
  OPENWEBNINJA_API_KEY - JSearch key from app.openwebninja.com (recommended, used if set)
  RAPIDAPI_KEY         - OR a RapidAPI key subscribed to "JSearch" (fallback)
  SMTP_USER      - sender Gmail address
  SMTP_PASS      - Gmail App Password (not your normal password)
  MAIL_TO        - where to send the digest (defaults to SMTP_USER)
"""
import json
import os
import re
import smtplib
import ssl
from urllib.parse import quote_plus
import sys
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# CONFIG - edit freely
# ----------------------------------------------------------------------------
# Target role keywords from your resume. One query per country per run keeps you inside a
# free API quota, so Mon/Wed/Fri use the primary title, Tue uses "Azure Cloud Architect", and
# Thu rotates through the other titles from your resume (one per week).
PRIMARY_QUERY = "Azure Solution Architect"
TUESDAY_QUERY = "Azure Cloud Architect"
ROTATING_QUERIES = [
    "Azure Migration Architect",
    "Infrastructure Architect Azure",
    "Azure Technical Architect",
    "Enterprise Cloud Architect Azure",
    "AKS Container Platform Architect",
    "Cloud Architect Azure",
]
QUERIES_PER_COUNTRY = int(os.getenv("QUERIES_PER_COUNTRY", "1"))   # raise to 2-3 on a paid API plan

# (label, JSearch country code, location text, needs_visa_filter)
TARGETS = [
    ("India (Mumbai)", "in", "Mumbai, India", False),   # home market: no sponsorship needed
    ("United Kingdom", "gb", "United Kingdom", True),
    ("Germany", "de", "Germany", True),
    ("Ireland", "ie", "Ireland", True),
    ("Netherlands", "nl", "Netherlands", True),
    ("Canada", "ca", "Canada", True),
    ("Australia", "au", "Australia", True),
    ("UAE / Dubai", "ae", "Dubai, United Arab Emirates", True),
]

# Skills taken from your resume (Core Competencies + Experience sections)
SKILLS = [
    # architecture & design
    "solution architect", "enterprise architecture", "cloud transformation", "hld", "lld",
    "high-level design", "low-level design", "architecture review", "stakeholder",
    # landing zone / frameworks / governance
    "landing zone", "cloud adoption framework", "caf", "well-architected", "waf",
    "azure policy", "management group", "rbac", "resource lock", "governance",
    "defender for cloud", "compliance", "cost optimi",
    # compute & containers
    "aks", "kubernetes", "container", "app service", "azure vm",
    # networking
    "vnet", "virtual network", "vpn gateway", "load balancer", "expressroute",
    "virtual wan", "private link", "hybrid connectivity", "network segmentation", "hub and spoke",
    # migration & resiliency
    "azure migrate", "migration", "site recovery", "azure backup", "disaster recovery",
    "high availability", "business continuity", "iaas", "paas",
    # IaC & DevOps
    "terraform", "arm template", "bicep", "powershell", "azure devops", "ci/cd",
    "infrastructure as code", "iac", "automation",
    # certifications
    "az-305", "az-104", "az-700",
]

VISA_PATTERNS = [
    r"visa sponsor", r"sponsorship", r"sponsor (a |your )?visa", r"relocation",
    r"relocate", r"work permit", r"blue card", r"skilled worker", r"tier 2",
    r"critical skills", r"global talent", r"lmia", r"482", r"186", r"190",
    r"golden visa", r"visa provided", r"visa support", r"immigration support",
]
# Phrases that mean the opposite
NO_VISA_PATTERNS = [
    r"no (visa )?sponsorship", r"not (able|offer|provide)[^.]{0,30}sponsor",
    r"unable to sponsor", r"without sponsorship", r"must (already )?have the right to work",
    r"cannot sponsor", r"will not sponsor", r"do not sponsor", r"not eligible for sponsorship",
]

# Most postings never mention visa sponsorship. With SHOW_UNCONFIRMED=1 the email also lists
# strong skill matches abroad that do NOT mention sponsorship, in a clearly labelled section.
SHOW_UNCONFIRMED = os.getenv("SHOW_UNCONFIRMED", "0") == "1"
MAX_UNCONFIRMED_PER_COUNTRY = 5
MIN_SKILL_MATCHES = 2          # drop jobs that match fewer than this many skills
MAX_JOBS_PER_COUNTRY = 10
DATE_POSTED = "3days"          # today | 3days | week | month  (dedupe handles repeats)
SEEN_FILE = Path(os.getenv("SEEN_FILE", "seen_jobs.json"))
IST = timezone(timedelta(hours=5, minutes=30))

API_HOST = "jsearch.p.rapidapi.com"
OWN_URL = "https://api.openwebninja.com/jsearch/search"


# ----------------------------------------------------------------------------
def fetch_jobs(query: str, country: str, location: str, creds: dict) -> list[dict]:
    params = {
        "query": f"{query} in {location}",
        "country": country,
        "date_posted": DATE_POSTED,
        "num_pages": "1",
    }
    if creds.get("own"):                     # OpenWeb Ninja (JSearch's own portal)
        url = OWN_URL
        headers = {"x-api-key": creds["own"]}
    else:                                    # RapidAPI marketplace
        url = f"https://{API_HOST}/search"
        headers = {"X-RapidAPI-Key": creds["rapid"], "X-RapidAPI-Host": API_HOST}
    r = requests.get(url, headers=headers, params=params, timeout=40)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} from {url.split('/')[2]}: {r.text[:160]}")
    return r.json().get("data", []) or []


def analyse(job: dict, needs_visa: bool):
    """Returns a dict if the job passes, otherwise a short string saying why it was dropped."""
    text = " ".join(
        str(x or "") for x in (
            job.get("job_title"), job.get("job_description"),
            " ".join((job.get("job_highlights") or {}).get("Qualifications", []) or []),
            " ".join((job.get("job_highlights") or {}).get("Benefits", []) or []),
        )
    ).lower()

    title = (job.get("job_title") or "").lower()
    if "azure" not in text and "cloud" not in title:
        return "not_azure"
    if not any(k in title for k in ("architect", "principal", "lead", "consultant")):
        return "title"
    if any(k in title for k in ("junior", "intern", "graduate", "trainee", "entry level")):
        return "title"

    matched = sorted({s for s in SKILLS if re.search(rf"(?<![a-z]){re.escape(s)}", text)})
    if len(matched) < MIN_SKILL_MATCHES:
        return "skills"

    visa_signal, unconfirmed = "", False
    if needs_visa:
        if any(re.search(p, text) for p in NO_VISA_PATTERNS):
            return "visa_negative"
        hit = next((p for p in VISA_PATTERNS if re.search(p, text)), None)
        if hit:
            m = re.search(hit, text)
            visa_signal = text[max(0, m.start() - 60): m.end() + 80].replace("\n", " ").strip()
        elif SHOW_UNCONFIRMED:
            unconfirmed = True
        else:
            return "no_visa_mention"

    return {"matched": matched, "visa_signal": visa_signal, "unconfirmed": unconfirmed}


def todays_queries() -> list[str]:
    today = datetime.now(IST)
    wd = today.weekday()                      # Mon=0
    week = today.isocalendar()[1]
    if wd == 1:
        first = TUESDAY_QUERY
    elif wd == 3:
        first = ROTATING_QUERIES[week % len(ROTATING_QUERIES)]
    else:
        first = PRIMARY_QUERY
    extras = [PRIMARY_QUERY, TUESDAY_QUERY] + ROTATING_QUERIES
    out = [first] + [q for q in extras if q != first]
    return out[:QUERIES_PER_COUNTRY]


def job_id(job: dict) -> str:
    return job.get("job_id") or job.get("job_apply_link") or job.get("job_title", "")


def load_seen() -> dict:
    try:
        return json.loads(SEEN_FILE.read_text())
    except Exception:
        return {}


def save_seen(seen: dict) -> None:
    cutoff = (datetime.now(IST) - timedelta(days=60)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}   # keep 60 days
    SEEN_FILE.write_text(json.dumps(seen))


INDEED_DOMAINS = {"in": "in.indeed.com", "gb": "uk.indeed.com", "de": "de.indeed.com", "ie": "ie.indeed.com",
                  "nl": "nl.indeed.com", "ca": "ca.indeed.com", "au": "au.indeed.com", "ae": "ae.indeed.com"}


def job_board_links(label: str) -> str:
    """One-click searches on Google Jobs, LinkedIn, Indeed (and Naukri for India). No API quota used."""
    cc, loc, needs_visa = next((c, l, v) for lab, c, l, v in TARGETS if lab == label)
    extra = " visa sponsorship" if needs_visa else ""
    rows = []
    for title in (PRIMARY_QUERY, TUESDAY_QUERY):
        kw = f"{title}{extra}"
        links = [
            ("Google Jobs", f"https://www.google.com/search?q={quote_plus(kw + ' jobs in ' + loc)}&ibp=htl;jobs&hl=en"),
            ("LinkedIn", f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(kw)}&location={quote_plus(loc)}&f_TPR=r604800&sortBy=DD"),
            ("Indeed", f"https://{INDEED_DOMAINS[cc]}/jobs?q={quote_plus(kw)}&l={quote_plus(loc)}&fromage=7&sort=date"),
        ]
        if cc == "in":
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            links.append(("Naukri", f"https://www.naukri.com/{slug}-jobs-in-mumbai"))
        rows.append(f"<b>{title}:</b> " + " &middot; ".join(f"<a href='{u}'>{n}</a>" for n, u in links))
    return ("<p style='font-size:12px;color:#555;margin:2px 0 14px;line-height:1.7'>Search more: <br>"
            + "<br>".join(rows) + "</p>")


def apply_links(job: dict) -> tuple[str, str]:
    """Pick the best main link and build a line of alternative links.
    Some job boards block visitors from other countries ("not available in your country"),
    so we prefer the employer's own page and always offer alternatives."""
    options = job.get("apply_options") or []
    direct = [o for o in options if o.get("is_direct") and o.get("apply_link")]
    main = (direct[0]["apply_link"] if direct else None) or job.get("job_apply_link") or \
        (options[0]["apply_link"] if options else "")
    alts, used = [], {main}
    for o in options:
        link = o.get("apply_link")
        if link and link not in used:
            used.add(link)
            alts.append(f"<a href='{link}'>{o.get('publisher') or 'Other'}</a>")
        if len(alts) == 3:
            break
    if job.get("job_google_link"):
        alts.append(f"<a href='{job['job_google_link']}'>Google Jobs</a>")
    q = quote_plus(f"{job.get('job_title', '')} {job.get('employer_name', '')}")
    alts.append(f"<a href='https://www.google.com/search?q={q}'>Find on Google</a>")
    return main, " &middot; ".join(alts)


def build_email(results: dict, errors: list[str], stats: dict | None = None) -> tuple[str, str]:
    today = datetime.now(IST).strftime("%a %d %b %Y")
    total = sum(len(v) for v in results.values())
    subject = f"Azure Architect jobs - {total} new ({today})"

    parts = [
        "<div style='font-family:Segoe UI,Arial,sans-serif;max-width:760px'>",
        f"<h2>Azure Solution Architect jobs - {today}</h2>",
        f"<p>{total} new matching roles. Abroad roles mention visa sponsorship / relocation; ranked by resume-skill match.</p>",
    ]
    for label, jobs in results.items():
        parts.append(f"<h3 style='border-bottom:1px solid #ddd;padding-bottom:4px'>{label} ({len(jobs)})</h3>")
        if stats and label in stats:
            st = stats[label]
            parts.append(
                "<p style='color:#888;font-size:12px;margin:0 0 6px'>Search funnel: "
                f"fetched {st['fetched']} &rarr; Azure architect-type titles {st['title_ok']} "
                f"&rarr; enough resume skills {st['skills_ok']} &rarr; passed visa rule {st['visa_ok']} "
                f"(already sent before: {st['seen']})</p>")
        if not jobs:
            parts.append("<p style='color:#888'>No new matches today.</p>")
        for j in jobs:
            job, a = j["job"], j["analysis"]
            loc = ", ".join(x for x in (job.get("job_city"), job.get("job_state"), job.get("job_country")) if x)
            main_link, alt_links = apply_links(job)
            parts.append(
                "<div style='margin:0 0 14px'>"
                f"<a href='{main_link}' style='font-size:15px;font-weight:600'>{job.get('job_title')}</a><br>"
                f"<span>{job.get('employer_name')} &middot; {loc} &middot; via {job.get('job_publisher') or 'web'}</span><br>"
                f"<span style='color:#0a6'>Skills matched ({len(a['matched'])}): {', '.join(a['matched'][:10])}</span>"
            )
            if a.get("unconfirmed"):
                parts.append("<br><span style='color:#b60'><i>Sponsorship not mentioned in the posting - check with the recruiter.</i></span>")
            if a["visa_signal"]:
                parts.append(f"<br><span style='color:#555'><i>Visa/relocation note:</i> ...{a['visa_signal']}...</span>")
            parts.append(f"<br><span style='font-size:12px;color:#555'>Link blocked or \"not available in your country\"? Try: {alt_links}</span>")
            parts.append("</div>")
        parts.append(job_board_links(label))
    if errors:
        parts.append("<p style='color:#b00'>Issues: " + "; ".join(errors) + "</p>")
    parts.append("<p style='color:#888;font-size:12px'>Always verify the sponsorship wording on the original posting before applying.</p></div>")
    return subject, "".join(parts)


def send_email(subject: str, html: str) -> None:
    user, pw = os.environ["SMTP_USER"], os.environ["SMTP_PASS"]
    to = os.getenv("MAIL_TO") or user
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(re.sub(r"<[^>]+>", "", html), "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pw)
        s.sendmail(user, [to], msg.as_string())


def main() -> int:
    creds = {"own": os.getenv("OPENWEBNINJA_API_KEY"), "rapid": os.getenv("RAPIDAPI_KEY")}
    if not (creds["own"] or creds["rapid"]):
        sys.exit("Missing API key: set OPENWEBNINJA_API_KEY (or RAPIDAPI_KEY) as a GitHub secret.")
    seen, now = load_seen(), datetime.now(IST).isoformat()
    results, errors = {}, []

    stats = {}
    for label, cc, loc, needs_visa in TARGETS:
        collected, ids = [], set()
        st = {"fetched": 0, "title_ok": 0, "skills_ok": 0, "visa_ok": 0, "seen": 0}
        for q in todays_queries():
            try:
                jobs = fetch_jobs(q, cc, loc, creds)
            except Exception as e:
                errors.append(f"{label}: {e}")
                continue
            for job in jobs:
                jid = job_id(job)
                if jid in ids:
                    continue
                st["fetched"] += 1
                if jid in seen:
                    st["seen"] += 1
                    continue
                a = analyse(job, needs_visa)
                if isinstance(a, str):
                    if a in ("skills", "visa_negative", "no_visa_mention"):
                        st["title_ok"] += 1
                    if a in ("visa_negative", "no_visa_mention"):
                        st["skills_ok"] += 1
                    continue
                st["title_ok"] += 1
                st["skills_ok"] += 1
                if not a["unconfirmed"]:
                    st["visa_ok"] += 1
                ids.add(jid)
                collected.append({"job": job, "analysis": a})
        confirmed = [c for c in collected if not c["analysis"]["unconfirmed"]]
        unconf = [c for c in collected if c["analysis"]["unconfirmed"]][:MAX_UNCONFIRMED_PER_COUNTRY]
        key = lambda x: len(x["analysis"]["matched"])
        confirmed.sort(key=key, reverse=True)
        unconf.sort(key=key, reverse=True)
        results[label] = (confirmed + unconf)[:MAX_JOBS_PER_COUNTRY]
        stats[label] = st
        for c in results[label]:
            seen[job_id(c["job"])] = now

    subject, html = build_email(results, errors, stats)
    subject += f" | search: {', '.join(todays_queries())}"
    if os.getenv("DRY_RUN"):
        Path("preview.html").write_text(html)
        print("DRY_RUN: wrote preview.html -", subject)
    else:
        send_email(subject, html)
        print("Sent:", subject)
    save_seen(seen)
    if len(errors) == len(TARGETS):
        print("All searches failed:", errors[0])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
