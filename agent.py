# Full hardened agent.py (replace the file contents)
import datetime
import json
import logging
import os
import smtplib
import sys
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

from google import genai
import requests

# Basic config
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10  # seconds
MAX_PROMPT_CHARS = 6000

# --- 1. CONFIG FROM ENV ---
ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "i510990")
INTERVALS_API_KEY = os.environ.get("INTERVALS_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")

# Keep the old tuple for basic auth fallback (user, pass) if requested
BASIC_AUTH_TUPLE = ("API_KEY", INTERVALS_API_KEY)


# Helper: decide headers vs requests auth
def get_request_auth() -> Tuple[Optional[Dict[str, str]], Optional[Tuple[str, str]]]:
    """Return (headers_dict or None, auth_tuple or None).

    If INTERVALS_USE_BASIC_AUTH is set in env, return auth tuple,
    otherwise return Authorization header.

    NOTE: Read the env var at call time so tests and runtime
    monkeypatching work correctly.
    """
    intervals_key = os.environ.get("INTERVALS_API_KEY")
    if os.environ.get("INTERVALS_USE_BASIC_AUTH"):
        # Build the basic-auth tuple at call time so it uses the current env value
        return None, ("API_KEY", intervals_key)
    if intervals_key:
        return {"Authorization": f"Bearer {intervals_key}"}, None
    return None, None


def validate_env_vars() -> None:
    """Validate required environment variables and exit
    with a helpful message if missing.
    """
    missing = []
    if not INTERVALS_API_KEY:
        missing.append("INTERVALS_API_KEY")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if missing:
        logger.error(
            "Missing required environment variables: %s", ", ".join(missing)
        )
        sys.exit(2)

    # Email vars are optional for development, warn if missing
    if not (EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECEIVER):
        logger.warning(
            "Email config incomplete; email sending may fail. "
            "Ensure EMAIL_SENDER, EMAIL_PASSWORD, "
            "and EMAIL_RECEIVER are set."
        )


# --- HTTP helpers with timeouts and safe JSON decoding ---
def safe_get(
    url: str, params: Optional[Dict[str, Any]] = None
) -> Optional[requests.Response]:
    headers, auth = get_request_auth()
    try:
        if headers:
            return requests.get(
                url, headers=headers, params=params, timeout=REQUEST_TIMEOUT
            )
        return requests.get(
            url, auth=auth, params=params, timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as e:
        logger.error("Network error during GET %s: %s", url, e)
        return None


def safe_post(
    url: str, json_payload: Dict[str, Any]
) -> Optional[requests.Response]:
    headers, auth = get_request_auth()
    try:
        if headers:
            return requests.post(
                url,
                headers=headers,
                json=json_payload,
                timeout=REQUEST_TIMEOUT,
            )
        return requests.post(
            url, auth=auth, json=json_payload, timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as e:
        logger.error("Network error during POST %s: %s", url, e)
        return None


def safe_json(response: Optional[requests.Response]) -> Any:
    if response is None:
        return None
    try:
        return response.json()
    except ValueError:
        logger.warning(
            "Response from %s returned non-JSON body",
            getattr(response, "url", "<unknown>"),
        )
        return None


# --- 2. SYNC PLAN TO INTERVALS.ICU ---
def sync_plan_from_file(filename: str = "plan.json") -> None:
    if not os.path.exists(filename):
        logger.info("File %s not found, skipping plan sync.", filename)
        return

    with open(filename, "r", encoding="utf-8") as f:
        try:
            planned_items = json.load(f)
        except ValueError as e:
            logger.error("Failed to parse %s: %s", filename, e)
            return

    if not planned_items:
        logger.info("File %s is empty.", filename)
        return

    # Validate and collect dates safely
    all_dates = []
    for it in planned_items:
        d = it.get("date")
        if not d:
            logger.warning("Skipping plan entry without date: %s", it)
            continue
        all_dates.append(d)
    if not all_dates:
        logger.info("No valid dates found in plan, skipping.")
        return

    min_date = min(all_dates)
    max_date = max(all_dates)

    url_events = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/events"
    params = {"oldest": min_date, "newest": max_date}

    res = safe_get(url_events, params=params)
    existing_events = safe_json(res) or []

    existing_keys = {
        f"{e.get('start_date_local', '')[:10]}_{e.get('name')}"
        for e in existing_events
    }

    for item in planned_items:
        item_date = item.get("date")
        name = item.get("name")
        if not item_date or not name:
            logger.warning(
                "Skipping invalid plan entry (missing date/name): %s", item
            )
            continue

        event_key = f"{item_date}_{name}"
        if event_key not in existing_keys:
            workout_text = item.get("description", "")
            payload = {
                "start_date_local": f"{item_date}T07:00:00",
                "type": item.get("type", "run"),
                "category": "WORKOUT",
                "name": name,
                "description": workout_text,
                "workout_doc": {"description": workout_text},
            }
            res_post = safe_post(url_events, json_payload=payload)
            if res_post is None:
                logger.error(
                    "Failed to POST event for %s: network error", item_date
                )
                continue
            if res_post.status_code in (200, 201):
                logger.info("Uploaded new workout on %s: %s", item_date, name)
            else:
                logger.error(
                    "Error uploading %s: %s %s",
                    item_date,
                    res_post.status_code,
                    getattr(res_post, "text", ""),
                )
        else:
            logger.info("Workout on %s (%s) already exists.", item_date, name)


# --- 3. GET INTERVALS DATA ---
def get_intervals_data() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=10)
    end_date = today + datetime.timedelta(days=2)

    wellness_url = (
            f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/"
            f"wellness/{today.isoformat()}"
    )
    res_wellness = safe_get(wellness_url)
    wellness_data = safe_json(res_wellness) or {}

    events_url = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/events"
    params = {"oldest": start_date.isoformat(), "newest": end_date.isoformat()}
    res_events = safe_get(events_url, params=params)
    events_data = safe_json(res_events) or []

    return wellness_data, events_data


# --- 4. GENERATE AI RECOMMENDATION ---
def _shorten_events_for_prompt(
    events: List[Dict[str, Any]], max_chars: int = MAX_PROMPT_CHARS
) -> str:
    dumped = json.dumps(events, indent=2, ensure_ascii=False)
    if len(dumped) <= max_chars:
        return dumped
    # If too long, take most recent entries and state we truncated
    truncated = json.dumps(events[-30:], indent=2, ensure_ascii=False)
    return f"(Truncated to last {min(30, len(events))} events)\n{truncated}"


def generate_ai_recommendation(
    wellness: Dict[str, Any], events: List[Dict[str, Any]]
) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY)

    events_for_prompt = _shorten_events_for_prompt(events)

    prompt = f"""
Jsi můj osobní vytrvalostní tréninkový AI kouč.

**Můj kontext:**
- Hlavní cíl: Maraton Luzern (25. 10. 2026) – cíl SUB 3:00 (MP: 4:12–4:18 min/km).
- Doplňkové akce:
  * Uster Triatlon (23. 8. 2026 – 1.5 km OWS <30m / 10 km RUN <40m / tempo 4:00 min/km)
  * Bodensee Radmarathon (12. 9. 2026 – 220 km na kole v Z2 jako objem na Ironmana)
- Tréninková filozofie: Ben Parkes Level 4 (vysoký objem, Easy Z2: 4:52–5:24 min/km).
- Priorita: Běh má 100% prioritu. Kolo a plavání jsou doplňkový cross-training.

**Aktuální data z mého účtu Intervals.icu (k dnešnímu dni):**
- Form / TSB (Čerstvost/Únava): {wellness.get('form', 'N/A')}
- Fitness / CTL: {wellness.get('ctl', 'N/A')}
- Fatigue / ATL: {wellness.get('atl', 'N/A')}
- Klidový tep (RHR): {wellness.get('restingHR', 'N/A')}

**Historie tréninků a naplánované tréninky (Posledních 10 dní + Dnes a Zítřek):**
{events_for_prompt}

**Tůj úkol:**
1. Porovnej naplánované tréninky s reálně odtrénovanými aktivitami za poslední týden.
2. Zhodnoť stav mé únavy (TSB/CTL/ATL) v kontextu Uster Triatlonu a maratonského cyklu.
3. Dej mi jasné, konkrétní a strukturované doporučení pro DNEŠNÍ DEN.
4. Buď stručný, věcný, motivující a piš v češtině.
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
    except Exception as e:
        logger.exception("Gemini API request failed: %s", e)
        raise RuntimeError(f"Gemini API error: {e}")

    # Safe extraction of text
    text = None
    for attr in ("text", "output", "content", "result"):
        text = getattr(response, attr, None)
        if text:
            break
    if text is None:
        # Fallback to str() representation
        text = str(response)

    if not isinstance(text, str):
        try:
            text = json.dumps(text)
        except Exception:
            text = str(text)

    return text


# --- 5. SEND EMAIL ---
def send_email(subject: str, body: str) -> bool:
    if not (EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECEIVER):
        logger.error("Email not sent: missing email configuration.")
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, [EMAIL_RECEIVER], msg.as_string())
        logger.info("Email sent to %s", EMAIL_RECEIVER)
        return True
    except Exception as e:
        logger.exception("Failed to send email: %s", e)
        return False


# --- MAIN RUN ---
def main() -> None:
    validate_env_vars()

    logger.info("1. Syncing planned workouts to Intervals.icu...")
    sync_plan_from_file("plan.json")

    logger.info("2. Fetching wellness and activity data...")
    wellness, events = get_intervals_data()

    logger.info("3. Generating AI recommendation...")
    try:
        report = generate_ai_recommendation(wellness, events)
    except Exception as e:
        logger.error("AI recommendation generation failed: %s", e)
        return

    if not report or not str(report).strip():
        logger.warning("Empty report received; skipping email send.")
        return

    today_str = datetime.date.today().strftime("%d. %m. %Y")
    subject = f"🏃‍♂️ Tréninkový report [{today_str}]"

    logger.info("4. Sending email...")
    success = send_email(subject, report)
    if success:
        logger.info("All done: report sent.")
    else:
        logger.error("Report was not sent.")


if __name__ == "__main__":
    main()
