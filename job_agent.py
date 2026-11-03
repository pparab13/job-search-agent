"""
Azure Solution Architect daily job-alert agent.

Searches Google-for-Jobs-style aggregated listings (via the JSearch API on RapidAPI)
for each target country, keeps only roles that mention visa sponsorship / relocation
support (except India), ranks them by how many of your resume skills they mention,
removes jobs already emailed before, and emails you an HTML digest.

Env vars (set as GitHub Actions secrets or in a local .env export):
  OPENWEBNINJA_API_KEY - JSearch key from app.openwebninja.com (recommended, used if set)
  RAPIDAPI_KEY         - OR a RapidAPI key subscribed to "JSearch" (fallback)
  SERPAPI_KEY          - OPTIONAL: SerpApi key; adds the exact Google Jobs results as a second source
  ADZUNA_APP_ID / ADZUNA_APP_KEY - OPTIONAL: free Adzuna API keys; adds Adzuna listings (UK, DE, NL, CA, AU, India)
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
import time
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
BEST_SCORE = int(os.getenv("BEST_SCORE", "88"))   # match score at or above this = "best match"
OTHERS_PER_COUNTRY = 8          # looser matches shown in a separate 'more roles' section
DATE_POSTED = "3days"          # today | 3days | week | month  (dedupe handles repeats)
SEEN_FILE = Path(os.getenv("SEEN_FILE", "seen_jobs.json"))
IST = timezone(timedelta(hours=5, minutes=30))

API_HOST = "jsearch.p.rapidapi.com"
OWN_URL = "https://api.openwebninja.com/jsearch/search"


# ----------------------------------------------------------------------------
def http_get(url: str, retries: int = 2, timeout: int = 50, **kw) -> requests.Response:
    """GET with retries: slow APIs (timeouts) and temporary 429/5xx errors are retried with a short pause."""
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout, **kw)
            if r.status_code in (429, 500, 502, 503, 504) and i < retries:
                time.sleep(4 * (i + 1))
                continue
            return r
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
            if i < retries:
                time.sleep(4 * (i + 1))
    raise last


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
    r = http_get(url, headers=headers, params=params)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} from {url.split('/')[2]}: {r.text[:160]}")
    return r.json().get("data", []) or []


ADZUNA_COUNTRIES = {"in", "gb", "de", "nl", "ca", "au"}     # Adzuna has no Ireland / UAE site


def fetch_adzuna(query: str, country: str, location: str, app_id: str, app_key: str) -> list[dict]:
    """Adzuna listings converted to the same shape as JSearch jobs."""
    params = {"app_id": app_id, "app_key": app_key, "what": query, "results_per_page": 20,
              "max_days_old": 3, "sort_by": "date", "content-type": "application/json"}
    if country == "in":
        params["where"] = "Mumbai"
    r = http_get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", params=params)
    if not r.ok:
        raise RuntimeError(f"Adzuna HTTP {r.status_code}: {r.text[:120]}")
    clean = lambda t: re.sub(r"<[^>]+>", "", t or "")
    jobs = []
    for it in r.json().get("results", []) or []:
        link = it.get("redirect_url")
        jobs.append({
            "job_id": f"adzuna-{it.get('id')}",
            "job_title": clean(it.get("title")),
            "employer_name": (it.get("company") or {}).get("display_name"),
            "job_city": (it.get("location") or {}).get("display_name"),
            "job_description": clean(it.get("description")),
            "job_publisher": "Adzuna",
            "job_apply_link": link,
            "apply_options": [{"publisher": "Adzuna", "apply_link": link, "is_direct": False}],
        })
    return jobs


def fetch_serpapi(query: str, country: str, location: str, key: str) -> list[dict]:
    """Google Jobs results via SerpApi, converted to the same shape as JSearch jobs."""
    params = {"engine": "google_jobs", "q": f"{query} jobs in {location}",
              "gl": country, "hl": "en", "api_key": key}
    for _ in range(2):
        r = http_get("https://serpapi.com/search.json", params=params, timeout=60)
        err = (r.json() if r.content else {}).get("error", "") if r.headers.get("content-type", "").startswith("application/json") else ""
        if "gl parameter" in err and "gl" in params:
            params.pop("gl")          # SerpApi rejects some country codes (e.g. ie, au); the location in q still applies
            continue
        break
    data = r.json() if r.content else {}
    if data.get("error") and not data.get("jobs_results"):
        if "hasn't returned any results" in data["error"]:
            return []
        raise RuntimeError(f"SerpApi: {data['error'][:160]}")
    if not r.ok:
        raise RuntimeError(f"SerpApi HTTP {r.status_code}")
    jobs = []
    for it in data.get("jobs_results", []) or []:
        hl = {b.get("title", ""): b.get("items", []) for b in (it.get("job_highlights") or [])}
        opts = []
        for o in it.get("apply_options") or []:
            t = (o.get("title") or "")
            opts.append({"publisher": re.sub(r"^Apply (directly )?on ", "", t),
                         "apply_link": o.get("link"), "is_direct": "directly" in t.lower()})
        jobs.append({
            "job_id": it.get("job_id"),
            "job_title": it.get("title"),
            "employer_name": it.get("company_name"),
            "job_city": it.get("location"),
            "job_description": it.get("description"),
            "job_highlights": {"Qualifications": hl.get("Qualifications", []), "Benefits": hl.get("Benefits", [])},
            "job_publisher": (it.get("via") or "").replace("via ", ""),
            "job_apply_link": (opts[0]["apply_link"] if opts else None) or it.get("share_link"),
            "apply_options": opts,
            "job_google_link": it.get("share_link"),
        })
    return jobs


def job_text(job: dict) -> str:
    return " ".join(
        str(x or "") for x in (
            job.get("job_title"), job.get("job_description"),
            " ".join((job.get("job_highlights") or {}).get("Qualifications", []) or []),
            " ".join((job.get("job_highlights") or {}).get("Benefits", []) or []),
        )
    ).lower()


def match_skills(text: str) -> list[str]:
    return sorted({s for s in SKILLS if re.search(rf"(?<![a-z]){re.escape(s)}", text)})


def visa_snippet(text: str) -> str:
    hit = next((p for p in VISA_PATTERNS if re.search(p, text)), None)
    if not hit:
        return ""
    m = re.search(hit, text)
    return text[max(0, m.start() - 60): m.end() + 80].replace("\n", " ").strip()


def analyse(job: dict, needs_visa: bool):
    """Returns a dict if the job passes, otherwise a short string saying why it was dropped."""
    text = job_text(job)

    title = (job.get("job_title") or "").lower()
    if "azure" not in text and "cloud" not in title:
        return "not_azure"
    if not any(k in title for k in ("architect", "principal", "lead", "consultant")):
        return "title"
    if any(k in title for k in ("junior", "intern", "graduate", "trainee", "entry level")):
        return "title"

    matched = match_skills(text)
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


# Skills that matter most for you count double in the match score
CORE_SKILLS = {
    "landing zone", "cloud adoption framework", "well-architected", "azure policy", "management group",
    "rbac", "terraform", "aks", "expressroute", "virtual wan", "private link", "azure migrate",
    "site recovery", "azure devops", "powershell", "arm template", "vpn gateway", "hub and spoke",
    "hld", "lld", "defender for cloud",
}


def score_job(job: dict, analysis: dict, needs_visa: bool) -> dict:
    """Match score out of 100: resume skills 55 + title fit 20 + visa/relocation 15 + experience fit 10."""
    title = (job.get("job_title") or "").lower()
    text = job_text(job)

    weighted = sum(2 if m in CORE_SKILLS else 1 for m in analysis["matched"])
    skills = min(55, round(55 * weighted / 14))

    if "azure" in title and ("solution architect" in title or "solutions architect" in title):
        t = 20
    elif "azure" in title and "architect" in title:
        t = 17
    elif "cloud" in title and "architect" in title:
        t = 14
    elif "architect" in title:
        t = 10
    else:
        t = 6

    if not needs_visa or analysis.get("visa_signal"):
        v = 15            # India needs no sponsorship; abroad needs sponsorship/relocation wording
    else:
        v = 0

    yrs = [int(x) for x in re.findall(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years|yrs)", text)]
    if not yrs:
        e = 7
    elif 5 <= yrs[0] <= 15:
        e = 10
    elif yrs[0] < 5:
        e = 5
    else:
        e = 3

    return {"score": skills + t + v + e,
            "breakdown": f"Skills {skills}/55 &middot; Title {t}/20 &middot; Visa/relocation {v}/15 &middot; Experience {e}/10"}


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


def dedupe_key(job: dict) -> str:
    return re.sub(r"\W+", "", f"{job.get('job_title')}|{job.get('employer_name')}".lower())


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


def badge(score: int) -> str:
    color = "#0a7d32" if score >= BEST_SCORE else "#c77700" if score >= 70 else "#777"
    label = f"{score}% match" + (" &middot; BEST MATCH" if score >= BEST_SCORE else "")
    return (f"<span style='background:{color};color:#fff;padding:1px 8px;border-radius:10px;"
            f"font-size:12px;font-weight:600;white-space:nowrap'>{label}</span>")


def render_job(j: dict, show_country: bool = False) -> str:
    job, a = j["job"], j["analysis"]
    loc = ", ".join(x for x in (job.get("job_city"), job.get("job_state"), job.get("job_country")) if x)
    main_link, alt_links = apply_links(job)
    h = (
        "<div style='margin:0 0 14px'>"
        f"<a href='{main_link}' style='font-size:15px;font-weight:600'>{job.get('job_title')}</a> {badge(a['score'])}<br>"
        f"<span>{('<b>' + j['label'] + '</b> &middot; ') if show_country and j.get('label') else ''}{job.get('employer_name')} &middot; {loc} &middot; via {job.get('job_publisher') or 'web'}</span><br>"
        f"<span style='color:#0a6'>Skills matched ({len(a['matched'])}): {', '.join(a['matched'][:10]) or 'none found in the text'}</span>"
    )
    h += f"<br><span style='font-size:11px;color:#999'>Score: {a['breakdown']}</span>"
    if a.get("unconfirmed"):
        h += "<br><span style='color:#b60'><i>Sponsorship not mentioned in the posting - check with the recruiter.</i></span>"
    if a["visa_signal"]:
        h += f"<br><span style='color:#555'><i>Visa/relocation note:</i> ...{a['visa_signal']}...</span>"
    h += f"<br><span style='font-size:12px;color:#555'>Link blocked or \"not available in your country\"? Try: {alt_links}</span></div>"
    return h


def build_email(results: dict, errors: list[str], stats: dict | None = None,
                others: dict | None = None) -> tuple[str, str]:
    today = datetime.now(IST).strftime("%a %d %b %Y")
    others = others or {}
    best = sorted(
        (j for d in (results, others) for lst in d.values() for j in lst if j["analysis"]["score"] >= BEST_SCORE),
        key=lambda j: j["analysis"]["score"], reverse=True)
    best_ids = {id(j) for j in best}
    results = {k: [j for j in v if id(j) not in best_ids] for k, v in results.items()}
    others = {k: [j for j in v if id(j) not in best_ids] for k, v in others.items()}
    total = len(best) + sum(len(v) for v in results.values())
    total_other = sum(len(v) for v in others.values())
    subject = (f"Azure Architect jobs - {len(best)} best match{'es' if len(best) != 1 else ''} ({BEST_SCORE}+), "
               f"{total} new ({today})")

    parts = [
        "<div style='font-family:Segoe UI,Arial,sans-serif;max-width:760px'>",
        f"<h2>Azure Solution Architect jobs - {today}</h2>",
        f"<p><b>{len(best)}</b> best matches (score {BEST_SCORE}+), {total - len(best)} other good matches and "
        f"{total_other} more Azure architect roles.</p>",
        "<p style='color:#777;font-size:12px'>Match score out of 100 = resume skills 55 + title fit 20 + "
        "visa/relocation 15 + experience fit 10. Abroad roles only reach "
        f"{BEST_SCORE}+ if the posting mentions sponsorship or relocation.</p>",
    ]
    if best:
        parts.append("<h3 style='border-bottom:2px solid #0a7d32;padding-bottom:4px;color:#0a7d32'>"
                     f"Best matches ({BEST_SCORE}+)</h3>")
        parts.extend(render_job(j, True) for j in best)
    for label, jobs in results.items():
        extra = (others or {}).get(label, [])
        bc = sum(1 for j in best if j.get("label") == label)
        parts.append(f"<h3 style='border-bottom:1px solid #ddd;padding-bottom:4px'>{label} "
                     f"({bc} in Best matches above, {len(jobs)} other good, {len(extra)} more)</h3>")
        if stats and label in stats:
            st = stats[label]
            parts.append(
                "<p style='color:#888;font-size:12px;margin:0 0 6px'>Search funnel: "
                f"fetched {st['fetched']} &rarr; Azure architect-type titles {st['title_ok']} "
                f"&rarr; enough resume skills {st['skills_ok']} &rarr; passed visa rule {st['visa_ok']} "
                f"(already sent before: {st['seen']})</p>")
        if not jobs:
            parts.append("<p style='color:#888'>" + ("This country's top jobs are listed under Best matches above." if bc else "No new good matches today.") + "</p>")
        parts.extend(render_job(j) for j in jobs)
        if extra:
            parts.append("<p style='margin:10px 0 6px;font-weight:600;color:#333'>More Azure architect roles from Google Jobs "
                         "<span style='font-weight:400;color:#777'>(fewer resume skills in the text, or sponsorship not mentioned)</span></p>")
            parts.extend(render_job(j) for j in extra)
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
    serp_key = os.getenv("SERPAPI_KEY")
    if not (creds["own"] or creds["rapid"] or serp_key or os.getenv("ADZUNA_APP_ID")):
        sys.exit("Missing API key: set OPENWEBNINJA_API_KEY (or RAPIDAPI_KEY, SERPAPI_KEY or ADZUNA_APP_ID/ADZUNA_APP_KEY) as a GitHub secret.")
    # IGNORE_SEEN=true (manual "Run workflow" checkbox) re-sends jobs that were already emailed
    seen = {} if os.getenv("IGNORE_SEEN", "").lower() == "true" else load_seen()
    now = datetime.now(IST).isoformat()
    results, others, stats, errors = {}, {}, {}, []
    failed_countries = 0

    for label, cc, loc, needs_visa in TARGETS:
        collected, extra, ids = [], [], set()
        st = {"fetched": 0, "title_ok": 0, "skills_ok": 0, "visa_ok": 0, "seen": 0}
        sources_ok = 0
        for q in todays_queries():
            fetchers = []
            if creds["own"] or creds["rapid"]:
                fetchers.append(("JSearch", lambda: fetch_jobs(q, cc, loc, creds)))
            if serp_key:
                fetchers.append(("Google Jobs (SerpApi)", lambda: fetch_serpapi(q, cc, loc, serp_key)))
            if os.getenv("ADZUNA_APP_ID") and os.getenv("ADZUNA_APP_KEY") and cc in ADZUNA_COUNTRIES:
                fetchers.append(("Adzuna", lambda: fetch_adzuna(
                    q, cc, loc, os.environ["ADZUNA_APP_ID"], os.environ["ADZUNA_APP_KEY"])))
            jobs = []
            for name, fn in fetchers:
                try:
                    jobs += fn()
                    sources_ok += 1
                except Exception as e:
                    errors.append(f"{label} [{name}]: {e}")
            for job in jobs:
                jid, dk = job_id(job), dedupe_key(job)
                if jid in ids or dk in ids:
                    continue
                ids.update((jid, dk))
                st["fetched"] += 1
                if jid in seen or dk in seen:
                    st["seen"] += 1
                    continue
                a = analyse(job, needs_visa)
                if isinstance(a, dict):
                    a.update(score_job(job, a, needs_visa))
                if isinstance(a, str):
                    if a in ("skills", "visa_negative", "no_visa_mention"):
                        st["title_ok"] += 1
                    if a in ("visa_negative", "no_visa_mention"):
                        st["skills_ok"] += 1
                    if a in ("skills", "no_visa_mention"):      # architect role, just a looser match
                        text = job_text(job)
                        snippet = visa_snippet(text) if needs_visa else ""
                        if needs_visa and any(re.search(p, text) for p in NO_VISA_PATTERNS):
                            continue
                        ea = {"matched": match_skills(text), "visa_signal": snippet,
                              "unconfirmed": needs_visa and not snippet}
                        ea.update(score_job(job, ea, needs_visa))
                        extra.append({"job": job, "analysis": ea})
                    continue
                st["title_ok"] += 1
                st["skills_ok"] += 1
                if not a["unconfirmed"]:
                    st["visa_ok"] += 1
                collected.append({"job": job, "analysis": a})
        if sources_ok == 0:
            failed_countries += 1
        confirmed = [c for c in collected if not c["analysis"]["unconfirmed"]]
        unconf = [c for c in collected if c["analysis"]["unconfirmed"]][:MAX_UNCONFIRMED_PER_COUNTRY]
        key = lambda x: x["analysis"]["score"]
        confirmed.sort(key=key, reverse=True)
        unconf.sort(key=key, reverse=True)
        extra.sort(key=key, reverse=True)
        results[label] = (confirmed + unconf)[:MAX_JOBS_PER_COUNTRY]
        others[label] = extra[:OTHERS_PER_COUNTRY]
        stats[label] = st
        for c in results[label] + others[label]:
            c["label"] = label
            seen[job_id(c["job"])] = now
            seen[dedupe_key(c["job"])] = now

    subject, html = build_email(results, errors, stats, others)
    subject += f" | search: {', '.join(todays_queries())}"
    if os.getenv("DRY_RUN"):
        Path("preview.html").write_text(html)
        print("DRY_RUN: wrote preview.html -", subject)
    else:
        send_email(subject, html)
        print("Sent:", subject)
    save_seen(seen)
    if failed_countries == len(TARGETS):
        print("All searches failed:", errors[0] if errors else "unknown")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
