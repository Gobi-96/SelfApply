"""Prompt builder for the autonomous job application agent.

Constructs the full instruction prompt that tells Claude Code / the AI agent
how to fill out a job application form using Playwright MCP tools. All
personal data is loaded from the user's profile -- nothing is hardcoded.
"""

import ast
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from selfapply import config

logger = logging.getLogger(__name__)


def _build_profile_summary(profile: dict) -> str:
    """Format the applicant profile section of the prompt.

    Reads all relevant fields from the profile dict and returns a
    human-readable multi-line summary for the agent.
    """
    p = profile
    personal = p["personal"]
    work_auth = p["work_authorization"]
    comp = p["compensation"]
    exp = p.get("experience", {})
    avail = p.get("availability", {})
    eeo = p.get("eeo_voluntary", {})
    disclosures = p.get("employment_disclosures", {})

    lines = [
        f"Name: {personal['full_name']}",
        f"Email: {personal['email']}",
        f"Phone: {personal['phone']}",
    ]

    # Address -- handle optional fields gracefully
    addr_parts = [
        personal.get("address", ""),
        personal.get("city", ""),
        personal.get("province_state", ""),
        personal.get("country", ""),
        personal.get("postal_code", ""),
    ]
    lines.append(f"Address: {', '.join(p for p in addr_parts if p)}")

    if personal.get("linkedin_url"):
        lines.append(f"LinkedIn: {personal['linkedin_url']}")
    if personal.get("github_url"):
        lines.append(f"GitHub: {personal['github_url']}")
    if personal.get("portfolio_url"):
        lines.append(f"Portfolio: {personal['portfolio_url']}")
    if personal.get("website_url"):
        lines.append(f"Website: {personal['website_url']}")

    # Work authorization
    lines.append(f"Work Auth: {work_auth.get('legally_authorized_to_work', 'See profile')}")
    lines.append(f"Sponsorship Needed: {work_auth.get('require_sponsorship', 'See profile')}")
    if work_auth.get("work_permit_type"):
        lines.append(f"Work Permit: {work_auth['work_permit_type']}")
    if "restrictive_covenant" in disclosures:
        lines.append(
            "Subject to Non-compete/Restrictive Agreement: "
            + ("Yes" if disclosures["restrictive_covenant"] else "No")
        )

    # Compensation
    currency = comp.get("salary_currency", "").strip()
    if comp.get("salary_expectation"):
        amount = str(comp["salary_expectation"]).strip()
        lines.append(f"Salary Expectation: {amount} {currency}".strip())

    # Experience
    if exp.get("years_of_experience_total"):
        lines.append(f"Years Experience: {exp['years_of_experience_total']}")
    if exp.get("education_level"):
        lines.append(f"Education: {exp['education_level']}")

    # Availability
    if avail.get("earliest_start_date"):
        lines.append(f"Available: {avail['earliest_start_date']}")

    # These answers materially differ between applicants. Include them only
    # when the user supplied them; the agent must never inherit a developer's
    # age, background, criminal-history, or prior-employment answer.
    disclosure_labels = {
        "age_18_or_older": "Age 18+",
        "consents_to_background_check": "Background Check",
        "felony_conviction": "Felony",
        "previously_worked_for_employer": "Previously Worked Here",
    }
    for key, label in disclosure_labels.items():
        if key in disclosures:
            lines.append(f"{label}: {'Yes' if disclosures[key] else 'No'}")

    # EEO
    lines.append(f"Gender: {eeo.get('gender', 'Decline to self-identify')}")
    lines.append(f"Race: {eeo.get('race_ethnicity', 'Decline to self-identify')}")
    lines.append(f"Sexual Orientation: {eeo.get('sexual_orientation', 'Decline to self-identify')}")
    lines.append(f"Veteran: {eeo.get('veteran_status', 'Decline to self-identify')}")
    lines.append(f"Disability: {eeo.get('disability_status', 'I do not wish to answer')}")

    return "\n".join(lines)


def _build_location_check(profile: dict, search_config: dict) -> str:
    """Build the location eligibility check section of the prompt.

    Uses the accept_patterns from search config to determine which cities
    are acceptable for hybrid/onsite roles.
    """
    personal = profile["personal"]
    location_cfg = search_config.get("location", {})
    accept_patterns = location_cfg.get("accept_patterns", [])
    primary_city = personal.get("city", location_cfg.get("primary", "the candidate's city"))
    home_country = personal.get("country") or "the candidate's country"

    # Older wizard-generated files may omit `location.accept_patterns` while
    # still containing valid values under `locations` or `defaults.location`.
    if not accept_patterns:
        inferred: list[str] = []

        def _add_location(value: object) -> None:
            if value is None:
                return
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    _add_location(item)
                return
            text = str(value).strip()
            if not text:
                return
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                parsed = text
            if parsed != text and isinstance(parsed, (list, tuple, set)):
                _add_location(parsed)
            elif text not in inferred:
                inferred.append(text)

        _add_location(primary_city)
        for entry in search_config.get("locations", []):
            _add_location(entry.get("location") if isinstance(entry, dict) else entry)
        _add_location(search_config.get("defaults", {}).get("location"))
        accept_patterns = inferred

    # Build the list of acceptable cities for hybrid/onsite
    if accept_patterns:
        city_list = ", ".join(accept_patterns)
    else:
        city_list = primary_city

    return f"""== LOCATION CHECK (do this FIRST before any form) ==
Read the job page. Determine the work arrangement. Then decide:
- Fully remote and explicitly open to {home_country} or the candidate's home
  location -> ELIGIBLE, even when the employer headquarters is elsewhere.
- A geographically restricted remote role is eligible only when its stated
  region includes the candidate's profile location or an accepted location.
- "Hybrid" or "onsite" in {city_list} -> ELIGIBLE. Apply.
- "Hybrid" or "onsite" in another city BUT the posting also says "remote OK" or "remote option available" -> ELIGIBLE. Apply.
- "Onsite only" or "hybrid only" outside the accepted locations with no remote
  option -> NOT ELIGIBLE. Stop immediately. Output RESULT:FAILED:not_eligible_location
- Cannot determine location -> Continue applying. If a screening question reveals it's non-local onsite, answer honestly and let the system reject if needed.
Do NOT fill out forms for jobs that are clearly onsite in a non-acceptable location. Check EARLY, save time."""


def _build_salary_section(profile: dict) -> str:
    """Build the salary negotiation instructions.

    Adapts floor, range, and currency from the profile's compensation section.
    """
    comp = profile.get("compensation", {})
    currency = str(comp.get("salary_currency") or "the profile currency").strip()
    floor = str(comp.get("salary_expectation") or "").strip()
    if not floor:
        return """== SALARY ==
No salary expectation is saved. Use a posted range only when the form permits
an answer based on that range. If a required salary answer cannot be grounded
in the profile, output RESULT:FAILED:missing_profile_answer."""
    range_min = comp.get("salary_range_min", floor)
    range_max = comp.get("salary_range_max", floor)
    conversion_note = comp.get("currency_conversion_note", "")

    # Currency conversion guidance
    if conversion_note:
        convert_line = f"Posting is in a different currency? -> {conversion_note}"
    else:
        convert_line = "Posting is in a different currency? -> Target midpoint of their range. Convert if needed."

    return f"""== SALARY (think, don't just copy) ==
{floor} {currency} is the FLOOR. Never go below it. But don't always use it either.

Decision tree:
1. Job posting shows a range? -> Answer with the midpoint in the posting's currency when that is compatible with the saved floor.
2. Use the role level and posted range; never substitute a developer-specific amount.
3. {convert_line}
4. No salary info anywhere? -> Use {floor} {currency}.
5. Asked for a range? -> Use the saved range "{range_min}-{range_max} {currency}" when no posted range exists.
6. Asked for a different pay period (hourly/monthly/annual)? Use only a
   conversion appropriate to the candidate's locale and stated working hours;
   do not assume a US work-year convention."""


def _build_screening_section(profile: dict) -> str:
    """Build the screening questions guidance section."""
    personal = profile["personal"]
    exp = profile.get("experience", {})
    city = personal.get("city", "their city")
    years = exp.get("years_of_experience_total", "multiple")
    target_role = exp.get("target_role") or personal.get("current_job_title") or "professional"
    work_auth = profile["work_authorization"]
    eeo = profile.get("eeo_voluntary", {})

    eeo_answers = ", ".join([
        f"gender={eeo.get('gender', 'Decline to self-identify')}",
        f"race/ethnicity={eeo.get('race_ethnicity', 'Decline to self-identify')}",
        f"sexual orientation={eeo.get('sexual_orientation', 'Decline to self-identify')}",
        f"veteran status={eeo.get('veteran_status', 'Decline to self-identify')}",
        f"disability={eeo.get('disability_status', 'I do not wish to answer')}",
    ])

    return f"""== SCREENING QUESTIONS (accurate and evidence-backed) ==
Hard facts -> answer truthfully from the profile. No guessing. This includes:
  - Location: lives in {city}. Relocation willingness must come from the profile;
    never infer it from the current address.
  - Work authorization: {work_auth.get('legally_authorized_to_work', 'see profile')}
  - Citizenship, clearance, licenses, certifications: answer from profile only
  - Criminal/background: answer from profile only

Skills and tools -> distinguish familiarity from claimed experience. This candidate is a {target_role} with {years} years total experience, but that does NOT imply experience with every adjacent tool.
  - Answer YES to "used/familiar with [tool]" only when the tool is explicitly present in the applicant profile or master-resume evidence.
  - Answer YES to "worked professionally/production experience with [tool]" only when the master resume or preserved profile facts explicitly support professional or production use of that exact tool. A tailored-resume keyword, adjacent technology, learning/exposure entry, or same-domain similarity is NOT sufficient evidence.
  - Questions naming specific services (for example AWS S3, CloudFront, or IAM) require evidence for those named services; generic AWS or cloud experience alone is not enough.
  - If evidence is absent, answer NO. Never infer YES because the candidate could learn the tool, because it appears in the job description, or because tailoring added it for ATS matching.

Open-ended questions ("Why do you want this role?", "Tell us about yourself", "What interests you?") -> Write 2-3 sentences. Be specific to THIS job. Reference something from the job description. Connect it to a real achievement from the resume. No generic fluff. No "I am passionate about..." -- sound like a real person.

EEO/demographics -> use the saved profile answers exactly where a matching option exists: {eeo_answers}. If wording differs, choose the semantically equivalent option. Never leave a required demographic dropdown blank."""


def _build_hard_rules(profile: dict) -> str:
    """Build the hard rules section with work auth and name from profile."""
    personal = profile["personal"]
    work_auth = profile["work_authorization"]

    full_name = personal["full_name"]
    preferred_name = personal.get("preferred_name", full_name.split()[0])
    preferred_last = full_name.split()[-1] if " " in full_name else ""
    display_name = f"{preferred_name} {preferred_last}".strip() if preferred_last else preferred_name

    # Build work auth rule dynamically
    auth_info = work_auth.get("legally_authorized_to_work", "")
    sponsorship = work_auth.get("require_sponsorship", "")
    permit_type = work_auth.get("work_permit_type", "")

    work_auth_rule = "Work auth: Answer truthfully from profile."
    if permit_type:
        work_auth_rule = f"Work auth: {permit_type}. Sponsorship needed: {sponsorship}."

    name_rule = f'Name: Legal name = {full_name}.'
    if preferred_name and preferred_name != full_name.split()[0]:
        name_rule += f' Preferred name = {preferred_name}. Use "{display_name}" unless a field specifically says "legal name".'

    return f"""== HARD RULES (never break these) ==
1. Never lie about: citizenship, work authorization, criminal history, education credentials, security clearance, licenses.
2. {work_auth_rule}
3. {name_rule}"""


def _build_captcha_section() -> str:
    """Build the CAPTCHA detection and solving instructions.

    Reads the CapSolver API key from environment. The CAPTCHA section
    contains no personal data -- it's the same for every user.
    """
    config.load_env()
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")

    return f"""== CAPTCHA ==
You solve CAPTCHAs via the CapSolver REST API. No browser extension. You control the entire flow.
API key: {capsolver_key or 'NOT CONFIGURED — skip to MANUAL FALLBACK for all CAPTCHAs'}
API base: https://api.capsolver.com

CRITICAL RULE: When ANY CAPTCHA appears (hCaptcha, reCAPTCHA, Turnstile -- regardless of what it looks like visually), you MUST:
1. Run CAPTCHA DETECT to get the type and sitekey
2. Run CAPTCHA SOLVE (createTask -> poll -> inject) with the CapSolver API
3. ONLY go to MANUAL FALLBACK if CapSolver returns errorId > 0
Do NOT skip the API call based on what the CAPTCHA looks like. CapSolver solves CAPTCHAs server-side -- it does NOT need to see or interact with images, puzzles, or games. Even "drag the pipe" or "click all traffic lights" hCaptchas are solved via API token, not visually. ALWAYS try the API first.

--- CAPTCHA DETECT ---
Run this browser_evaluate after every navigation, Apply/Submit/Login click, or when a page feels stuck.
IMPORTANT: Detection order matters. hCaptcha elements also have data-sitekey, so check hCaptcha BEFORE reCAPTCHA.

browser_evaluate function: () => {{{{
  const r = {{}};
  const url = window.location.href;
  // 1. hCaptcha (check FIRST -- hCaptcha uses data-sitekey too)
  const hc = document.querySelector('.h-captcha, [data-hcaptcha-sitekey]');
  if (hc) {{{{
    r.type = 'hcaptcha'; r.sitekey = hc.dataset.sitekey || hc.dataset.hcaptchaSitekey;
  }}}}
  if (!r.type && document.querySelector('script[src*="hcaptcha.com"], iframe[src*="hcaptcha.com"]')) {{{{
    const el = document.querySelector('[data-sitekey]');
    if (el) {{{{ r.type = 'hcaptcha'; r.sitekey = el.dataset.sitekey; }}}}
  }}}}
  // 2. Cloudflare Turnstile
  if (!r.type) {{{{
    const cf = document.querySelector('.cf-turnstile, [data-turnstile-sitekey]');
    if (cf) {{{{
      r.type = 'turnstile'; r.sitekey = cf.dataset.sitekey || cf.dataset.turnstileSitekey;
      if (cf.dataset.action) r.action = cf.dataset.action;
      if (cf.dataset.cdata) r.cdata = cf.dataset.cdata;
    }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="challenges.cloudflare.com"]')) {{{{
    r.type = 'turnstile_script_only'; r.note = 'Wait 3s and re-detect.';
  }}}}
  // 3. reCAPTCHA v3 (invisible, loaded via render= param)
  if (!r.type) {{{{
    const s = document.querySelector('script[src*="recaptcha"][src*="render="]');
    if (s) {{{{
      const m = s.src.match(/render=([^&]+)/);
      if (m && m[1] !== 'explicit') {{{{ r.type = 'recaptchav3'; r.sitekey = m[1]; }}}}
    }}}}
  }}}}
  // 4. reCAPTCHA v2 (checkbox or invisible)
  if (!r.type) {{{{
    const rc = document.querySelector('.g-recaptcha');
    if (rc) {{{{ r.type = 'recaptchav2'; r.sitekey = rc.dataset.sitekey; }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="recaptcha"]')) {{{{
    const el = document.querySelector('[data-sitekey]');
    if (el) {{{{ r.type = 'recaptchav2'; r.sitekey = el.dataset.sitekey; }}}}
  }}}}
  // 5. FunCaptcha (Arkose Labs)
  if (!r.type) {{{{
    const fc = document.querySelector('#FunCaptcha, [data-pkey], .funcaptcha');
    if (fc) {{{{ r.type = 'funcaptcha'; r.sitekey = fc.dataset.pkey; }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="arkoselabs"], script[src*="funcaptcha"]')) {{{{
    const el = document.querySelector('[data-pkey]');
    if (el) {{{{ r.type = 'funcaptcha'; r.sitekey = el.dataset.pkey; }}}}
  }}}}
  if (r.type) {{{{ r.url = url; return r; }}}}
  return null;
}}}}

Result actions:
- null -> no CAPTCHA. Continue normally.
- "turnstile_script_only" -> browser_wait_for time: 3, re-run detect.
- Any other type -> proceed to CAPTCHA SOLVE below.

--- CAPTCHA SOLVE ---
Three steps: createTask -> poll -> inject. Do each as a separate browser_evaluate call.

STEP 1 -- CREATE TASK (copy this exactly, fill in the 3 placeholders):
browser_evaluate function: async () => {{{{
  const r = await fetch('https://api.capsolver.com/createTask', {{{{
    method: 'POST',
    headers: {{{{'Content-Type': 'application/json'}}}},
    body: JSON.stringify({{{{
      clientKey: '{capsolver_key}',
      task: {{{{
        type: 'TASK_TYPE',
        websiteURL: 'PAGE_URL',
        websiteKey: 'SITE_KEY'
      }}}}
    }}}})
  }}}});
  return await r.json();
}}}}

TASK_TYPE values (use EXACTLY these strings):
  hcaptcha     -> HCaptchaTaskProxyLess
  recaptchav2  -> ReCaptchaV2TaskProxyLess
  recaptchav3  -> ReCaptchaV3TaskProxyLess
  turnstile    -> AntiTurnstileTaskProxyLess
  funcaptcha   -> FunCaptchaTaskProxyLess

PAGE_URL = the url from detect result. SITE_KEY = the sitekey from detect result.
For recaptchav3: add "pageAction": "submit" to the task object (or the actual action found in page scripts).
For turnstile: add "metadata": {{"action": "...", "cdata": "..."}} if those were in detect result.

Response: {{"errorId": 0, "taskId": "abc123"}} on success.
If errorId > 0 -> CAPTCHA SOLVE failed. Go to MANUAL FALLBACK.

STEP 2 -- POLL (replace TASK_ID with the taskId from step 1):
Loop: browser_wait_for time: 3, then run:
browser_evaluate function: async () => {{{{
  const r = await fetch('https://api.capsolver.com/getTaskResult', {{{{
    method: 'POST',
    headers: {{{{'Content-Type': 'application/json'}}}},
    body: JSON.stringify({{{{
      clientKey: '{capsolver_key}',
      taskId: 'TASK_ID'
    }}}})
  }}}});
  return await r.json();
}}}}

- status "processing" -> wait 3s, poll again. Max 10 polls (30s).
- status "ready" -> extract token:
    reCAPTCHA: solution.gRecaptchaResponse
    hCaptcha:  solution.gRecaptchaResponse
    Turnstile: solution.token
- errorId > 0 or 30s timeout -> MANUAL FALLBACK.

STEP 3 -- INJECT TOKEN (replace THE_TOKEN with actual token string):

For reCAPTCHA v2/v3:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => {{{{ el.value = token; el.style.display = 'block'; }}}});
  if (window.___grecaptcha_cfg) {{{{
    const clients = window.___grecaptcha_cfg.clients;
    for (const key in clients) {{{{
      const walk = (obj, d) => {{{{
        if (d > 4 || !obj) return;
        for (const k in obj) {{{{
          if (typeof obj[k] === 'function' && k.length < 3) try {{{{ obj[k](token); }}}} catch(e) {{{{}}}}
          else if (typeof obj[k] === 'object') walk(obj[k], d+1);
        }}}}
      }}}};
      walk(clients[key], 0);
    }}}}
  }}}}
  return 'injected';
}}}}

For hCaptcha:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const ta = document.querySelector('[name="h-captcha-response"], textarea[name*="hcaptcha"]');
  if (ta) ta.value = token;
  document.querySelectorAll('iframe[data-hcaptcha-response]').forEach(f => f.setAttribute('data-hcaptcha-response', token));
  const cb = document.querySelector('[data-hcaptcha-widget-id]');
  if (cb && window.hcaptcha) try {{{{ window.hcaptcha.getResponse(cb.dataset.hcaptchaWidgetId); }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

For Turnstile:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const inp = document.querySelector('[name="cf-turnstile-response"], input[name*="turnstile"]');
  if (inp) inp.value = token;
  if (window.turnstile) try {{{{ const w = document.querySelector('.cf-turnstile'); if (w) window.turnstile.getResponse(w); }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

For FunCaptcha:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const inp = document.querySelector('#FunCaptcha-Token, input[name="fc-token"]');
  if (inp) inp.value = token;
  if (window.ArkoseEnforcement) try {{{{ window.ArkoseEnforcement.setConfig({{{{data: {{{{blob: token}}}}}}}}) }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

After injecting: browser_wait_for time: 2, then snapshot.
- Widget gone or green check -> success. Click Submit if needed.
- No change -> click Submit/Verify/Continue button (some sites need it).
- Still stuck -> token may have expired (~2 min lifetime). Re-run from STEP 1.

--- MANUAL FALLBACK ---
Never click, solve, or bypass a visible interactive CAPTCHA yourself -- this
includes a reCAPTCHA "I'm not a robot" checkbox with no challenge grid showing
yet. A scripted click on that checkbox is exactly the kind of automated
interaction CAPTCHAs exist to detect and is never allowed, regardless of how
simple it looks. Before waiting, output a line exactly as:
CAPTCHA_WAITING:<captcha_type>
so SelfApply can record the blocker. Do not wait, refresh, retry, or interact
with the challenge. Close/leave the challenged page and immediately output
RESULT:CAPTCHA so the current job is marked as blocked and the worker proceeds
to the next queued job."""


def build_prompt(job: dict, tailored_resume: str,
                 cover_letter: str | None = None,
                 dry_run: bool = False,
                 upload_dir: Path | None = None) -> str:
    """Build the full instruction prompt for the apply agent.

    Loads the user profile and search config internally. All personal data
    comes from the profile -- nothing is hardcoded.

    Args:
        job: Job dict from the database (must have url, title, site,
             application_url, fit_score, tailored_resume_path).
        tailored_resume: Plain-text content of the tailored resume.
        cover_letter: Optional plain-text cover letter content.
        dry_run: If True, tell the agent not to click Submit.
        upload_dir: Optional MCP workspace directory for staged upload files.

    Returns:
        Complete prompt string for the AI agent.
    """
    profile = config.load_profile()
    search_config = config.load_search_config()
    personal = profile["personal"]

    # --- Resolve resume PDF path ---
    resume_path = job.get("tailored_resume_path")
    if not resume_path:
        raise ValueError(f"No tailored resume for job: {job.get('title', 'unknown')}")

    src_pdf = Path(resume_path).with_suffix(".pdf").resolve()
    if not src_pdf.exists():
        raise ValueError(f"Resume PDF not found: {src_pdf}")

    # Copy to a clean filename for upload (recruiters see the filename)
    full_name = personal["full_name"]
    name_slug = full_name.replace(" ", "_")
    dest_dir = upload_dir or (config.APPLY_WORKER_DIR / "current")
    dest_dir.mkdir(parents=True, exist_ok=True)
    upload_pdf = dest_dir / f"{name_slug}_Resume.pdf"
    shutil.copy(str(src_pdf), str(upload_pdf))
    pdf_path = str(upload_pdf)

    # --- Cover letter handling ---
    cover_letter_text = cover_letter or ""
    cl_upload_path = ""
    cl_path = job.get("cover_letter_path")
    if cl_path and Path(cl_path).exists():
        cl_src = Path(cl_path)
        # Read text from .txt sibling (PDF is binary)
        cl_txt = cl_src.with_suffix(".txt")
        if cl_txt.exists():
            cover_letter_text = cl_txt.read_text(encoding="utf-8")
        elif cl_src.suffix == ".txt":
            cover_letter_text = cl_src.read_text(encoding="utf-8")
        # Upload must be PDF
        cl_pdf_src = cl_src.with_suffix(".pdf")
        if cl_pdf_src.exists():
            cl_upload = dest_dir / f"{name_slug}_Cover_Letter.pdf"
            shutil.copy(str(cl_pdf_src), str(cl_upload))
            cl_upload_path = str(cl_upload)

    # --- Build all prompt sections ---
    profile_summary = _build_profile_summary(profile)
    location_check = _build_location_check(profile, search_config)
    salary_section = _build_salary_section(profile)
    screening_section = _build_screening_section(profile)
    hard_rules = _build_hard_rules(profile)
    captcha_section = _build_captcha_section()

    # Cover letter fallback text
    city = personal.get("city", "the area")
    if not cover_letter_text:
        cl_display = (
            f"None available. Skip if optional. If required, write 2 factual "
            f"sentences: (1) relevant experience from the resume that matches "
            f"this role, (2) available immediately and based in {city}."
        )
    else:
        cl_display = cover_letter_text

    # Phone digits only (for fields with country prefix)
    phone_digits = "".join(c for c in personal.get("phone", "") if c.isdigit())

    # Preferred display name
    gmail_browser_allowed = bool(
        personal.get("allow_gmail_verification_read", False)
    )
    if gmail_browser_allowed:
        email_verification_steps = f"""Use search_emails + read_email when those
        read-only tools are available. If they are unavailable, open a NEW browser
        tab to https://mail.google.com/ using the already signed-in Google account.
        Search only for the newest message from the current employer/portal that
        contains the verification code, confirmation link, or password-reset link
        needed for this application. Read only that matching message, copy the code
        or open the required link, return to the application tab, and close the Gmail
        tab. Never open unrelated messages; never send, reply, delete, archive, label,
        mark spam, change Gmail settings, or download attachments. If Gmail asks for
        a password or 2-step verification, do not wait for the user; output
        RESULT:FAILED:google_session_expired."""
    else:
        email_verification_steps = (
            "No Gmail-read consent is saved. Do not open Gmail. If email "
            "verification is mandatory, output RESULT:FAILED:email_verification_unavailable."
        )

    preferred_name = personal.get("preferred_name", full_name.split()[0])
    last_name = full_name.split()[-1] if " " in full_name else ""
    display_name = f"{preferred_name} {last_name}".strip()

    # Dry-run: override submit instruction
    if dry_run:
        submit_instruction = "IMPORTANT: Do NOT click the final Submit/Apply button. Review the form, verify all fields, then output RESULT:APPLIED with a note that this was a dry run."
    else:
        submit_instruction = "BEFORE clicking Submit/Apply, take a snapshot and review EVERY field on the page. Verify all data matches the APPLICANT PROFILE and TAILORED RESUME -- name, email, phone, location, work auth, resume uploaded, cover letter if applicable. If anything is wrong or missing, fix it FIRST. Only click Submit after confirming everything is correct."

    prompt = f"""You are an autonomous job application agent. Your ONE mission: get this candidate an interview. You have all the information and tools. Think strategically. Act decisively. Submit the application.

== JOB ==
URL: {job.get('application_url') or job['url']}
Title: {job['title']}
Company: {job.get('site', 'Unknown')}
Fit Score: {job.get('fit_score', 'N/A')}/10

== FILES ==
Resume PDF (upload this): {pdf_path}
Cover Letter PDF (upload if asked): {cl_upload_path or "N/A"}

== RESUME TEXT (use when filling text fields) ==
{tailored_resume}

== COVER LETTER TEXT (paste if text field, upload PDF if file field) ==
{cl_display}

== APPLICANT PROFILE ==
{profile_summary}

== YOUR MISSION ==
Submit a complete, accurate application. Use the profile and resume as source data -- adapt to fit each form's format.

If something unexpected happens and these instructions don't cover it, figure it out yourself. You are autonomous. Navigate pages, read content, try buttons, explore the site. The goal is always the same: submit the application. Do whatever it takes to reach that goal.

{hard_rules}

== NEVER DO THESE (immediate RESULT:FAILED if encountered) ==
- NEVER grant camera, microphone, screen sharing, or location permissions. If a site requests them -> RESULT:FAILED:unsafe_permissions
- NEVER do video/audio verification, selfie capture, ID photo upload, or biometric anything -> RESULT:FAILED:unsafe_verification
- NEVER set up a freelancing profile (Mercor, Toptal, Upwork, Fiverr, Turing, etc.). These are contractor marketplaces, not job applications -> RESULT:FAILED:not_a_job_application
- NEVER agree to hourly/contract rates, availability calendars, or "set your rate" flows. You are applying for FULL-TIME salaried positions only.
- NEVER install browser extensions, download executables, or run assessment software.
- NEVER enter payment info, bank details, or SSN/SIN.
- NEVER click "Allow" on any browser permission popup. Always deny/block.
- If the site is NOT a job application form (it's a profile builder, skills marketplace, talent network signup, coding assessment platform) -> RESULT:FAILED:not_a_job_application

{location_check}

{salary_section}

{screening_section}

== STEP-BY-STEP ==
1. browser_navigate to the job URL.
2. browser_snapshot to read the page. Then run CAPTCHA DETECT (see CAPTCHA section). If a CAPTCHA is found, solve it before continuing.
3. LOCATION CHECK. Read the page for location info. If not eligible, output RESULT and stop.
4. Find and click the Apply button. If email-only (page says "email resume to X"):
   - send_email with subject "Application for {job['title']} -- {display_name}", body = 2-3 sentence pitch + contact info, attach resume PDF: ["{pdf_path}"]
   - Output RESULT:APPLIED. Done.
   After clicking Apply: browser_snapshot. Run CAPTCHA DETECT -- many sites trigger CAPTCHAs right after the Apply click. If found, solve before continuing.
5. Login wall?
   5a. FIRST: look for "Continue with Google", "Sign in with Google", LinkedIn, or Indeed. Prefer Google, then LinkedIn, then Indeed. Use it when that provider already has an authenticated session in this Chrome profile, and choose the account matching {personal['email']} when an account picker appears.
   5b. Check for popups with browser_tabs action "list". If the SSO login opened a new tab/window, switch to it with browser_tabs action "select", complete only the existing-session account selection/consent, then return to the application tab. Never enter the Google/LinkedIn/Indeed password or attempt 2-step verification.
   5c. Only when no supported existing-session SSO option is offered: use the employer's own login form with {personal['email']} / {personal.get('password', '')}.
   5d. After clicking Login/Sign-in: run CAPTCHA DETECT. Login pages frequently have invisible CAPTCHAs that silently block form submissions. If found, solve it then retry login.
   5e. Sign in failed and no supported SSO option exists? Try sign up with the same email and password.
   5e2. Sign-up failed specifically because the account already exists (page says
        "email already registered," "account already exists," "try signing in
        instead," etc.)? Click "Forgot password?" / "Reset password" on the
        login form. Submit {personal['email']}. Wait briefly, then use
        the authorized email-verification procedure below to find the reset message,
        open its reset link, and set the new password to
        {personal.get('password', '')} (the same saved job-site password) in
        both password fields. Submit, then sign in with that same email and
        password. Attempt this once per job; if it fails, do not retry --
        fall through to the final login-failed step below.
   5f. Need email verification or a reset link? Follow this procedure immediately:
       {email_verification_steps}
   5g. After login, run browser_tabs action "list" again. Switch back to the application tab if needed.
   5h. If Continue with Google lands on a Google page asking for a password or 2-step verification (not merely an account picker), the persistent base Google session is stale. Do not enter anything. Output RESULT:FAILED:google_session_expired and stop immediately.
   5i. All failed? Output RESULT:FAILED:login_issue. Do not loop.
6. Upload resume. ALWAYS upload fresh -- delete any existing resume first, then browser_file_upload with the PDF path above. This is the tailored resume for THIS job. Non-negotiable.
7. Upload cover letter if there's a field for it. Text field -> paste the cover letter text. File upload -> use the cover letter PDF path.
8. Check ALL pre-filled fields. ATS systems parse your resume and auto-fill -- it's often WRONG.
   - "Current Job Title" or "Most Recent Title" -> use the title from the TAILORED RESUME summary, NOT whatever the parser guessed.
   - Compare every other field to the APPLICANT PROFILE. Fix mismatches. Fill empty fields.
9. Answer screening questions using the rules above.
10. REQUIRED-FIELD GATE. Before EVERY Next, Continue, Review, Save and Continue,
    or final Submit action, run the required-field gate defined below. Do not advance
    while any visible required control is empty, invalid, or still red-highlighted.
11. {submit_instruction}
12. After submit: browser_snapshot. Run CAPTCHA DETECT -- submit buttons often trigger invisible CAPTCHAs. If found, solve it (the form will auto-submit once the token clears, or you may need to click Submit again). Then check for new tabs (browser_tabs action: "list"). Switch to newest and snapshot it. Output RESULT:APPLIED only when the browser visibly shows a receipt such as "application submitted", "application received", "thank you for applying", or LinkedIn's Applied status. Narration, a completed-looking form, or clicking Submit is not proof. If validation remains, the same form is still visible, or no positive receipt appears, output RESULT:FAILED:submission_unverified. Never claim success to bypass a validation error.
13. Output your result.

== RESULT CODES (output EXACTLY one) ==
RESULT:APPLIED -- submitted successfully
RESULT:EXPIRED -- job closed or no longer accepting applications
RESULT:CAPTCHA -- blocked by unsolvable captcha
RESULT:LOGIN_ISSUE -- could not sign in or create account
RESULT:FAILED:not_eligible_location -- onsite outside acceptable area, no remote option
RESULT:FAILED:not_eligible_work_auth -- requires unauthorized work location
RESULT:FAILED:reason -- any other failure (brief reason)

== BROWSER EFFICIENCY ==
- browser_snapshot ONCE per page to understand it. Then use browser_take_screenshot to check results (10x less memory).
- Only snapshot again when you need element refs to click/fill.
- Multi-page forms (Workday, Taleo, iCIMS): snapshot each new page, fill all currently visible stable fields, run the REQUIRED-FIELD GATE, then click Next/Continue ONCE. Wait for navigation or postback and take a fresh snapshot. Repeat until final review.
- Fill all currently visible stable fields in one browser_fill_form call when possible. Do not include hidden, disabled, conditionally absent, or detached controls merely to force one batch.
- Keep your thinking SHORT. Don't repeat page structure back.
- CAPTCHA AWARENESS: After any navigation, Apply/Submit/Login click, or when a page feels stuck -- run CAPTCHA DETECT (see CAPTCHA section). Invisible CAPTCHAs (Turnstile, reCAPTCHA v3) show NO visual widget but block form submissions silently. The detect script finds them even when invisible.

== REQUIRED-FIELD GATE (RUN BEFORE EVERY PAGE-ADVANCE ACTION) ==
1. Take a fresh browser_snapshot. Inspect every visible, enabled control. Treat a control as required when it has an asterisk, red label/border, `required`, `aria-required`, "Required"/"This field is required" text, or belongs to a required radio/checkbox group.
2. Verify required text inputs/textareas are non-blank; native/custom selects do not still show Select/Choose/empty; required radio groups have one answer; required checkboxes are checked when they represent agreement rather than an optional preference; and required uploads show the expected filename. For custom React comboboxes (especially Greenhouse), a visible selected label such as Yes/No is authoritative evidence that the control is filled even when stale `aria-invalid=true` or an old "This field is required" message remains in the snapshot. Do not clear or reselect a visibly selected value just to remove stale validation markup.
3. Fill all known answers from the APPLICANT PROFILE. Never invent a hard fact. If a required hard-fact answer is absent from the profile, stop with RESULT:FAILED:missing_profile_answer and name the field briefly.
4. After answering any controller that can reveal conditional questions (work authorization, sponsorship, citizenship, location, veteran/disability, prior employment, criminal history, referral source, or Yes/No screening), wait briefly and take a fresh snapshot. Fill newly revealed required fields, then repeat this gate until no new required controls appear. Limit this stabilization loop to two cycles.
5. Only when the page has no visible empty/invalid required controls may you click Next/Continue/Review/Save and Continue/Submit once.
6. After that click, wait and take a fresh snapshot. If the same page remains with validation errors or red-highlighted controls, take a screenshot too. First distinguish a genuinely empty/rejected control from stale error markup beside a control that visibly retains a valid selection. Repair only genuinely empty/rejected controls, then rerun this gate and click once more. Never keep clearing or reselecting an already-filled Greenhouse combobox; submit once to make the page revalidate it. Do not defer intermediate-page errors until final submit and do not spend three blind retries.
6a. Build a short signature from the current page/step plus the visible validation messages and rejected field labels. If the same signature remains after that one repair-and-retry cycle, stop immediately with RESULT:FAILED:validation_loop. Do not delete resume-derived experience or education entries, do not keep scrolling/reselecting the same values, and never output RESULT:APPLIED while a validation error remains.
7. Embedded form/EEO iframe present? Inspect its accessible snapshot content and complete its visible required controls with the same gate. Never assume an iframe section is optional. If a cross-origin frame's required controls are not exposed to the browser tools, stop with RESULT:FAILED:embedded_form_unavailable instead of advancing with missing data.

== FORM TRICKS ==
- Popup/new window opened? browser_tabs action "list" to see all tabs. browser_tabs action "select" with the tab index to switch. ALWAYS check for new tabs after clicking login/apply/sign-in buttons.
- "Upload your resume" pre-fill page (Workday, Lever, etc.): This is NOT the application form yet. Click "Select file" or the upload area, then browser_file_upload with the resume PDF path. Wait for parsing to finish. Then click Next/Continue to reach the actual form.
- File upload not working? Try: (1) browser_click the upload button/area, (2) browser_file_upload with the path. If still failing, look for a hidden file input or a "Select file" link and click that first.
- Dropdown won't fill? browser_click to open it, then browser_click the option.
- Checkbox won't check via fill_form? Use browser_click on it instead. Snapshot to verify.
- Phone field with country prefix: just type digits {phone_digits}
- Date fields: {datetime.now().strftime('%m/%d/%Y')}
- Validation errors after ANY Next/Continue/Review/Submit action? Take BOTH snapshot AND screenshot. Snapshot shows text errors, screenshot shows red-highlighted fields. Fix all immediately, rerun the REQUIRED-FIELD GATE, then retry once.
- Honeypot fields (hidden, "leave blank"): skip them.
- Format-sensitive fields: read the placeholder text, match it exactly.

{captcha_section}

== WHEN TO GIVE UP ==
- Same page after 2 required-field correction cycles with no progress -> RESULT:FAILED:stuck
- Job is closed/expired/page says "no longer accepting" -> RESULT:EXPIRED
- Page is broken/500 error/blank -> RESULT:FAILED:page_error
Stop immediately. Output your RESULT code. Do not loop."""

    return prompt
