import datetime
import json
import logging
import os
import smtplib
import sys
from email.mime.text import MIMEText
from typing import Dict, Optional, Tuple

from google import genai
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# --- 1. KONTROLA PROSTŘEDÍ (ENVIRONMENT VARIABLES) ---
def validate_env_vars() -> None:
    """Zkontroluje, zda jsou nastaveny všechny povinné proměnné prostředí."""
    required_vars = ["INTERVALS_API_KEY", "GEMINI_API_KEY"]
    missing_vars = [var for var in required_vars if not os.environ.get(var)]

    if missing_vars:
        logger.error(
            f"❌ Chybí povinné proměnné prostředí: {', '.join(missing_vars)}"
        )
        sys.exit(1)


# --- 2. HELPER PRO AUTORIZACI INTERVALS.ICU ---
def get_request_auth() -> Tuple[Optional[Dict[str, str]], Optional[Tuple[str, str]]]:
    """Return (headers_dict or None, auth_tuple or None).

    If INTERVALS_USE_BASIC_AUTH is set in env, return auth tuple,
    otherwise return Authorization header.
    NOTE: Read the env var at call time so tests and runtime
    monkeypatching work correctly.
    """
    intervals_key = os.environ.get("INTERVALS_API_KEY")
    if os.environ.get("INTERVALS_USE_BASIC_AUTH"):
        return None, ("API_KEY", intervals_key)
    if intervals_key:
        return {"Authorization": f"Bearer {intervals_key}"}, None
    return None, None


# --- 3. KONFIGURACE Z PROSTŘEDÍ ---
ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "i510990")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")


# --- 4. SYNCHRONIZACE TRÉNINKOVÉHO PLÁNU DO INTERVALS.ICU ---
def sync_plan_from_file(filename="plan.json"):
    if not os.path.exists(filename):
        logger.warning(
            f"⚠️ Soubor {filename} nenalezen, přeskakuji synchronizaci."
        )
        return

    with open(filename, "r", encoding="utf-8") as f:
        planned_items = json.load(f)

    if not planned_items:
        logger.warning("⚠️ Soubor plan.json je prázdný.")
        return

    all_dates = [item["date"] for item in planned_items]
    min_date = min(all_dates)
    max_date = max(all_dates)

    url_events = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/events"
    params = {"oldest": min_date, "newest": max_date}

    headers, auth = get_request_auth()
    res = requests.get(url_events, headers=headers, auth=auth, params=params)
    existing_events = res.json() if res.status_code == 200 else []

    existing_keys = {
        f"{e.get('start_date_local', '')[:10]}_{e.get('name')}"
        for e in existing_events
    }

    for item in planned_items:
        item_date = item["date"]
        event_key = f"{item_date}_{item['name']}"

        if event_key not in existing_keys:
            workout_text = item["description"]
            payload = {
                "start_date_local": f"{item_date}T07:00:00",
                "type": item["type"],
                "category": "WORKOUT",
                "name": item["name"],
                "description": workout_text,
                "workout_doc": {"description": workout_text},
            }
            res_post = requests.post(
                url_events, headers=headers, auth=auth, json=payload
            )
            if res_post.status_code in (200, 201):
                logger.info(
                    f"✅ Nahraný nový trénink na {item_date}: {item['name']}"
                )
            else:
                logger.error(
                    f"❌ Chyba při nahrávání {item_date}: "
                    f"{res_post.status_code} {res_post.text}"
                )
        else:
            logger.info(
                f"ℹ️ Trénink na {item_date} ({item['name']}) "
                f"už v kalendáři existuje."
            )


# --- 5. ZÍSKÁNÍ DAT Z INTERVALS.ICU ---
def get_intervals_data():
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=10)
    end_date = today + datetime.timedelta(days=2)

    headers, auth = get_request_auth()

    wellness_url = (
        f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/"
        f"wellness/{today.isoformat()}"
    )
    res_wellness = requests.get(wellness_url, headers=headers, auth=auth)
    wellness_data = (
        res_wellness.json() if res_wellness.status_code == 200 else {}
    )

    events_url = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/events"
    params = {"oldest": start_date.isoformat(), "newest": end_date.isoformat()}
    res_events = requests.get(
        events_url, headers=headers, auth=auth, params=params
    )
    events_data = res_events.json() if res_events.status_code == 200 else []

    return wellness_data, events_data


# --- 6. GENEROVÁNÍ DOPORUČENÍ POMOCÍ GEMINI AI ---
def generate_ai_recommendation(wellness, events):
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
    Jsi můj osobní vytrvalostní tréninkový AI kouč.

    **Můj kontext:**
    - Hauptziel: Maraton Luzern (25. 10. 2026) - SUB 3:00 (MP: 4:12-4:18/km).
    - Uster Triatlon (23. 8. 2026 - 1.5km OWS <30m / 10km RUN <40m tempo 4:00).
    - Bodensee Radmarathon (12. 9. 2026 - 220 km kolo Z2).
    - Filozofie: Ben Parkes Level 4 (Easy Z2: 4:52-5:24/km, MP intervaly).
    - Priorita: Běh má 100% prioritu. Kolo a plavání jsou doplňkové.

    **Data z Intervals.icu:**
    - Form / TSB: {wellness.get('form', 'N/A')}
    - Fitness / CTL: {wellness.get('ctl', 'N/A')}
    - Fatigue / ATL: {wellness.get('atl', 'N/A')}
    - Klidový tep (RHR): {wellness.get('restingHR', 'N/A')}

    **Historie a plán:**
    {json.dumps(events, indent=2, ensure_ascii=False)}

    **Úkol:**
    1. Porovnej plánované vs odtrénované aktivity za poslední týden.
    2. Zhodnoť únavu (TSB/CTL/ATL) vzhledem k Uster Triatlonu.
    3. Dej konkrétní doporučení pro DNEŠNÍ DEN.
    4. Buď stručný, věcný a piš česky.
    """

    response = client.models.generate_content(
        model="gemini-2.5-flash", contents=prompt
    )
    return response.text


# --- 7. ODESLÁNÍ EMAILU ---
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
        logger.info("✉️ E-mail úspěšně odeslán!")
        return True
    except Exception as e:
        logger.error(f"❌ Chyba při odesílání e-mailu: {e}")
        return False


# --- HLAVNÍ SPUŠTĚNÍ ---
if __name__ == "__main__":
    logger.info("0. Kontroluji proměnné prostředí...")
    validate_env_vars()

    logger.info("1. Synchronizuji plánované tréninky do Intervals.icu...")
    sync_plan_from_file("plan.json")

    logger.info("2. Stahuji data o únavě a aktivitách...")
    wellness_info, events_info = get_intervals_data()

    logger.info("3. Generuji AI doporučení...")
    report = generate_ai_recommendation(wellness_info, events_info)

    logger.info("4. Odesílám e-mail s reportem...")
    today_str = datetime.date.today().strftime("%d. %m. %Y")
    send_email(f"🏃‍♂️ Tréninkový report [{today_str}]", report)

    logger.info("🚀 Vše hotovo!")
