"""
Auto-apply agent + application report.

For every NEW job that scores >= APPLY_MIN_SCORE (default 80):
  1. Builds a resume/CV and cover letter tailored to the job and to the country's standards (apply_agent.py).
  2. If the posting contains an application e-mail address and every safety check passes, it SENDS the
     application from your Gmail (Cc to you) with the resume and cover letter attached.
  3. If the job only has an online form / portal (LinkedIn, Indeed, Naukri, company career sites), it does NOT
     try to drive the website - those sites forbid bots and use CAPTCHAs. The kit is attached to your report with
     the apply link so you can finish in about 2 minutes.
  4. E-mails you ONE report: applied / ready for you to apply / held back (and why), plus a running tracker (CSV).

Safety rules (all must pass before anything is sent):
  * AUTO_APPLY must be "true" (default false = preview: nothing is sent to employers)
  * score >= APPLY_MIN_SCORE; sponsorship is mentioned (or the job is in India)
  * the job description is complete (300+ characters)
  * at most MAX_GAPS_AUTO requirements that your resume does not show
  * never the same job twice, never the same recipient twice in 14 days, never a blocklisted company
  * at most MAX_AUTO_APPLY_PER_RUN applications per run
Every claim in the documents is checked against master_resume.json (see apply_agent.py).

Env: ANTHROPIC_API_KEY, SMTP_USER, SMTP_PASS, MAIL_TO, AUTO_APPLY, MAX_AUTO_APPLY_PER_RUN (3), MAX_GAPS_AUTO (2),
     BLOCKLIST_COMPANIES (ltimindtree,ltim,mindtree), APPLY_MIN_SCORE (80), MAX_KITS_PER_RUN (5), MAKE_PDF (1), DRY_RUN
"""
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import apply_agent as A
import job_agent as ja

DRY = bool(os.getenv("DRY_RUN"))
AUTO_APPLY = os.getenv("AUTO_APPLY", "false").lower() == "true" and not DRY
MAX_AUTO = int(os.getenv("MAX_AUTO_APPLY_PER_RUN", "3"))
MAX_GAPS = int(os.getenv("MAX_GAPS_AUTO", "2"))
BLOCK = [b.strip().lower() for b in os.getenv("BLOCKLIST_COMPANIES", "ltimindtree,ltim,mindtree").split(",") if b.strip()]
LOG = Path(os.getenv("APPLIED_LOG", "applied_log.json"))
CSV_FILE = Path(os.getenv("APPLIED_CSV", "applications_tracker.csv"))
IST = timezone(timedelta(hours=5, minutes=30))

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
APPLY_WORDS = re.compile(r"\b(apply|send|email|e-mail|mail|submit|forward|cv|resume|application|applications|interested)\b", re.I)
BAD_ADDR = re.compile(r"(no-?reply|do-?not-?reply|donotreply|unsubscribe|privacy|support@|abuse|postmaster|webmaster|example\.)", re.I)


# ----------------------------------------------------------------------------
def load_log() -> dict:
    try:
        return json.loads(LOG.read_text())
    except Exception:
        LOG.write_text("{}")
        return {}


def save_log(log: dict) -> None:
    LOG.write_text(json.dumps(log, indent=1))
    with CSV_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "country", "company", "job_title", "score", "method", "sent_to", "link"])
        for v in sorted(log.values(), key=lambda x: x["date"]):
            w.writerow([v["date"], v["country"], v["company"], v["title"], v["score"], v["method"], v.get("to", ""), v.get("link", "")])


def find_apply_email(job: dict, own_email: str) -> str | None:
    link = (job.get("job_apply_link") or "")
    if link.lower().startswith("mailto:"):
        return link[7:].split("?")[0] or None
    text = job.get("job_description") or ""
    for m in EMAIL_RE.finditer(text):
        addr = m.group(0).strip(".")
        if BAD_ADDR.search(addr) or addr.lower() == own_email.lower() or addr.lower().endswith((".png", ".jpg", ".gif")):
            continue
        if APPLY_WORDS.search(text[max(0, m.start() - 150): m.end() + 150]):
            return addr
    return None


def to_pdf(path: Path) -> Path:
    """Employers prefer PDF. Uses LibreOffice if present; otherwise keeps the .docx."""
    if os.getenv("MAKE_PDF", "1") != "1" or not shutil.which("soffice"):
        return path
    try:
        subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(path.parent), str(path)],
                       check=True, capture_output=True, timeout=150)
        pdf = path.with_suffix(".pdf")
        return pdf if pdf.exists() else path
    except Exception:
        return path


def letter_text(master: dict, job: dict, t: dict, rules: dict) -> str:
    p = master["personal"]
    parts = ["Dear Hiring Manager,", ""] + [x + "\n" for x in t["cover_letter"]]
    if A.NEEDS_SPONSORSHIP and rules["visa_note"]:
        extra = rules["visa_note"]
        if p.get("notice_period"):
            extra += f" My notice period is {p['notice_period']}."
        parts.append(extra + "\n")
    parts += ["I have attached my CV and cover letter for your review.", "", "Kind regards,", p["name"],
              p.get("phone", ""), p.get("email", "")]
    return "\n".join(parts)


def send_application(master: dict, job: dict, t: dict, rules: dict, to: str, files: list[Path]) -> None:
    user, pw = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", "")
    me = os.getenv("MAIL_TO") or user
    p = master["personal"]
    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"Application for {job.get('job_title')} - {p['name']}"
    msg["From"], msg["To"], msg["Cc"] = f"{p['name']} <{user}>", to, me
    if p.get("email"):
        msg["Reply-To"] = p["email"]
    msg.attach(MIMEText(letter_text(master, job, t, rules), "plain", "utf-8"))
    for f in files:
        part = MIMEApplication(f.read_bytes(), Name=f.name)
        part["Content-Disposition"] = f'attachment; filename="{f.name}"'
        msg.attach(part)
    ja.smtp_send(msg, user, pw, [to, me])


# ----------------------------------------------------------------------------
def report_html(rows: list[dict], total_log: int, auto_on: bool) -> str:
    today = datetime.now(IST).strftime("%a %d %b %Y")
    by = lambda st: [r for r in rows if r["status"] == st]
    applied, preview, manual, held, skipped, failed, lowfit = (by(s) for s in ("applied", "preview", "manual", "held", "skipped", "failed", "lowfit"))
    h = ["<div style='font-family:Segoe UI,Arial,sans-serif;max-width:780px'>", f"<h2>Application report - {today}</h2>"]
    mode = "AUTO-APPLY is ON" if auto_on else "PREVIEW mode: nothing was sent to employers (set the AUTO_APPLY variable to true to start applying)"
    h.append(f"<p style='color:#555'>{mode}. Jobs scoring {A.MIN_SCORE}+ today: {len(rows)}. "
             f"Applications sent so far (all time): {total_log}.</p>")
    h.append(f"<p><b>{len(applied)}</b> applied by e-mail &middot; <b>{len(preview)}</b> would apply (preview) &middot; "
             f"<b>{len(manual)}</b> ready for you to apply &middot; <b>{len(held)}</b> held back &middot; {len(lowfit)} below {A.MIN_SCORE}% after the fit check &middot; {len(skipped)} skipped</p>")

    def card(r, extra=""):
        job, a = r["job"], r["analysis"]
        main_link, alt = ja.apply_links(job)
        score = r.get("final", a["score"])
        s = ["<div style='border:1px solid #ddd;border-radius:8px;padding:10px 14px;margin:10px 0'>",
             f"<a href='{main_link}' style='font-size:15px;font-weight:600'>{job.get('job_title')}</a> {ja.badge(score)}<br>"
             f"<span>{job.get('employer_name')} &middot; {r['label']} &middot; {r['rules']['doc'] if r.get('rules') else ''}</span>"]
        if "final" in r:
            s.append(f"<br><span style='color:#555;font-size:12px'>Fit check: keyword score {a['score']}% &middot; resume covers {r['coverage']}% "
                     f"of the essential requirements &rarr; final {r['final']}%</span>")
        if extra:
            s.append(f"<br>{extra}")
        if r.get("reason"):
            s.append(f"<br><span style='color:#b60'>{r['reason']}</span>")
        if r.get("gaps"):
            shown = r["gaps"][:3] if r["status"] == "lowfit" else r["gaps"]
            s.append("<br><span style='color:#b00'>Not claimed (your resume doesn't show): " + "; ".join(shown) + "</span>")
        if r.get("notes"):
            s.append("<br><span style='color:#777;font-size:12px'>Checks: " + "; ".join(r["notes"]) + "</span>")
        if r.get("files"):
            s.append(f"<br><span style='font-size:12px;color:#555'>Documents: {', '.join(r['files'])}</span>")
        s.append(f"<br><span style='font-size:12px;color:#555'>Link blocked? {alt}</span></div>")
        return "".join(s)

    def section(title, color, items, extra_fn=lambda r: ""):
        if items:
            h.append(f"<h3 style='color:{color};border-bottom:1px solid #ddd;padding-bottom:3px'>{title} ({len(items)})</h3>")
            h.extend(card(r, extra_fn(r)) for r in items)

    section("Applied by e-mail", "#0a7d32", applied, lambda r: f"<b>Sent to:</b> {r['to']} (a copy was sent to you as Cc)")
    section("Would apply by e-mail (preview mode, nothing sent)", "#0a5fb4", preview, lambda r: f"<b>Would send to:</b> {r['to']}")
    section("Ready for you to apply (online form / portal)", "#c77700", manual,
            lambda r: "Documents are attached to this e-mail. Open the link, upload them, and submit.")
    section("Held back", "#b00", held)
    section(f"Below {A.MIN_SCORE}% after the detailed fit check (not worth applying)", "#777", lowfit)
    section("Skipped", "#777", skipped)
    section("Could not prepare", "#b00", failed)
    h.append("<p style='color:#555;font-size:12px'>To finish the 'ready for you' jobs faster, download the <b>application-kits</b> artifact "
             "from the run (Actions &gt; this run &gt; Artifacts) and use apply_helper.py on your computer: it opens each apply page, fills your "
             "details and uploads the documents, then waits for you to review and click Submit.</p>")
    h.append("<p style='color:#888;font-size:12px'>applications_tracker.csv (attached) lists every application sent so far. "
             "Read each document before relying on it; the agent only uses facts from master_resume.json.</p></div>")
    return "".join(h)


# ----------------------------------------------------------------------------
def main() -> int:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set - skipping.")
        return 0
    log = load_log()
    if not A.JOBS_FILE.exists():
        print("No scored_jobs.json - nothing to do.")
        return 0
    master = json.loads(A.MASTER_FILE.read_text())
    own_email = master["personal"].get("email", "")
    entries = [e for e in json.loads(A.JOBS_FILE.read_text()) if e["analysis"]["score"] >= A.MIN_SCORE and e.get("cc") in A.RULES]
    entries.sort(key=lambda e: e["analysis"]["score"], reverse=True)
    if not entries:
        print(f"No new jobs scoring {A.MIN_SCORE}+ today.")
        return 0

    A.OUT.mkdir(exist_ok=True)
    rows, attach, sent_now = [], [], 0
    now_s = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
    recent = {v.get("to", "").lower(): v["date"] for v in log.values() if v.get("to")}

    for idx, e in enumerate(entries):
        job, rules, a = e["job"], A.RULES[e["cc"]], e["analysis"]
        row = {"job": job, "analysis": a, "label": e["label"], "rules": rules, "status": "manual", "files": [], "notes": [], "gaps": []}
        rows.append(row)
        key = ja.dedupe_key(job)
        company = (job.get("employer_name") or "").lower()

        if idx >= A.MAX_KITS:
            row.update(status="skipped", reason=f"Over the limit of {A.MAX_KITS} kits per run - will not be prepared automatically.")
            continue
        if key in log:
            row.update(status="skipped", reason=f"Already applied on {log[key]['date']}.")
            continue
        if any(b in company for b in BLOCK):
            row.update(status="skipped", reason="Company is on your blocklist (e.g. your current employer).")
            continue
        try:
            if len(job.get("job_description") or "") < 300:
                row["notes"].append("short job description - tailoring is limited")
            t, problems = A.tailor(master, job, rules)
            row["notes"] += problems
            row["gaps"] = t["gaps"]
            row["coverage"] = t["coverage"]
            row["final"] = round((a["score"] + t["coverage"]) / 2)      # keyword score + honest resume-coverage check
            if row["final"] < A.MIN_SCORE:
                row.update(status="lowfit", reason=f"Keyword score {a['score']}% but your resume covers only about {t['coverage']}% "
                                                   f"of the essential requirements, so the final fit is {row['final']}%. No documents prepared.")
                continue
            base = f"{A.slug(job.get('employer_name'))}_{A.slug(job.get('job_title'))}_{e['cc'].upper()}"
            rp, lp = A.OUT / f"{base}_{rules['doc'].split()[0]}.docx", A.OUT / f"{base}_CoverLetter.docx"
            A.build_resume(master, t, rules, rp)
            A.build_letter(master, job, t, rules, lp)
            files = [to_pdf(rp), to_pdf(lp)]
            row["files"] = [f.name for f in files]
            row["letter_text"] = letter_text(master, job, t, rules)
        except Exception as ex:
            row.update(status="failed", reason=str(ex)[:200])
            continue

        to = find_apply_email(job, own_email)
        if not to:
            row["status"] = "manual"
            attach += files
            continue

        row["to"] = to
        why = []
        if a.get("unconfirmed"):
            why.append("the posting does not mention visa sponsorship / relocation")
        if len(job.get("job_description") or "") < 300:
            why.append("the job description is too short to tailor safely")
        if len(t["gaps"]) > MAX_GAPS:
            why.append(f"{len(t['gaps'])} requirements are not shown in your resume (limit {MAX_GAPS})")
        if to.lower() in recent:
            why.append(f"you already e-mailed {to} on {recent[to.lower()]}")
        if sent_now >= MAX_AUTO:
            why.append(f"daily limit of {MAX_AUTO} automatic applications reached")
        if why:
            row.update(status="held", reason="Not sent automatically because " + "; ".join(why) + ". Documents attached - review and send yourself.")
            attach += files
            continue
        if not AUTO_APPLY:
            row["status"] = "preview"
            attach += files
            continue
        try:
            send_application(master, job, t, rules, to, files)
            sent_now += 1
            recent[to.lower()] = now_s
            log[key] = {"date": now_s, "country": e["label"], "company": job.get("employer_name"), "title": job.get("job_title"),
                        "score": a["score"], "method": "email", "to": to, "link": job.get("job_apply_link")}
            save_log(log)                     # write immediately so a later crash can never cause a duplicate send
            row["status"] = "applied"
            print("APPLIED:", job.get("job_title"), "-", job.get("employer_name"), "->", to)
        except Exception as ex:
            row.update(status="failed", reason=f"E-mail could not be sent: {str(ex)[:160]}")
            attach += files

    save_log(log)
    # queue for the local helper (apply_helper.py): jobs you still need to submit yourself
    queue = [{"company": r["job"].get("employer_name"), "title": r["job"].get("job_title"), "country": r["label"],
              "score": r["analysis"]["score"], "status": r["status"], "link": ja.apply_links(r["job"])[0],
              "resume": r["files"][0], "cover_letter": r["files"][1], "cover_letter_text": r.get("letter_text", ""),
              "note": r.get("reason", "")}
             for r in rows if r["status"] in ("manual", "held", "preview") and len(r.get("files", [])) == 2]
    Path("apply_queue.json").write_text(json.dumps(queue, indent=1, ensure_ascii=False))
    html = report_html(rows, len(log), AUTO_APPLY)
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("applied", "preview", "manual", "held")}
    subject = (f"Application report: {counts['applied']} applied, {counts['preview'] + counts['manual'] + counts['held']} for you to review "
               f"({datetime.now(IST).strftime('%d %b')})")
    if DRY:
        Path("report_preview.html").write_text(html)
        print("DRY_RUN: wrote report_preview.html -", subject)
        return 0
    A.send_kits(subject, html, attach + ([CSV_FILE] if CSV_FILE.exists() else []))
    print("Report sent:", subject)
    return 0


if __name__ == "__main__":
    sys.exit(main())
