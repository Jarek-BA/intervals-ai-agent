import datetime
import json
import os
from google import genai
import requests

# --- 1. KONFIGURACE Z PROSTŘEDÍ ---
ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "i510990")
INTERVALS_API_KEY = os.environ.get("INTERVALS_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

AUTH = ("API_KEY", INTERVALS_API_KEY)


# --- 2. SYNCHRONIZACE TRÉNINKOVÉHO PLÁNU DO INTERVALS.ICU ---
def sync_plan_from_file(filename="plan.json"):
    if not os.path.exists(filename):
        print(f"⚠️ Soubor {filename} nenalezen, přeskakuji synchronizaci.")
        return

    with open(filename, "r", encoding="utf-8") as f:
        planned_items = json.load(f)

    if not planned_items:
        print("⚠️ Soubor plan.json je prázdný.")
        return

    all_dates = [item["date"] for item in planned_items]
    min_date = min(all_dates)
    max_date = max(all_dates)

    url_events = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/events"
    params = {"oldest": min_date, "newest": max_date}

    res = requests.get(url_events, auth=AUTH, params=params)
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
            res_post = requests.post(url_events, auth=AUTH, json=payload)
            if res_post.status_code in (200, 201):
                print(f"✅ Nahraný nový trénink na {item_date}: {item['name']}")
            else:
                print(
                    f"❌ Chyba při nahrávání {item_date}: "
                    f"{res_post.status_code} {res_post.text}"
                )
        else:
            print(
                f"ℹ️ Trénink na {item_date} ({item['name']}) "
                f"už v kalendáři existuje."
            )


# --- 3. ZÍSKÁNÍ DAT Z INTERVALS.ICU ---
def get_intervals_data():
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=10)
    end_date = today + datetime.timedelta(days=2)

    wellness_url = (
        f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/"
        f"wellness/{today.isoformat()}"
    )
    res_wellness = requests.get(wellness_url, auth=AUTH)
    wellness_data = (
        res_wellness.json() if res_wellness.status_code == 200 else {}
    )

    events_url = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/events"
    params = {"oldest": start_date.isoformat(), "newest": end_date.isoformat()}
    res_events = requests.get(events_url, auth=AUTH, params=params)
    events_data = res_events.json() if res_events.status_code == 200 else []

    return wellness_data, events_data


# --- 4. GENEROVÁNÍ DOPORUČENÍ POMOCÍ GEMINI AI ---
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


# --- HLAVNÍ SPUŠTĚNÍ ---
if __name__ == "__main__":
    print("1. Synchronizuji plánované tréninky do Intervals.icu...")
    sync_plan_from_file("plan.json")

    print("2. Stahuji data o únavě a aktivitách...")
    wellness_info, events_info = get_intervals_data()

    print("3. Generuji AI doporučení...")
    report = generate_ai_recommendation(wellness_info, events_info)

    print("\n--- AI REPORT ---")
    print(report)
