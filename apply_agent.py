"""
Application-kit agent.

For every new job that scores >= APPLY_MIN_SCORE (default 80) in job_agent.py, this prepares:
  * a resume/CV tailored to that job AND formatted to the norms of that country
  * a tailored cover letter
and emails them to you as attachments, with the apply link and a checklist.

It does NOT submit applications for you. Job boards and employers' systems forbid bots, CAPTCHAs
block them, and an application sent without a human check is how wrong claims get submitted.
You review the kit and click Apply yourself (about 2 minutes per job).

Honesty guard: the model may only reorder, select and rephrase facts that exist in master_resume.json.
Every number, skill and certification is checked against that file; anything not found is removed.

Env vars:
  ANTHROPIC_API_KEY  (required)  console.anthropic.com
  SMTP_USER, SMTP_PASS, MAIL_TO  same as job_agent.py
  APPLY_MIN_SCORE    default 80
  MAX_KITS_PER_RUN   default 5
  CLAUDE_MODEL       default claude-sonnet-5-5
  NEEDS_SPONSORSHIP  default true  (adds a visa/relocation line to the cover letter)
  DRY_RUN=1          write the kit files and preview.html but do not email
"""
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

MIN_SCORE = int(os.getenv("APPLY_MIN_SCORE", "80"))
MAX_KITS = int(os.getenv("MAX_KITS_PER_RUN", "5"))
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
NEEDS_SPONSORSHIP = os.getenv("NEEDS_SPONSORSHIP", "true").lower() == "true"
MASTER_FILE = Path(os.getenv("MASTER_RESUME", "master_resume.json"))
JOBS_FILE = Path(os.getenv("SCORED_JOBS", "scored_jobs.json"))
OUT = Path(os.getenv("KITS_DIR", "kits"))
IST = timezone(timedelta(hours=5, minutes=30))

# ----------------------------------------------------------------------------
# Country standards. Edit freely - these drive both the model's instructions and the document layout.
#   bullets  = (max bullets for the 2 most recent roles, max for other roles)
# ----------------------------------------------------------------------------
RULES = {
    "in": dict(country="India", doc="Resume", pages="2 pages (3 at most)", spelling="British/Indian English",
               paper="A4", photo=False, nationality=False, notice=True, visa_line=False, refs_line=False,
               bullets=(6, 4), letter_words=230, date_fmt="%d %B %Y", visa_note="",
               checklist=["Add your notice period / earliest joining date (fill notice_period in master_resume.json)."]),
    "gb": dict(country="United Kingdom", doc="CV", pages="2 pages", spelling="British English",
               paper="A4", photo=False, nationality=False, notice=False, visa_line=False, refs_line=False,
               bullets=(5, 3), letter_words=250, date_fmt="%d %B %Y",
               visa_note="I would require Skilled Worker visa sponsorship and am open to relocating to the UK.",
               checklist=["No photo, date of birth or marital status on a UK CV.",
                          "Check the employer appears on the Home Office register of licensed sponsors before applying."]),
    "de": dict(country="Germany", doc="CV (Lebenslauf)", pages="2 pages", spelling="British English",
               paper="A4", photo=True, nationality=False, notice=False, visa_line=False, refs_line=False,
               bullets=(5, 3), letter_words=250, date_fmt="%d %B %Y",
               visa_note="I would require an EU Blue Card (or another employer-supported work permit) and am open to relocating to Germany.",
               checklist=["A photo is optional in Germany; it is added only if photo_path is set in master_resume.json.",
                          "If the job ad is in German, apply with a German-language CV and letter.",
                          "Add language levels (e.g. CEFR) in master_resume.json if you want them shown."]),
    "ie": dict(country="Ireland", doc="CV", pages="2 pages", spelling="British English",
               paper="A4", photo=False, nationality=False, notice=False, visa_line=False, refs_line=False,
               bullets=(5, 3), letter_words=250, date_fmt="%d %B %Y",
               visa_note="I would require a Critical Skills Employment Permit (or another employer-supported permit) and am open to relocating to Ireland.",
               checklist=["No photo or date of birth on an Irish CV.",
                          "Check the current salary threshold for the Critical Skills Employment Permit."]),
    "nl": dict(country="Netherlands", doc="CV", pages="2 pages", spelling="British English",
               paper="A4", photo=True, nationality=False, notice=False, visa_line=False, refs_line=False,
               bullets=(5, 3), letter_words=230, date_fmt="%d %B %Y",
               visa_note="I would require sponsorship as a highly skilled migrant (kennismigrant) and am open to relocating to the Netherlands.",
               checklist=["Check the employer is a recognised sponsor on the IND public register.",
                          "A photo is optional in the Netherlands; added only if photo_path is set."]),
    "ca": dict(country="Canada", doc="Resume", pages="1-2 pages", spelling="Canadian English",
               paper="Letter", photo=False, nationality=False, notice=False, visa_line=False, refs_line=False,
               bullets=(5, 3), letter_words=250, date_fmt="%B %d, %Y",
               visa_note="I would require employer support for a Canadian work permit and am open to relocating to Canada.",
               checklist=["No photo, date of birth or personal details on a Canadian resume.",
                          "Keep it ATS-friendly: this file is single-column with no tables or images."]),
    "au": dict(country="Australia", doc="Resume", pages="2-3 pages", spelling="Australian English",
               paper="A4", photo=False, nationality=False, notice=False, visa_line=True, refs_line=True,
               bullets=(6, 3), letter_words=250, date_fmt="%d %B %Y",
               visa_note="I would require employer sponsorship (subclass 482 visa) and am open to relocating to Australia.",
               checklist=["A visa/work-rights line is included in the header, as is common in Australia.",
                          "Referees are listed as 'available on request'."]),
    "ae": dict(country="UAE", doc="CV", pages="2 pages", spelling="British English",
               paper="A4", photo=True, nationality=True, notice=True, visa_line=True, refs_line=False,
               bullets=(5, 3), letter_words=230, date_fmt="%d %B %Y",
               visa_note="I would require an employment visa sponsored by the employer and am open to relocating to the UAE.",
               checklist=["UAE CVs usually show nationality, current location and notice period: fill nationality and "
                          "notice_period in master_resume.json.",
                          "A photo is common but optional (photo_path in master_resume.json)."]),
}

NUM = re.compile(r"\d+(?:\.\d+)?")


# ----------------------------------------------------------------------------
# Model call
# ----------------------------------------------------------------------------
def http_post(url: str, retries: int = 2, **kw) -> requests.Response:
    last = None
    for i in range(retries + 1):
        try:
            r = requests.post(url, **kw)
            if r.status_code in (429, 500, 502, 503, 529) and i < retries:
                time.sleep(6 * (i + 1))
                continue
            return r
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
            if i < retries:
                time.sleep(6 * (i + 1))
    raise last


def claude_json(system: str, messages: list[dict]) -> dict:
    r = http_post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 4500, "system": system, "messages": messages},
        timeout=180,
    )
    if not r.ok:
        raise RuntimeError(f"Claude API HTTP {r.status_code}: {r.text[:200]}")
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("Claude returned no JSON")
    return json.loads(m.group(0))


def build_prompt(master: dict, job: dict, rules: dict) -> tuple[str, str]:
    system = f"""You are an expert resume writer and recruiter who knows hiring norms in {rules['country']}.
You tailor a candidate's {rules['doc']} and cover letter to ONE job.

STRICT RULES
1. Use ONLY facts found in MASTER_RESUME. Never invent employers, titles, dates, numbers, tools, certifications,
   responsibilities, or years of experience. If the job asks for something MASTER_RESUME does not show, do not claim it:
   list it under "gaps".
2. You may select, reorder and rephrase existing bullets, and use the job's own wording ONLY where it truthfully
   describes what the candidate really did. Do not copy the job ad's sentences.
3. Every bullet must cite the id of the master bullet it comes from ("source"). Every number you write must appear in MASTER_RESUME.
4. "skills" must be chosen from MASTER_RESUME.skills, spelled exactly as written there.
5. Write in {rules['spelling']}. Plain, specific, no buzzword padding, no first-person pronouns in bullets.
6. Return ONE JSON object and nothing else."""
    user = f"""COUNTRY STANDARD: {rules['doc']} for {rules['country']}, target length {rules['pages']}.
Bullets: at most {rules['bullets'][0]} for each of the two most recent roles, at most {rules['bullets'][1]} for other roles.
Cover letter: 3 paragraphs, about {rules['letter_words']} words in total, specific to the company and role. Do NOT mention visas
or relocation (that is added separately). Do not invent anything about the company beyond what the job text says.

JOB
Title: {job.get('job_title')}
Company: {job.get('employer_name')}
Location: {job.get('job_city') or ''} {job.get('job_country') or ''}
Description:
{(job.get('job_description') or '')[:7000]}

MASTER_RESUME
{json.dumps({k: master[k] for k in ('summary_base', 'skills', 'achievements', 'experience', 'certifications')}, ensure_ascii=False)}

Return JSON with exactly these keys:
{{
 "summary": "3-4 sentence professional summary, max 70 words, tailored to this job",
 "skills": ["10 to 16 items from MASTER_RESUME.skills, most relevant to this job first"],
 "achievement_ids": ["ids from MASTER_RESUME.achievements, 3 to 4, most relevant first"],
 "roles": {{"ltim": [{{"source": "ltim-3", "text": "..."}}], "hexaware": [...], "tcs": [...], "informist": [...]}},
 "cover_letter": ["paragraph 1", "paragraph 2", "paragraph 3"],
 "fit_notes": ["2-4 short reasons the candidate fits this job"],
 "gaps": ["requirements in the job that MASTER_RESUME does not clearly show; empty list if none"]
}}"""
    return system, user


# ----------------------------------------------------------------------------
# Honesty guard
# ----------------------------------------------------------------------------
def allowed_numbers(master: dict) -> set[str]:
    return set(NUM.findall(json.dumps(master, ensure_ascii=False)))


def bad_numbers(text: str, allowed: set[str]) -> list[str]:
    return sorted({n for n in NUM.findall(text or "") if n not in allowed})


def drop_bad_sentences(text: str, allowed: set[str]) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text or "")
    return " ".join(p for p in parts if not bad_numbers(p, allowed)).strip()


def validate(t: dict, master: dict, rules: dict) -> tuple[dict, list[str]]:
    """Returns (cleaned tailoring, list of problems that were fixed or removed)."""
    allowed = allowed_numbers(master)
    problems = []
    by_id = {b["id"]: b["text"] for e in master["experience"] for b in e["bullets"]}
    skills_lc = {s.lower(): s for s in master["skills"]}
    ach_ids = {a["id"] for a in master["achievements"]}

    clean = {"summary": drop_bad_sentences(t.get("summary", ""), allowed) or master["summary_base"].split(". ")[0] + ".",
             "fit_notes": [str(x) for x in t.get("fit_notes", [])][:4],
             "gaps": [str(x) for x in t.get("gaps", [])][:8]}
    if bad_numbers(t.get("summary", ""), allowed):
        problems.append("summary: removed sentences with numbers not in your resume")

    clean["skills"] = []
    for s in t.get("skills", []):
        k = str(s).strip().lower()
        if k in skills_lc and skills_lc[k] not in clean["skills"]:
            clean["skills"].append(skills_lc[k])
        else:
            problems.append(f"skill not in your resume, removed: {s}")
    if len(clean["skills"]) < 8:
        clean["skills"] = (clean["skills"] + [s for s in master["skills"] if s not in clean["skills"]])[:12]

    clean["achievement_ids"] = [a for a in t.get("achievement_ids", []) if a in ach_ids][:4] or ["ach-1", "ach-2", "ach-3"]

    clean["roles"] = {}
    for idx, e in enumerate([e for e in master["experience"] if e["tier"] == "main"]):
        cap = rules["bullets"][0] if idx < 2 else rules["bullets"][1]
        out = []
        for b in (t.get("roles", {}) or {}).get(e["id"], []) or []:
            src, text = b.get("source"), (b.get("text") or "").strip()
            if src not in by_id or not text:
                problems.append(f"bullet without a valid source removed ({e['id']})")
                continue
            if bad_numbers(text, allowed):
                problems.append(f"bullet had numbers not in your resume, replaced with original ({src})")
                text = by_id[src]
            out.append(text)
        clean["roles"][e["id"]] = out[:cap] or [b["text"] for b in e["bullets"]][:cap]

    paras = [str(p).strip() for p in t.get("cover_letter", []) if str(p).strip()][:4]
    fixed = [drop_bad_sentences(p, allowed) for p in paras]
    if fixed != paras:
        problems.append("cover letter: removed sentences with numbers not in your resume")
    clean["cover_letter"] = [p for p in fixed if p]
    return clean, problems


def tailor(master: dict, job: dict, rules: dict) -> tuple[dict, list[str]]:
    system, user = build_prompt(master, job, rules)
    raw = claude_json(system, [{"role": "user", "content": user}])
    return validate(raw, master, rules)


# ----------------------------------------------------------------------------
# DOCX builders (single column, no tables/images except optional photo: ATS friendly)
# ----------------------------------------------------------------------------
FONT = "Calibri"
ACCENT = RGBColor(0x1F, 0x3A, 0x5F)


def new_doc(rules: dict) -> Document:
    d = Document()
    sec = d.sections[0]
    if rules["paper"] == "Letter":
        sec.page_width, sec.page_height = Cm(21.59), Cm(27.94)
    else:
        sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    for side in ("left_margin", "right_margin"):
        setattr(sec, side, Cm(1.8))
    sec.top_margin = sec.bottom_margin = Cm(1.5)
    st = d.styles["Normal"]
    st.font.name, st.font.size = FONT, Pt(10.5)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    st.paragraph_format.space_after = Pt(2)
    st.paragraph_format.space_before = Pt(0)
    return d


def text_width_cm(rules: dict) -> float:
    return (21.59 if rules["paper"] == "Letter" else 21.0) - 3.6


def heading(d: Document, text: str) -> None:
    p = d.add_paragraph()
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.keep_with_next = True      # never leave a heading alone at the bottom of a page
    r = p.add_run(text.upper())
    r.bold, r.font.size, r.font.color.rgb = True, Pt(11), ACCENT
    pPr = p._p.get_or_add_pPr()
    b = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for k, v in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "1F3A5F")):
        bottom.set(qn(k), v)
    b.append(bottom)
    pPr.append(b)


def bullet(d: Document, text: str) -> None:
    p = d.add_paragraph(style="List Bullet")
    p.paragraph_format.space_after = Pt(1)
    p.paragraph_format.left_indent = Cm(0.6)
    p.add_run(text)


def role_line(d: Document, left: str, right: str, rules: dict, sub: str = "") -> None:
    p = d.add_paragraph()
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.keep_with_next = True
    p.paragraph_format.tab_stops.add_tab_stop(Cm(text_width_cm(rules)), WD_TAB_ALIGNMENT.RIGHT)
    r = p.add_run(left)
    r.bold = True
    p.add_run("\t" + right).italic = True
    if sub:
        s = d.add_paragraph()
        s.paragraph_format.keep_with_next = True
        s.add_run(sub).italic = True


def contact_items(master: dict, rules: dict) -> list[str]:
    p = master["personal"]
    items = [p.get("location"), p.get("phone"), p.get("email"), p.get("linkedin")]
    if rules["nationality"] and p.get("nationality"):
        items.append(f"Nationality: {p['nationality']}")
    if rules["notice"] and p.get("notice_period"):
        items.append(f"Notice period: {p['notice_period']}")
    return [i for i in items if i]


def build_resume(master: dict, t: dict, rules: dict, path: Path) -> None:
    d, p = new_doc(rules), master["personal"]
    photo = p.get("photo_path")
    use_photo = rules["photo"] and photo and Path(photo).exists()

    if use_photo:
        tbl = d.add_table(rows=1, cols=2)
        left, right = tbl.rows[0].cells
        right.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
        right.paragraphs[0].add_run().add_picture(photo, width=Cm(2.8))
        hp = left.paragraphs[0]
    else:
        hp = d.add_paragraph()
    r = hp.add_run(p["name"])
    r.bold, r.font.size, r.font.color.rgb = True, Pt(20), ACCENT
    if use_photo:
        h2, h3 = left.add_paragraph(), left.add_paragraph()
    else:
        h2, h3 = d.add_paragraph(), d.add_paragraph()
    h2.add_run(p["headline"]).font.size = Pt(11)
    h3.add_run("  |  ".join(contact_items(master, rules))).font.size = Pt(9.5)
    if rules["visa_line"] and NEEDS_SPONSORSHIP:
        h4 = left.add_paragraph() if use_photo else d.add_paragraph()
        h4.add_run("Work rights: requires employer visa sponsorship; open to relocation").font.size = Pt(9.5)

    heading(d, "Professional Summary")
    d.add_paragraph(t["summary"])

    heading(d, "Core Skills")
    d.add_paragraph("  \u2022  ".join(t["skills"]))

    heading(d, "Key Achievements")
    ach = {a["id"]: a["text"] for a in master["achievements"]}
    for aid in t["achievement_ids"]:
        bullet(d, ach[aid])

    heading(d, "Professional Experience")
    for e in [x for x in master["experience"] if x["tier"] == "main"]:
        role_line(d, f"{e['title']}  |  {e['company']}", f"{e['start']} - {e['end']}", rules, sub=e["location"])
        for b in t["roles"][e["id"]]:
            bullet(d, b)

    earlier = [x for x in master["experience"] if x["tier"] == "earlier"]
    if earlier:
        heading(d, "Earlier Experience")
        for e in earlier:
            role_line(d, f"{e['title']}  |  {e['company']}", f"{e['start']} - {e['end']}", rules)

    heading(d, "Certifications")
    for c in master["certifications"]:
        bullet(d, c)

    heading(d, "Education")
    for e in master["education"]:
        if e["level"] == "degree" or rules["country"] == "India":
            bullet(d, e["text"])

    if master.get("languages"):
        heading(d, "Languages")
        d.add_paragraph(" | ".join(master["languages"]))
    if rules["refs_line"]:
        heading(d, "Referees")
        d.add_paragraph("Available on request.")
    d.save(path)


def build_letter(master: dict, job: dict, t: dict, rules: dict, path: Path) -> None:
    d, p = new_doc(rules), master["personal"]
    d.styles["Normal"].paragraph_format.space_after = Pt(8)
    hp = d.add_paragraph()
    r = hp.add_run(p["name"])
    r.bold, r.font.size, r.font.color.rgb = True, Pt(16), ACCENT
    d.add_paragraph("  |  ".join(contact_items(master, rules)))
    d.add_paragraph(datetime.now(IST).strftime(rules["date_fmt"]))
    d.add_paragraph(f"Application: {job.get('job_title')} - {job.get('employer_name')}").runs[0].bold = True
    d.add_paragraph("Dear Hiring Manager,")
    for para in t["cover_letter"]:
        d.add_paragraph(para)
    if NEEDS_SPONSORSHIP and rules["visa_note"]:
        extra = rules["visa_note"]
        if p.get("notice_period"):
            extra += f" My notice period is {p['notice_period']}."
        d.add_paragraph(extra)
    d.add_paragraph("Thank you for your time and consideration. I would welcome the opportunity to discuss the role.")
    d.add_paragraph("Kind regards,\n" + p["name"])
    d.save(path)


# ----------------------------------------------------------------------------
# Email
# ----------------------------------------------------------------------------
def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")[:40] or "job"


def kit_html(kits: list[dict], failed: list[str], skipped: list[str]) -> str:
    import job_agent as ja
    today = datetime.now(IST).strftime("%a %d %b %Y")
    h = ["<div style='font-family:Segoe UI,Arial,sans-serif;max-width:760px'>",
         f"<h2>Application kits - {today}</h2>",
         f"<p>{len(kits)} job(s) scored {MIN_SCORE}+. Each has a tailored resume/CV and cover letter attached, formatted for that country. "
         "<b>Review before you apply</b> - applications are not sent for you.</p>"]
    for k in kits:
        job, a, rules = k["job"], k["analysis"], k["rules"]
        main_link, alt = ja.apply_links(job)
        h.append("<div style='border:1px solid #ddd;border-radius:8px;padding:10px 14px;margin:12px 0'>")
        h.append(f"<a href='{main_link}' style='font-size:16px;font-weight:600'>{job.get('job_title')}</a> {ja.badge(a['score'])}<br>"
                 f"<span>{job.get('employer_name')} &middot; {k['label']} &middot; {rules['doc']}, {rules['pages']}</span>")
        if a.get("unconfirmed"):
            h.append("<br><span style='color:#b60'><i>Posting does not mention sponsorship - ask the recruiter before investing more time.</i></span>")
        if k["fit_notes"]:
            h.append("<p style='margin:8px 0 2px'><b>Why you fit</b></p><ul style='margin:0'>" + "".join(f"<li>{x}</li>" for x in k["fit_notes"]) + "</ul>")
        if k["gaps"]:
            h.append("<p style='margin:8px 0 2px;color:#b00'><b>Gaps - not claimed in your documents</b></p><ul style='margin:0'>" + "".join(f"<li>{x}</li>" for x in k["gaps"]) + "</ul>")
        if k["notes"]:
            h.append("<p style='margin:8px 0 2px;color:#555'><b>Automatic checks</b></p><ul style='margin:0;color:#555'>" + "".join(f"<li>{x}</li>" for x in k["notes"]) + "</ul>")
        h.append("<p style='margin:8px 0 2px'><b>Before you apply</b></p><ul style='margin:0'>" +
                 "".join(f"<li>{x}</li>" for x in rules["checklist"] + ["Read every line of the resume and letter; delete anything that is not true for you."]) + "</ul>")
        h.append(f"<p style='font-size:12px;color:#555;margin:8px 0 0'>Attached: {', '.join(k['files'])}. Link blocked? {alt}</p></div>")
    if skipped:
        h.append("<p style='color:#777'>Skipped (kit limit): " + "; ".join(skipped) + "</p>")
    if failed:
        h.append("<p style='color:#b00'>Could not prepare: " + "; ".join(failed) + "</p>")
    h.append("</div>")
    return "".join(h)


def send_kits(subject: str, html: str, files: list[Path]) -> None:
    import job_agent as ja
    user, pw = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", "")
    to = os.getenv("MAIL_TO") or user
    msg = MIMEMultipart("mixed")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(re.sub(r"<[^>]+>", "", html), "plain"))
    alt.attach(MIMEText(html, "html"))
    msg.attach(alt)
    for f in files:
        part = MIMEApplication(f.read_bytes(), Name=f.name)
        part["Content-Disposition"] = f'attachment; filename="{f.name}"'
        msg.attach(part)
    ja.smtp_send(msg, user, pw, to)


# ----------------------------------------------------------------------------
def main() -> int:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set - skipping application kits.")
        return 0
    if not JOBS_FILE.exists():
        print("No scored_jobs.json - run job_agent.py first.")
        return 0
    master = json.loads(MASTER_FILE.read_text())
    entries = [e for e in json.loads(JOBS_FILE.read_text()) if e["analysis"]["score"] >= MIN_SCORE and e.get("cc") in RULES]
    entries.sort(key=lambda e: e["analysis"]["score"], reverse=True)
    if not entries:
        print(f"No new jobs scoring {MIN_SCORE}+ today.")
        return 0

    OUT.mkdir(exist_ok=True)
    kits, failed, files = [], [], []
    todo, skipped = entries[:MAX_KITS], [f"{e['job'].get('job_title')} ({e['job'].get('employer_name')})" for e in entries[MAX_KITS:]]
    for e in todo:
        job, rules = e["job"], RULES[e["cc"]]
        try:
            notes = []
            if len(job.get("job_description") or "") < 300:
                notes.append("The job description we received is short, so tailoring is limited - open the posting and check.")
            t, problems = tailor(master, job, rules)
            notes += problems
            base = f"{slug(job.get('employer_name'))}_{slug(job.get('job_title'))}_{e['cc'].upper()}"
            rp, lp = OUT / f"{base}_{rules['doc'].split()[0]}.docx", OUT / f"{base}_CoverLetter.docx"
            build_resume(master, t, rules, rp)
            build_letter(master, job, t, rules, lp)
            files += [rp, lp]
            kits.append({"job": job, "analysis": e["analysis"], "label": e["label"], "rules": rules,
                         "fit_notes": t["fit_notes"], "gaps": t["gaps"], "notes": notes,
                         "files": [rp.name, lp.name]})
            print("Kit ready:", job.get("job_title"), "-", job.get("employer_name"))
        except Exception as ex:
            failed.append(f"{job.get('job_title')} ({job.get('employer_name')}): {ex}")

    if not kits and not failed:
        return 0
    html = kit_html(kits, failed, skipped)
    subject = f"Application kits ready: {len(kits)} job(s) scoring {MIN_SCORE}+"
    if os.getenv("DRY_RUN"):
        Path("kits_preview.html").write_text(html)
        print("DRY_RUN: wrote kits_preview.html -", subject)
    elif kits:
        send_kits(subject, html, files)
        print("Sent:", subject)
    return 0 if kits or not failed else 1


if __name__ == "__main__":
    sys.exit(main())
