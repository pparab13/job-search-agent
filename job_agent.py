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
import email as email_lib
import imaplib
import re
import smtplib
import ssl
from urllib.parse import quote_plus
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser
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
            "job_posted_at_datetime_utc": it.get("created"),
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
            "job_posted_text": (it.get("detected_extensions") or {}).get("posted_at"),
        })
    return jobs


# ---------------------------------------------------------------------------
# Infrastructure-only filter: drop roles that ask for coding / software-development experience.
# (PowerShell, Terraform, ARM, Bicep, Bash and YAML are infrastructure tooling and are NOT treated as coding.)
# Set ALLOW_CODING=1 to switch the filter off.
# ---------------------------------------------------------------------------
ALLOW_CODING = os.getenv("ALLOW_CODING", "0") == "1"
CODING_TITLE_WORDS = ("developer", "software engineer", "software architect", "application architect",
                      "full stack", "full-stack", "fullstack", "programmer", "sde")
CODING_PATTERNS = [re.compile(p) for p in (
    r"(?<![a-z0-9])\.net(?![a-z])", r"\bdotnet\b", r"\basp\.net\b", r"(?<![a-z])c#", r"\bc[- ]sharp\b", r"\bc\+\+",
    r"\bjava\b", r"\bjavascript\b", r"\btypescript\b", r"\bpython\b", r"\bnode\.?js\b", r"\bgolang\b",
    r"\bruby\b", r"\bphp\b", r"\bscala\b", r"\bkotlin\b", r"\brust\b", r"\bspring boot\b",
    r"\bdjango\b", r"\bflask\b", r"\breact(?:\.?js)?\b", r"\bangular\b", r"\bvue(?:\.?js)?\b",
    r"\bfull[- ]?stack\b", r"\bcoding\b", r"\bprogramming\b", r"\bsoftware development\b",
    r"\bsoftware engineering\b", r"\bapplication development\b", r"\bsoftware developer\b",
    r"\bdevelopment experience\b", r"\bwrite code\b",
)]
NO_CODING_OK = re.compile(r"\b(?:no|without|not require[d]?|not necessary)\s+(?:hands[- ]on\s+)?(?:coding|programming)\b|"
                          r"\b(?:coding|programming)\s+(?:is\s+)?not\s+required\b")


def requires_coding(title: str, text: str) -> bool:
    if any(w in title for w in CODING_TITLE_WORDS):
        return True
    text = NO_CODING_OK.sub(" ", text)
    return any(p.search(text) for p in CODING_PATTERNS)


# ---------------------------------------------------------------------------
# Expired / closed / unreachable listings
#  * MAX_AGE_DAYS: drop postings older than this (and anything whose own expiry date has passed)
#  * AVOID_PUBLISHERS: re-post aggregators that keep dead or country-blocked listings online; jobs that are
#    ONLY available through them are dropped, and they are never used as the main apply link.
#    Add more with the env var AVOID_PUBLISHERS="site1,site2"
#  * CHECK_LINKS: open each apply link before e-mailing and drop jobs whose page says the job is closed
# ---------------------------------------------------------------------------
MAX_AGE_DAYS = int(os.getenv("MAX_AGE_DAYS", "10"))
CHECK_LINKS = os.getenv("CHECK_LINKS", "1") == "1"
AVOID_PUBLISHERS = [x.strip().lower() for x in
                    ("jooble,jobleads,jobrapido,bebee,jobilize,drlogy," + os.getenv("AVOID_PUBLISHERS", "")).split(",") if x.strip()]
CLOSED_PHRASES = (
    "no longer available", "no longer accepting", "no longer open", "position has been filled", "job has expired",
    "this job has expired", "job is no longer", "has been closed", "vacancy has closed", "posting has expired",
    "this position is closed", "this job is closed", "no longer active", "expired job", "position is no longer",
    "role is no longer", "this job posting is no longer", "this vacancy is no longer", "job posting has been removed",
    "this position has been filled", "applications are closed", "page not found",
)
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")


def _parse_dt(v):
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def posted_dt(job: dict):
    dt = _parse_dt(job.get("job_posted_at_datetime_utc")) if job.get("job_posted_at_datetime_utc") else None
    if dt:
        return dt
    txt = (job.get("job_posted_text") or "").lower()
    now = datetime.now(timezone.utc)
    m = re.search(r"(\d+)\s*\+?\s*(hour|day|week|month)", txt)
    if m:
        days = int(m.group(1)) * {"hour": 0, "day": 1, "week": 7, "month": 30}[m.group(2)]
        return now - timedelta(days=days)
    return now if ("today" in txt or "just posted" in txt) else None


def is_stale(job: dict) -> bool:
    now = datetime.now(timezone.utc)
    exp = _parse_dt(job.get("job_offer_expiration_datetime_utc")) if job.get("job_offer_expiration_datetime_utc") else None
    if exp and exp < now:
        return True
    pd = posted_dt(job)
    return bool(pd and (now - pd).days > MAX_AGE_DAYS)


def avoided(name) -> bool:
    return any(a in (name or "").lower() for a in AVOID_PUBLISHERS)


def aggregator_only(job: dict) -> bool:
    pubs = [o.get("publisher") for o in (job.get("apply_options") or [])] or \
           ([job.get("job_publisher")] if job.get("job_publisher") else [])
    return bool(pubs) and all(avoided(p) for p in pubs)


def link_status(url: str) -> str:
    """'dead' only on clear signals (404/410 or a 'job closed' message). Bot-blocked pages (403/429/login walls) are 'unknown'."""
    if not url or "google." in url:
        return "unknown"
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en"}, timeout=12,
                         allow_redirects=True, stream=True)
        if r.status_code in (404, 410):
            return "dead"
        if r.status_code >= 400:
            return "unknown"
        body = r.raw.read(150000, decode_content=True).decode("utf-8", "ignore").lower()
        return "dead" if any(p in body for p in CLOSED_PHRASES) else "ok"
    except Exception:
        return "unknown"


def candidate_links(job: dict) -> list[str]:
    main, _ = apply_links(job)
    out = [main] if main else []
    for o in (job.get("apply_options") or [])[:4]:
        if o.get("apply_link") and o["apply_link"] not in out and not avoided(o.get("publisher")):
            out.append(o["apply_link"])
    return out[:3]


def drop_dead(items: list[dict], st: dict) -> list[dict]:
    if not CHECK_LINKS or not items:
        return items

    def verdict(c):
        states = [link_status(u) for u in candidate_links(c["job"])]
        return "dead" if states and all(x == "dead" for x in states) else "ok"

    with ThreadPoolExecutor(8) as ex:
        verdicts = list(ex.map(verdict, items))
    keep = []
    for c, v in zip(items, verdicts):
        if v == "dead":
            st["stale"] += 1
        else:
            keep.append(c)
    return keep


# ---------------------------------------------------------------------------
# Job-alert e-mails (LinkedIn / Naukri / Indeed) read from your own Gmail inbox over IMAP.
# Set up the alerts on those sites, enable IMAP in Gmail, and set READ_JOB_ALERTS=1.
# The e-mails only contain title, company, location and a link (no description), so these jobs are listed
# separately and are not skill-scored.
# ---------------------------------------------------------------------------
ALERT_DAYS = int(os.getenv("ALERT_DAYS", "3"))
ALERT_SOURCES = {          # sender domain -> (name, regex that finds the job id in a link)
    "linkedin.com": ("LinkedIn", re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?\"']*?-)?(\d{6,})")),
    "naukri.com": ("Naukri", re.compile(r"naukri\.com/(job-listings[^?\s\"'<>]+)")),
    "indeed.com": ("Indeed", re.compile(r"indeed\.com/[^\"'<>\s]*?(?:jk=|/job/)([0-9a-zA-Z]+)")),
}


class _AlertParser(HTMLParser):
    def __init__(self, rx):
        super().__init__()
        self.rx, self.items, self._seen, self._cur, self._href, self._txt = rx, [], set(), None, None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            m = self.rx.search(href)
            self._href, self._txt = ((href, m.group(1)) if m else None), []

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            title = " ".join(self._txt).strip()
            href, jid = self._href
            if len(title) > 3 and jid not in self._seen:
                self._seen.add(jid)
                self._cur = {"title": title, "href": href, "id": jid, "lines": []}
                self.items.append(self._cur)
            self._href = None

    def handle_data(self, data):
        t = " ".join(data.split())
        if not t:
            return
        if self._href is not None:
            self._txt.append(t)
        elif self._cur is not None and len(self._cur["lines"]) < 3:
            self._cur["lines"].append(t)


def parse_alert_html(html: str, source: str, rx) -> list[dict]:
    parser = _AlertParser(rx)
    parser.feed(html)
    jobs = []
    for it in parser.items:
        lines = [l for l in it["lines"] if l.lower() not in ("new", "easy apply", "actively recruiting", "view job")]
        company, loc = (lines[0] if lines else ""), (lines[1] if len(lines) > 1 else "")
        if " \u00b7 " in company and not loc:
            company, loc = [x.strip() for x in company.split(" \u00b7 ", 1)]
        link = it["href"]
        if source == "LinkedIn":
            link = f"https://www.linkedin.com/jobs/view/{it['id']}/"
        jobs.append({"job_id": f"alert-{source}-{it['id']}", "job_title": it["title"], "employer_name": company,
                     "job_city": loc, "job_publisher": source, "job_apply_link": link,
                     "apply_options": [{"publisher": source, "apply_link": link, "is_direct": False}], "from_alert": True})
    return jobs


def fetch_alert_jobs(user: str, pw: str) -> list[dict]:
    since = (datetime.now(IST) - timedelta(days=ALERT_DAYS)).strftime("%d-%b-%Y")
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(user, pw.replace(" ", ""))
    M.select("INBOX", readonly=True)            # read-only: your mail is never marked as read or changed
    jobs = []
    try:
        for domain, (source, rx) in ALERT_SOURCES.items():
            typ, data = M.search(None, "SINCE", since, "FROM", domain)
            for num in (data[0].split() if data and data[0] else [])[-25:]:
                typ, md = M.fetch(num, "(RFC822)")
                msg = email_lib.message_from_bytes(md[0][1])
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                        jobs += parse_alert_html(html, source, rx)
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return jobs


ALERT_NEGATIVE_TITLE = ("data ", "sap ", "salesforce", "servicenow", "workday", "erp", "crm", "machine learning", "network security")


def alert_job_ok(job: dict) -> bool:
    title = (job.get("job_title") or "").lower()
    if "architect" not in title or any(w in title for w in ALERT_NEGATIVE_TITLE):
        return False
    if alert_rank(job) == 0 and "solution" not in title:
        return False
    if any(w in title for w in CODING_TITLE_WORDS) or any(w in title for w in ("junior", "intern", "graduate", "trainee")):
        return False
    return True


def alert_rank(job: dict) -> int:
    t = (job.get("job_title") or "").lower()
    return sum(w in t for w in ("azure", "cloud", "infrastructure", "platform", "migration", "enterprise"))


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
    if is_stale(job):
        return "expired"
    if aggregator_only(job):
        return "low_quality_link"
    text = job_text(job)

    title = (job.get("job_title") or "").lower()
    if "azure" not in text and "cloud" not in title:
        return "not_azure"
    if not any(k in title for k in ("architect", "principal", "lead", "consultant")):
        return "title"
    if any(k in title for k in ("junior", "intern", "graduate", "trainee", "entry level")):
        return "title"
    if not ALLOW_CODING and requires_coding(title, text):
        return "coding"

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


def local_boards(cc: str, title: str, needs_visa: bool) -> list[tuple[str, str]]:
    """Country-specific job boards. Direct links use each site's own search address; 'site:' links open a Google search
    limited to that site, so they keep working even if the site changes its URLs."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    q = quote_plus(title)
    extra = " visa sponsorship" if needs_visa else ""
    site = lambda domain: f"https://www.google.com/search?q={quote_plus(title + extra + ' site:' + domain)}"
    boards = {
        "in": [("Foundit", f"https://www.foundit.in/srp/results?query={q}&locations=mumbai"),
               ("TimesJobs", f"https://www.timesjobs.com/candidate/job-search.html?searchType=personalizedSearch&from=submit&txtKeywords={q}&txtLocation=mumbai"),
               ("Shine", f"https://www.shine.com/job-search/{slug}-jobs-in-mumbai"),
               ("Instahyre", site("instahyre.com")), ("Cutshort", site("cutshort.io")), ("Hirist", site("hirist.tech")),
               ("Wellfound", site("wellfound.com")), ("Glassdoor", site("glassdoor.co.in"))],
        "gb": [("Reed", f"https://www.reed.co.uk/jobs/{slug}-jobs"), ("CWJobs", f"https://www.cwjobs.co.uk/jobs/{slug}"),
               ("Totaljobs", f"https://www.totaljobs.com/jobs/{slug}"), ("Glassdoor", site("glassdoor.co.uk"))],
        "de": [("StepStone", f"https://www.stepstone.de/jobs/{slug}"), ("Xing", site("xing.com/jobs")),
               ("Glassdoor", site("glassdoor.de"))],
        "ie": [("IrishJobs", f"https://www.irishjobs.ie/jobs/{slug}"), ("Jobs.ie", site("jobs.ie")),
               ("Glassdoor", site("glassdoor.ie"))],
        "nl": [("Nationale Vacaturebank", f"https://www.nationalevacaturebank.nl/vacature/zoeken?query={q}"),
               ("Glassdoor", site("glassdoor.nl"))],
        "ca": [("Job Bank (gov.)", f"https://www.jobbank.gc.ca/jobsearch/jobsearch?searchstring={q}&locationstring=Canada"),
               ("Glassdoor", site("glassdoor.ca"))],
        "au": [("Seek", f"https://www.seek.com.au/{slug}-jobs"), ("Jora", f"https://au.jora.com/j?q={q}&l=Australia"),
               ("Glassdoor", site("glassdoor.com.au"))],
        "ae": [("Bayt", f"https://www.bayt.com/en/uae/jobs/{slug}-jobs/"), ("GulfTalent", site("gulftalent.com")),
               ("Naukrigulf", f"https://www.naukrigulf.com/{slug}-jobs-in-uae")],
    }
    return boards.get(cc, [])


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
    local = local_boards(cc, PRIMARY_QUERY, needs_visa)
    if local:
        rows.append("<b>Local job boards:</b> " + " &middot; ".join(f"<a href='{u}'>{n}</a>" for n, u in local))
    return ("<p style='font-size:12px;color:#555;margin:2px 0 14px;line-height:1.7'>Search more: <br>"
            + "<br>".join(rows) + "</p>")


def apply_links(job: dict) -> tuple[str, str]:
    """Pick the best main link and build a line of alternative links.
    Preference: the employer's own page, then normal job boards. Re-post aggregators (AVOID_PUBLISHERS) are skipped
    because they often keep closed or country-blocked listings online."""
    options = [o for o in (job.get("apply_options") or []) if o.get("apply_link")]
    good = [o for o in options if not avoided(o.get("publisher"))]
    pool = good or options
    direct = [o for o in pool if o.get("is_direct")]
    main = (direct[0]["apply_link"] if direct else None) or (pool[0]["apply_link"] if pool else None) or job.get("job_apply_link") or ""
    alts, used = [], {main}
    for o in good:
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


def build_email(results: dict, errors: list[str], stats: dict | None = None,
                others: dict | None = None, alerts: list[dict] | None = None) -> tuple[str, str]:
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
    if alerts:
        parts.append("<h3 style='border-bottom:1px solid #ddd;padding-bottom:4px'>"
                     f"From your job-alert e-mails ({len(alerts)})</h3>"
                     "<p style='color:#777;font-size:12px;margin:0 0 6px'>LinkedIn / Naukri / Indeed alert e-mails contain no job description, "
                     "so these are not scored. Open the link to check the details and the closing date.</p>")
        for j in alerts:
            parts.append("<div style='margin:0 0 10px'>"
                         f"<a href='{j['job_apply_link']}' style='font-size:15px;font-weight:600'>{j['job_title']}</a> "
                         f"<span style='background:#0a66c2;color:#fff;padding:1px 7px;border-radius:10px;font-size:11px'>{j['job_publisher']}</span><br>"
                         f"<span>{j.get('employer_name') or ''}{' &middot; ' + j['job_city'] if j.get('job_city') else ''}</span></div>")
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
                f"(dropped as expired / closed / aggregator-only: {st['stale']}; coding/developer requirements: {st['coding']}; already sent before: {st['seen']})</p>")
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


def smtp_send(msg, user: str, pw: str, to: str) -> None:
    """Send via Gmail. Checks the secrets first, strips spaces from the App Password, and if Gmail drops the
    connection on port 465 retries on port 587 (STARTTLS) before giving up with a plain-English error."""
    user, pw = (user or "").strip(), (pw or "").replace(" ", "").strip()
    if not user or not pw:
        raise RuntimeError("SMTP_USER or SMTP_PASS is EMPTY in this run. Re-add both as repository secrets "
                           "(Settings > Secrets and variables > Actions > Secrets) and check the names match exactly.")
    rcpts = [to] if isinstance(to, str) else list(to)      # `to` may be one address or a list (To + Cc)
    last = None
    for attempt in range(2):
        for mode in ("ssl465", "starttls587"):
            try:
                if mode == "ssl465":
                    s = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60)
                else:
                    s = smtplib.SMTP("smtp.gmail.com", 587, timeout=60)
                    s.starttls(context=ssl.create_default_context())
                with s:
                    s.login(user, pw)
                    s.sendmail(user, rcpts, msg.as_string())
                return
            except smtplib.SMTPAuthenticationError as e:
                raise RuntimeError("Gmail rejected the login (wrong or revoked App Password, or 2-Step Verification is off). "
                                   "Create a NEW App Password and update the SMTP_PASS secret.") from e
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError) as e:
                last = e
        time.sleep(8)
    raise RuntimeError(f"Could not connect to Gmail after retries: {last}. If this keeps happening, SMTP_USER/SMTP_PASS "
                       "may be blank or the App Password was revoked - create a new one and update the secret.")


def send_email(subject: str, html: str) -> None:
    user, pw = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", "")
    to = os.getenv("MAIL_TO") or user
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(re.sub(r"<[^>]+>", "", html), "plain"))
    msg.attach(MIMEText(html, "html"))
    smtp_send(msg, user, pw, to)


def main() -> int:
    creds = {"own": os.getenv("OPENWEBNINJA_API_KEY"), "rapid": os.getenv("RAPIDAPI_KEY")}
    serp_key = os.getenv("SERPAPI_KEY")
    if not (creds["own"] or creds["rapid"] or serp_key or os.getenv("ADZUNA_APP_ID")):
        sys.exit("Missing API key: set OPENWEBNINJA_API_KEY (or RAPIDAPI_KEY, SERPAPI_KEY or ADZUNA_APP_ID/ADZUNA_APP_KEY) as a GitHub secret.")
    # IGNORE_SEEN=true (manual "Run workflow" checkbox) re-sends jobs that were already emailed
    seen = {} if os.getenv("IGNORE_SEEN", "").lower() == "true" else load_seen()
    now = datetime.now(IST).isoformat()
    today = datetime.now(IST).strftime("%Y-%m-%d")
    # Backup schedule: GitHub sometimes delays or drops a scheduled run. A second scheduled run later in the
    # morning sends the digest only if today's first run did not already send it.
    if os.getenv("SKIP_IF_SENT_TODAY") == "1" and seen.get("__digest_sent__") == today:
        print("Today's digest was already sent - skipping this backup run.")
        return 0
    results, others, stats, errors = {}, {}, {}, []
    failed_countries = 0

    for label, cc, loc, needs_visa in TARGETS:
        collected, extra, ids = [], [], set()
        st = {"fetched": 0, "title_ok": 0, "skills_ok": 0, "visa_ok": 0, "seen": 0, "coding": 0, "stale": 0}
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
                    if a == "coding":
                        st["coding"] += 1
                    if a in ("expired", "low_quality_link"):
                        st["stale"] += 1
                    if a in ("skills", "visa_negative", "no_visa_mention", "coding"):
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
        collected, extra = drop_dead(collected, st), drop_dead(extra, st)     # remove closed / expired listings
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

    # hand the scored jobs to apply_agent.py (tailored resume + cover letter for 80+ matches)
    cc_of = {lab: c for lab, c, _, _ in TARGETS}
    dump = [{"label": lab, "cc": cc_of[lab], "job": c["job"], "analysis": c["analysis"]}
            for lab in results for c in results[lab] + others[lab]]
    Path(os.getenv("SCORED_JOBS", "scored_jobs.json")).write_text(json.dumps(dump, default=str))

    alerts = []
    if os.getenv("READ_JOB_ALERTS") == "1" and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS"):
        try:
            seen_titles = set()
            for j in fetch_alert_jobs(os.environ["SMTP_USER"].strip(), os.environ["SMTP_PASS"]):
                jid, dk = job_id(j), dedupe_key(j)
                if jid in seen or dk in seen or dk in seen_titles or not alert_job_ok(j):
                    continue
                seen_titles.add(dk)
                alerts.append(j)
            alerts.sort(key=alert_rank, reverse=True)
            alerts = alerts[:30]
            for j in alerts:
                seen[job_id(j)] = now
                seen[dedupe_key(j)] = now
        except Exception as e:
            errors.append("Gmail job alerts: " + str(e)[:150] +
                          " (check that IMAP is enabled in Gmail settings > Forwarding and POP/IMAP, and that SMTP_PASS is an App Password)")

    subject, html = build_email(results, errors, stats, others, alerts)
    subject += f" | search: {', '.join(todays_queries())}"
    if os.getenv("DRY_RUN"):
        Path("preview.html").write_text(html)
        print("DRY_RUN: wrote preview.html -", subject)
    else:
        send_email(subject, html)
        print("Sent:", subject)
        seen["__digest_sent__"] = today
    save_seen(seen)
    if failed_countries == len(TARGETS):
        print("All searches failed:", errors[0] if errors else "unknown")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
