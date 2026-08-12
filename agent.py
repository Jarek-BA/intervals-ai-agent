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

    # 1. Wellness
    wellness_url = (
        f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/"
        f"wellness/{today.isoformat()}"
    )
    res_wellness = safe_get(wellness_url)
    wellness_data = safe_json(res_wellness) or {}

    # 2. Kalendář (Events / Planned Workouts)
    events_url = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/events"
    params = {"oldest": start_date.isoformat(), "newest": end_date.isoformat()}
    res_events = safe_get(events_url, params=params)
    events_data = safe_json(res_events) or []

    # 3. Odtrénované aktivity (Reálná GPS/HR data)
    activities_url = (
        f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/activities"
    )
    res_act = safe_get(activities_url, params=params)
    activities_list = safe_json(res_act) or []

    enriched_events = []

    # Přidáme plně stažené odtrénované aktivity s úseky (Laps)
    if isinstance(activities_list, list):
        for act in activities_list:
            if not isinstance(act, dict):
                continue
            act_id = act.get("id")
            if act_id:
                # Parametr ?intervals=true přinutí API vrátit detailní Laps/Intervaly
                single_act_url = (
                    f"https://intervals.icu/api/v1/activity/{act_id}"
                    "?intervals=true"
                )
                res_single = safe_get(single_act_url)
                single_data = safe_json(res_single)
                if isinstance(single_data, dict):
                    single_data["is_completed_activity"] = True
                    enriched_events.append(single_data)

    # Přidáme plánované eventy, které ještě nemají odpovídající aktivitu
    if isinstance(events_data, list):
        for ev in events_data:
            if isinstance(ev, dict) and ev.get("type") != "Activity":
                enriched_events.append(ev)

    return wellness_data, enriched_events


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
            "✅ REÁLNĚ ODTRÉNOVÁNO (MÁŠ REÁLNÁ DATA)"
            if is_completed
            else "📅 POUZE NAPLÁNOVÁNO (NEPROBĚHLO)"
        )

        line = f"• [{start_date}] {name} ({category}) - {status_str}"

        if is_completed:
            dist = (e.get("distance") or 0) / 1000.0
            moving_time = (e.get("moving_time") or 0) // 60
            avg_hr = e.get("average_heartrate", "N/A")
            max_hr = e.get("max_heartrate", "N/A")
            avg_watts = e.get("icu_average_watts", "N/A")

            line += (
                f"\n   -> Celkem: {dist:.2f} km | Čas: {moving_time} min | "
                f"Avg HR: {avg_hr} bpm | Max HR: {max_hr} bpm | "
                f"Avg Power: {avg_watts} W"
            )

            # Extraktujeme Laps/Úseky vrácené z Intervals.icu
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

                    # Bezpečné ošetření None prvků
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
                        float(raw_moving)
                        if raw_moving is not None
                        else 0.0
                    )

                    if lap_dist > 0 and lap_moving > 0:
                        pace_seconds = lap_moving / lap_dist
                        mins = int(pace_seconds // 60)
                        secs = int(pace_seconds % 60)
                        pace_str = f"{mins}:{secs:02d} min/km"
                    else:
                        pace_str = "N/A"

                    # Hodnota GAP z Intervals.icu
                    gap = lap.get("gap")
                    gap_str = f"{gap}" if gap else pace_str

                    alt = lap.get(
                        "total_elevation_gain", lap.get("altitude_gain", 0)
                    )
                    cadence = lap.get("average_cadence", "N/A")
                    l_hr = lap.get("average_heartrate", "N/A")
                    l_max_hr = lap.get("max_heartrate", "N/A")
                    label = (
                        lap.get("label")
                        or lap.get("type")
                        or f"Úsek {idx}"
                    )

                    line += (
                        f"\n      * {label} ({lap_dist:.2f} km): "
                        f"GAP/Tempo: {gap_str} | "
                        f"HR: {l_hr} (Max {l_max_hr}) bpm | "
                        f"Kadence: {cadence} spm | "
                        f"Převýšení: +{alt}m"
                    )
            else:
                line += "\n   -> Detailní úseky (Laps) nebyly v datech nalezeny."

        formatted_lines.append(line)

    return "\n".join(formatted_lines)


def generate_ai_recommendation(
    wellness: Dict[str, Any], events: List[Dict[str, Any]]
) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY)

    today = datetime.date.today()
    events_for_prompt = _shorten_events_for_prompt(events)

    prompt = f"""
Jsi expert na vytrvalostní běh a osobní AI kouč. Tvým úkolem je provádět
přísnou, objektivní a datově přesnou analýzu tréninků z Intervals.icu.

**DNEŠNÍ DATUM:** {today.isoformat()} ({today.strftime('%A')})

**MŮJ PROFIL A HLAVNÍ CÍL:**
- Hlavní závod: Maraton Luzern (25. 10. 2026) – cíl SUB 3:00.
- Doplňkové akce: Uster Triatlon (23. 8. 2026) &
  Bodensee Radmarathon (12. 9. 2026).

**DEFINICE TRÉNINKOVÝCH ZÓN A TEMPA (CÍLOVÉ PROSINKY):**
- **Recovery / Regeneration:** > 5:25 min/km (včetně
  intervalových pauz 6:00–5:25 min/km)
- **Easy / Z2:** 4:52 – 5:24 min/km
- **Marathon Pace (MP):** 4:12 – 4:18 min/km
- **Threshold / Tempo:** 3:59 – 4:06 min/km
- **VO2max / Intervals:** 3:44 – 3:53 min/km

**AKTUÁLNÍ WELLNESS DNEŠKA ({today.isoformat()}):**
- Form (TSB): {wellness.get('form', 'N/A')}
- Fitness (CTL): {wellness.get('ctl', 'N/A')}
- Fatigue (ATL): {wellness.get('atl', 'N/A')}
- Klidový tep (RHR): {wellness.get('restingHR', 'N/A')}

**HISTORIE A PLÁN Z INTERVALS.ICU:**
{events_for_prompt}

---

**OBECNÁ ANALYTICKÁ PRAVIDLA (APLIKUJ NA VŠECHNY TRÉNINKY):**

1. **Časový rámec a stav aktivit:**
   - DNEŠEK je {today.isoformat()}.
   - Položky označené jako "POUZE NAPLÁNOVÁNO" ještě neproběhly a
     nikdy je nehodnoť jako odtrénované.
   - Hodnoť výhradně položky "RÉALNĚ ODTRÉNOVÁNO".

2. **Matematicky přesné vyhodnocení tempa úseků (Laps):**
   - Vždy porovnávej předepsaný typ úseku (Warmup, Easy, MP,
     Interval, Recovery) s odpovídající cílovou zónou definovanou výše.
   - Pro posouzení rychlosti/tempa **používej výhradně hodnotu GAP
     (Grade Adjusted Pace)**, nikoliv běžný Pace.
   - Pokles či zrychlení GAP mimo předepsané rozmezí dané zóny vyhodnoť
     jako **nedodržení tempa** s uvedením přesné odchylky v sekundách
     na kilometr. Buď nekompromisní a data nezaokrouhluj ani neomlouvej.

3. **Biomechanická a kardiovaskulární odezva:**
   - **Heart Rate Drift:** Sleduj vývoj tepovky (Avg HR / Max HR)
     napříč jednotlivými úseky o stejné intenzitě.
   - **Převýšení:** Vyhodnoť profil (Elev/Gradient) pro vysvětlení dynamiky běhu.
   - **Kadence a Efektivita:** Sleduj stabilitu kadence (spm) a poměr výkonu
     k tepu (Efficiency Factor / Power-to-HR) v průběhu tréninku jako
     ukazatele svalové či kardiální únavy.

---

**STRUKTURA VÝSTUPU (HTML/Markdown):**

## 🏃‍♂️ Denní AI Koučink – {today.isoformat()}

### 📊 1. Stav těla a Únava (Wellness)
- Zhodnocení aktuálního TSB, CTL, ATL a RHR v kontextu přípravy.

### 🎯 2. Analýza odtrénovaných aktivit (Laps & Biometrics)
- Detailní rozbor odtrénovaných běhů po jednotlivých úsecích (Laps).
- Přesné srovnání reálného **GAP** vůči příslušné definované zóně
  (např. MP, Easy, VO2max).
- Hodnocení HR driftu, kadence a běžecké efektivity.

### 📋 3. Verdikt a doporučení pro DNEŠNÍ DEN ({today.isoformat()})
- Jasné a konkrétní doporučení pro dnešek na základě aktuální únavy (TSB)
a odtrénované zátěže.

Nikdy nepoužívej LaTeX syntaxi (žádné znaky $ nebo ~). Všechna čísla,
jednotky a vzorce piš jako běžný text (např. "4:15 min/km" nebo "CTL - ATL").
Piš věcně, pracuj s přesnými čísly z dat a piš v češtině.
"""

    try:
        interaction = client.interactions.create(
            model="gemini-3.5-flash", input=prompt
        )
        text = interaction.output_text
    except Exception as e:
        logger.exception("Gemini API request failed: %s", e)
        raise RuntimeError(f"Gemini API error: {e}")

    if not isinstance(text, str):
        try:
            text = json.dumps(text)
        except Exception:
            text = str(text)

    return text


# --- 5. SEND EMAIL ---
def send_email(subject: str, markdown_content: str) -> None:
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

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())


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
