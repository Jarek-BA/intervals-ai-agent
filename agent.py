import datetime
import json
import logging
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

import markdown
import requests
from google import genai

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

# Datum začátku 1. týdne podle Bena Parkese (Pondělí 1. týdne)
PLAN_START_DATE = os.environ.get("PLAN_START_DATE", "2026-07-13")

BASIC_AUTH_TUPLE = ("API_KEY", INTERVALS_API_KEY)


def get_request_auth(
) -> Tuple[Optional[Dict[str, str]], Optional[Tuple[str, str]]]:
    intervals_key = os.environ.get("INTERVALS_API_KEY")
    if os.environ.get("INTERVALS_USE_BASIC_AUTH"):
        return None, ("API_KEY", intervals_key)
    if intervals_key:
        return {"Authorization": f"Bearer {intervals_key}"}, None
    return None, None


def validate_env_vars() -> None:
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

    if not (EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECEIVER):
        logger.warning(
            "Email config incomplete; email sending may fail. "
            "Ensure EMAIL_SENDER, EMAIL_PASSWORD, "
            "and EMAIL_RECEIVER are set."
        )


# --- HTTP helpers ---
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


# --- HELPER PRO BEN PARKES PLAN ---
def load_ben_parkes_plan(filepath: str = "ben_parkes_plan.json") -> dict:
    if not os.path.exists(filepath):
        logger.warning("Soubor %s nenalezen.", filepath)
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Chyba při čtení %s: %s", filepath, e)
        return {}


def get_current_plan_context(plan_data: dict, start_date_str: str) -> dict:
    if not plan_data or "weeks" not in plan_data:
        return {}
    try:
        start_date = datetime.datetime.strptime(
            start_date_str, "%Y-%m-%d"
        ).date()
    except ValueError:
        logger.error("Neplatný formát PLAN_START_DATE: %s", start_date_str)
        return {}

    today = datetime.date.today()
    days_diff = (today - start_date).days
    current_week_num = (days_diff // 7) + 1
    total_weeks = plan_data.get("plan_metadata", {}).get(
        "duration_weeks", 15
    )

    week_info = next(
        (w for w in plan_data["weeks"] if w["week"] == current_week_num), None
    )

    return {
        "current_week_num": current_week_num,
        "total_weeks": total_weeks,
        "week_details": week_info,
        "pace_chart": plan_data.get("plan_metadata", {}).get(
            "pace_chart_km", {}
        ),
    }


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

    all_dates = [it.get("date") for it in planned_items if it.get("date")]
    if not all_dates:
        logger.info("No valid dates found in plan, skipping.")
        return

    min_date = min(all_dates)
    max_date = max(all_dates)

    url_events = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/events"
    params = {"oldest": min_date, "newest": max_date}

    res = safe_get(url_events, params=params)
    existing_events = safe_json(res) or []

    existing_keys = set()
    if isinstance(existing_events, list):
        for e in existing_events:
            if isinstance(e, dict):
                start_date = e.get("start_date_local", "")[:10]
                name = e.get("name", "")
                if start_date and name:
                    existing_keys.add(f"{start_date}_{name}")

    for item in planned_items:
        item_date = item.get("date")
        name = item.get("name")
        if not item_date or not name:
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
            if res_post and res_post.status_code in (200, 201):
                logger.info("Uploaded new workout on %s: %s", item_date, name)
        else:
            logger.info("Workout on %s (%s) already exists.", item_date, name)


# --- 3. GET INTERVALS DATA ---
def get_intervals_data() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    today = datetime.date.today()
    start_14d = today - datetime.timedelta(days=14)
    start_30d = today - datetime.timedelta(days=30)
    # Načteme události v kalendáři na 5 týdnů do budoucna z Intervals.icu
    end_date = today + datetime.timedelta(days=35)

    # 1. Wellness za posledních 30 dní
    wellness_url = (
        f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/wellness"
    )
    params_wellness = {
        "oldest": start_30d.isoformat(),
        "newest": today.isoformat(),
    }
    res_wellness = safe_get(wellness_url, params=params_wellness)
    wellness_history = safe_json(res_wellness) or []

    # 2. Kalendář z Intervals.icu (-14 dní historie až +35 dní budoucí plán)
    events_url = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/events"
    params_events = {
        "oldest": start_14d.isoformat(),
        "newest": end_date.isoformat(),
    }
    res_events = safe_get(events_url, params=params_events)
    events_data = safe_json(res_events) or []

    # 3. Odtrénované aktivity za posledních 14 dní
    activities_url = (
        f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/activities"
    )
    params_activities = {
        "oldest": start_14d.isoformat(),
        "newest": today.isoformat(),
    }
    res_act = safe_get(activities_url, params=params_activities)
    activities_list = safe_json(res_act) or []

    enriched_events = []

    # Připojíme detailní odtrénované aktivity (s úseky/laps)
    if isinstance(activities_list, list):
        for act in activities_list:
            if not isinstance(act, dict):
                continue
            act_id = act.get("id")
            if act_id:
                single_act_url = (
                    f"https://intervals.icu/api/v1/activity/{act_id}"
                    "?intervals=true"
                )
                res_single = safe_get(single_act_url)
                single_data = safe_json(res_single)
                if isinstance(single_data, dict):
                    single_data["is_completed_activity"] = True
                    enriched_events.append(single_data)

    # Připojíme naplánované tréninky z kalendáře Intervals.icu
    if isinstance(events_data, list):
        for ev in events_data:
            if isinstance(ev, dict) and ev.get("type") != "Activity":
                enriched_events.append(ev)

    return wellness_history, enriched_events


def _shorten_events_for_prompt(events: List[Dict[str, Any]]) -> str:
    formatted_lines = []
    for e in events:
        if not isinstance(e, dict):
            continue

        start_date = (
            e.get("start_date_local") or e.get("start_date") or ""
        )[:10]
        name = e.get("name", "Bez názvu")
        category = e.get("category") or e.get("type", "")

        is_completed = e.get("is_completed_activity", False) or (
            e.get("type") == "Activity"
        )
        status_str = (
            "✅ REÁLNĚ ODTRÉNOVÁNO" if is_completed else "📅 POUZE NAPLÁNOVÁNO"
        )

        line = f"• [{start_date}] {name} ({category}) - {status_str}"

        if is_completed:
            dist = (e.get("distance") or 0) / 1000.0
            moving_time = (e.get("moving_time") or 0) // 60
            avg_hr = e.get("average_heartrate", "N/A")
            max_hr = e.get("max_heartrate", "N/A")
            avg_temp = e.get("average_temp", "N/A")

            line += (
                f"\n   -> Celkem: {dist:.2f} km | Čas: {moving_time} min | "
                f"Avg HR: {avg_hr} bpm | Max HR: {max_hr} bpm | "
                f"Teplota tréninku: {avg_temp} °C"
            )

            laps = (
                e.get("icu_intervals")
                or e.get("icu_lap_outlines")
                or e.get("laps")
                or []
            )

            if isinstance(laps, list) and len(laps) > 0:
                line += "\n   -> DETAILNÍ ÚSEKY / KOLA (LAPS):"
                for idx, lap in enumerate(laps, 1):
                    if not isinstance(lap, dict):
                        continue

                    raw_dist = lap.get("distance")
                    lap_dist = (
                        (float(raw_dist) / 1000.0)
                        if raw_dist is not None
                        else 0.0
                    )

                    raw_moving = (
                        lap.get("moving_time") or lap.get("elapsed_time")
                    )
                    lap_moving = (
                        float(raw_moving) if raw_moving is not None else 0.0
                    )

                    if lap_dist > 0 and lap_moving > 0:
                        pace_seconds = lap_moving / lap_dist
                        mins = int(pace_seconds // 60)
                        secs = int(pace_seconds % 60)
                        pace_str = f"{mins}:{secs:02d} min/km"
                    else:
                        pace_str = "N/A"

                    gap = lap.get("gap")
                    gap_str = f"{gap}" if gap else pace_str

                    alt = lap.get(
                        "total_elevation_gain", lap.get("altitude_gain", 0)
                    )
                    cadence = lap.get("average_cadence", "N/A")
                    l_hr = lap.get("average_heartrate", "N/A")
                    label = (
                        lap.get("label") or lap.get("type") or f"Úsek {idx}"
                    )

                    line += (
                        f"\n      * {label} ({lap_dist:.2f} km): "
                        f"GAP/Tempo: {gap_str} | "
                        f"HR: {l_hr} bpm | Kadence: {cadence} spm | "
                        f"Převýšení: +{alt}m"
                    )
            else:
                line += "\n   -> Detailní úseky nebyly nalezeny."

        formatted_lines.append(line)

    return "\n".join(formatted_lines)


def generate_ai_recommendation(
    wellness_history: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY)
    today = datetime.date.today()

    events_for_prompt = _shorten_events_for_prompt(events)

    today_str = today.isoformat()
    today_wellness = next(
        (w for w in wellness_history if w.get("id") == today_str), {}
    )

    prompt = f"""\
# DENNÍ BĚŽECKÝ & FYZIOLOGICKÝ REPORT
Jsi elitní běžecký trenér a sportovní fyziolog. Tento komplexní report
generuješ na základě dat z kalendáře Intervals.icu.

**DNEŠNÍ DATUM:** {today.isoformat()} ({today.strftime('%A')})

**DEFINICE CÍLOVÝCH TEMP / ZÓN (Cíl Maraton < 3:00):**
- Recovery: > 5:25 min/km
- Easy / Z2: 4:52 – 5:24 min/km
- Marathon Pace (MP): 4:12 – 4:18 min/km
- Threshold / Tempo: 3:59 – 4:06 min/km
- VO2max / Intervals: 3:44 – 3:53 min/km

**AKTUÁLNÍ DNEŠNÍ WELLNESS:**
- Form (TSB): {today_wellness.get('form', 'N/A')}
- Fitness (CTL): {today_wellness.get('ctl', 'N/A')}
- Fatigue (ATL): {today_wellness.get('atl', 'N/A')}
- Dnešní Klidový tep (RHR): {today_wellness.get('restingHR', 'N/A')}

- HISTORIE WELLNESS (POSLEDNÍCH 14 DNÍ):
{json.dumps(wellness_history[-14:], ensure_ascii=False)}

**KALENDÁŘ INTERVALS.ICU (ODTRÉNOVANÁ HISTORIE + NAPLÁNOVANÁ BUDOUCNOST):**
DŮLEŽITÉ: Veškerý plán tréninků vychází výhradně ze záznamů níže v kalendáři
Intervals.icu. Ignoruj jakékoliv dřívější šablony.
{events_for_prompt}

---

**POKYNY PRO GENEROVÁNÍ REPORTU:**

1. **REKAPITULACE DNEŠNÍHO DNE ({today.isoformat()}):**
   - Vyhodnoť dnešní odběhanou aktivitu (odpovídá-li naplánovanému workoutu
     v Intervals.icu, tempa, TF, laps).
   - Pokud byl na dnes naplánován trénink v Intervals.icu a chybí, vyhodnoť
     ho jako vynechaný.

2. **MAKRO ANALÝZA A KONTROLA PLÁNU:**
   - Vyhodnoť plnění aktuálního týdne podle naplánovaných událostí
     v Intervals.icu a trend zátěže (CTL/TSB).

3. **VERDIKT A PŘÍPRAVA NA ZÍTŘEK:**
   - Na základě dnešního výkonu a plánovaných nadcházejících tréninků z
     kalendáře dej doporučení na zítřek.

Formátuj výstup v čistém Markdownu. Nepoužívej LaTeX syntaxi ($ ani ~).
"""

    try:
        interaction = client.interactions.create(
            model="gemini-3.5-flash", input=prompt
        )
        text = interaction.output_text
    except Exception as e:
        logger.exception("Gemini API request failed: %s", e)
        raise RuntimeError(f"Gemini API error: {e}")

    return text if isinstance(text, str) else str(text)


# --- 5. SEND EMAIL ---
def send_email(subject: str, markdown_content: str) -> bool:
    html_body = markdown.markdown(
        markdown_content, extensions=["tables", "fenced_code"]
    )

    full_html = (
        "<html>\n"
        '  <body style="font-family: Arial, sans-serif; line-height: 1.6; '
        'color: #333;">\n'
        '    <div style="max-width: 650px; margin: 0 auto; padding: 20px; '
        'border: 1px solid #e0e0e0; border-radius: 8px;">\n'
        f"        {html_body}\n"
        "    </div>\n"
        "  </body>\n"
        "</html>"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER

    part_text = MIMEText(markdown_content, "plain", "utf-8")
    part_html = MIMEText(full_html, "html", "utf-8")

    msg.attach(part_text)
    msg.attach(part_html)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        return True
    except Exception as e:
        logger.error("Failed to send email: %s", e)
        return False


# --- MAIN RUN ---
def main() -> None:
    validate_env_vars()

    logger.info(
        "1. Fetching wellness, activities and planned events "
        "from Intervals.icu..."
    )
    wellness_history, events = get_intervals_data()

    logger.info(
        "2. Generating AI recommendation with Macro & Micro "
        "analysis..."
    )
    try:
        report = generate_ai_recommendation(wellness_history, events)
    except Exception as e:
        logger.error("AI recommendation generation failed: %s", e)
        return

    if not report or not str(report).strip():
        logger.warning("Empty report received; skipping email send.")
        return

    today_str = datetime.date.today().strftime("%d. %m. %Y")
    subject = f"🏃‍♂️ Tréninkový report [{today_str}]"

    logger.info("3. Sending email...")
    success = send_email(subject, report)
    if success:
        logger.info("All done: report sent.")
    else:
        logger.error("Report was not sent.")


if __name__ == "__main__":
    main()
