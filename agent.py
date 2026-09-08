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
WELLNESS_MISSING_VALUES = (None, "")

# --- 1. CONFIG FROM ENV ---
ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "i510990")
INTERVALS_API_KEY = os.environ.get("INTERVALS_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")

TRAINING_GOAL = os.environ.get(
    "TRAINING_GOAL", "Run a marathon in under 3:00"
)


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


def build_wellness_context(
    wellness_history: List[Dict[str, Any]],
    evaluation_date: Optional[datetime.date] = None,
) -> Dict[str, Any]:
    """Select each wellness metric independently and record its source date."""
    evaluation_date = evaluation_date or datetime.date.today()
    evaluation_date_str = evaluation_date.isoformat()
    previous_date = evaluation_date - datetime.timedelta(days=1)
    previous_date_str = previous_date.isoformat()

    records = {
        record.get("id"): record
        for record in wellness_history
        if isinstance(record, dict) and record.get("id")
    }
    current_record = records.get(evaluation_date_str, {})
    previous_record = records.get(previous_date_str, {})

    merged: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    for key in set(current_record) | set(previous_record):
        current_value = current_record.get(key)
        if current_value not in WELLNESS_MISSING_VALUES:
            merged[key] = current_value
            sources[key] = evaluation_date_str
        elif previous_record.get(key) not in WELLNESS_MISSING_VALUES:
            merged[key] = previous_record[key]
            sources[key] = previous_date_str

    merged["id"] = evaluation_date_str
    return {
        "evaluation_date": evaluation_date_str,
        "previous_date": previous_date_str,
        "data": merged,
        "sources": sources,
    }

# --- GET INTERVALS DATA ---
def get_intervals_data() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    today = datetime.date.today()
    start_14d = today - datetime.timedelta(days=14)
    start_30d = today - datetime.timedelta(days=30)
    # Load calendar events for the previous 14 days and next 35 days.
    end_date = today + datetime.timedelta(days=35)

    # 1. Wellness for the last 30 days.
    wellness_url = (
        f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/wellness"
    )
    params_wellness = {
        "oldest": start_30d.isoformat(),
        "newest": today.isoformat(),
    }
    res_wellness = safe_get(wellness_url, params=params_wellness)
    wellness_history = safe_json(res_wellness) or []

    # 2. Intervals.icu calendar (14 days of history and 35 days ahead).
    events_url = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/events"
    params_events = {
        "oldest": start_14d.isoformat(),
        "newest": end_date.isoformat(),
    }
    res_events = safe_get(events_url, params=params_events)
    events_data = safe_json(res_events) or []

    # 3. Completed activities for the last 14 days.
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

    # Add detailed completed activities, including laps.
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

    # Add planned workouts from the Intervals.icu calendar.
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
        name = e.get("name", "Unnamed workout")
        category = e.get("category") or e.get("type", "")

        is_completed = e.get("is_completed_activity", False) or (
            e.get("type") == "Activity"
        )
        status_str = (
            "✅ COMPLETED" if is_completed else "📅 PLANNED ONLY"
        )

        line = f"• [{start_date}] {name} ({category}) - {status_str}"

        if is_completed:
            dist = (e.get("distance") or 0) / 1000.0
            moving_time = (e.get("moving_time") or 0) // 60
            avg_hr = e.get("average_heartrate", "N/A")
            max_hr = e.get("max_heartrate", "N/A")
            avg_temp = e.get("average_temp", "N/A")

            line += (
                f"\n   -> Total: {dist:.2f} km | Time: {moving_time} min | "
                f"Avg HR: {avg_hr} bpm | Max HR: {max_hr} bpm | "
                f"Workout temperature: {avg_temp} °C"
            )

            laps = (
                e.get("icu_intervals")
                or e.get("icu_lap_outlines")
                or e.get("laps")
                or []
            )

            if isinstance(laps, list) and len(laps) > 0:
                line += "\n   -> DETAILED INTERVALS / LAPS:"
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
                        lap.get("label") or lap.get("type") or f"Interval {idx}"
                    )

                    line += (
                        f"\n      * {label} ({lap_dist:.2f} km): "
                        f"GAP/Tempo: {gap_str} | "
                        f"HR: {l_hr} bpm | Kadence: {cadence} spm | "
                        f"Elevation gain: +{alt}m"
                    )
            else:
                line += "\n   -> No detailed intervals were found."

        formatted_lines.append(line)

    return "\n".join(formatted_lines)


def generate_ai_recommendation(
    wellness_history: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY)
    today = datetime.date.today()
    execution_time = datetime.datetime.now().astimezone()

    events_for_prompt = _shorten_events_for_prompt(events)

    wellness_context = build_wellness_context(
        wellness_history, evaluation_date=today
    )
    wellness = wellness_context["data"]
    wellness_sources = wellness_context["sources"]
    source_lines = "\n".join(
        f"- {metric}: {source_date}"
        for metric, source_date in sorted(wellness_sources.items())
        if metric != "id"
    ) or "- No wellness metrics available"

    prompt = f"""\
# DAILY RUNNING AND PHYSIOLOGY REPORT
You are an elite running coach and sports physiologist. Generate this report
from the Intervals.icu calendar, wellness data, and completed activities.

**EXECUTION TIME:** {execution_time.isoformat()}
**EVALUATION DATE:** {today.isoformat()} ({today.strftime('%A')})
**TRAINING GOAL:** {TRAINING_GOAL}

**TARGET PACES / ZONES:**
- Recovery: > 5:25 min/km
- Easy / Z2: 4:52 – 5:24 min/km
- Marathon Pace (MP): 4:12 – 4:18 min/km
- Threshold / Tempo: 3:59 – 4:06 min/km
- VO2max / Intervals: 3:44 – 3:53 min/km

**WELLNESS VALUES USED FOR THIS REPORT:**
{json.dumps(wellness, ensure_ascii=False, sort_keys=True)}

**SOURCE DATE FOR EACH WELLNESS METRIC:**
{source_lines}

If a metric comes from the previous date, explicitly say so. Sleep and
overnight metrics usually describe the night before the source date. Steps
and resting heart rate may be incomplete early in the day. A previous-day
resting heart rate can be a post-training response to the previous workout,
especially when that workout happened in the morning; do not treat it as a
pre-workout measurement for today's training.

**WELLNESS HISTORY (LAST 14 DAYS):**
{json.dumps(wellness_history[-14:], ensure_ascii=False)}

**INTERVALS.ICU CALENDAR (COMPLETED HISTORY + PLANNED FUTURE):**
IMPORTANT: Derive the training plan only from the calendar records below.
Ignore any older templates or assumptions.
{events_for_prompt}

**REPORT REQUIREMENTS:**
1. Summarize today's completed activity, comparing it with today's planned
    workout, pace, heart rate, and laps. If a planned workout is missing,
    identify it as missed. If no activity is available yet, say that clearly.
2. Analyze this week's plan completion and training-load trend using CTL,
    ATL, and TSB where available.
3. Assess whether the athlete is moving toward the stated goal. Cite the
    evidence: recent pace, volume, workouts, load trend, and consistency.
4. Recommend specific training adjustments that improve the probability of
    achieving the goal without ignoring recovery or injury risk.
5. If the goal appears unrealistic based on the collected data, provide a
    data-based performance forecast. Explain the method and assumptions,
    distinguish an estimate from a measured result, include uncertainty or a
    plausible range, and say which additional data would improve it. Do not
    invent data or promise a precise outcome.
6. Give practical advice for tomorrow, accounting for the fact that this
    report may run in the morning before today's complete wellness data exist.

Use clean Markdown. Do not use LaTeX syntax ($ or ~).
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
    subject = f"🏃‍♂️ Training report [{today_str}]"

    logger.info("3. Sending email...")
    success = send_email(subject, report)
    if success:
        logger.info("All done: report sent.")
    else:
        logger.error("Report was not sent.")


if __name__ == "__main__":
    main()
