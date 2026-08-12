import os
import json
import datetime
import smtplib
from email.mime.text import MIMEText
import requests
import google.generativeai as genai

# --- 1. KONFIGURACE Z PROSTŘEDÍ ---
ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "i510990")
INTERVALS_API_KEY = os.environ.get("INTERVALS_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")

AUTH = ("API_KEY", INTERVALS_API_KEY)

# --- 2. SYNCHRONIZACE TRÉNINKOVÉHO PLÁNU DO INTERVALS.ICU ---
def sync_plan_from_file(filename="plan.json"):
    if not os.path.exists(filename):
        print(f"⚠️ Soubor {filename} nenalezen, přeskakuji synchronizaci plánu.")
        return

    with open(filename, "r", encoding="utf-8") as f:
        planned_items = json.load(f)

    if not planned_items:
        print("⚠️ Soubor plan.json je prázdný.")
        return

    # Najdeme nejstarší a nejnovější datum přímo v plan.json
    all_dates = [item["date"] for item in planned_items]
    min_date = min(all_dates)
    max_date = max(all_dates)

    url_events = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/events"
    params = {
        "oldest": min_date,
        "newest": max_date
    }
    
    res = requests.get(url_events, auth=AUTH, params=params)
    existing_events = res.json() if res.status_code == 200 else []
    
    # Existující události ve tvaru "YYYY-MM-DD_Název"
    existing_keys = {f"{e.get('start_date_local', '')[:10]}_{e.get('name')}" for e in existing_events}

    for item in planned_items:
        item_date = item["date"]
        event_key = f"{item_date}_{item['name']}"
        
        if event_key not in existing_keys:
            payload = {
                "start_date_local": f"{item_date}T07:00:00",
                "type": item["type"],
                "name": item["name"],
                "description": item["description"]
            }
            res_post = requests.post(url_events, auth=AUTH, json=payload)
            if res_post.status_code in (200, 201):
                print(f"✅ Nahraný nový trénink na {item_date}: {item['name']}")
            else:
                print(f"❌ Chyba při nahrávání {item_date}: {res_post.status_code} {res_post.text}")
        else:
            print(f"ℹ️ Trénink na {item_date} ({item['name']}) už v kalendáři existuje.")

# --- 3. ZÍSKÁNÍ DAT Z INTERVALS.ICU ---
def get_intervals_data():
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=10)
    end_date = today + datetime.timedelta(days=2)
    
    # Wellness data
    wellness_url = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/wellness/{today.isoformat()}"
    res_wellness = requests.get(wellness_url, auth=AUTH)
    wellness_data = res_wellness.json() if res_wellness.status_code == 200 else {}
    
    # Události a aktivity
    events_url = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/events"
    params = {"oldest": start_date.isoformat(), "newest": end_date.isoformat()}
    res_events = requests.get(events_url, auth=AUTH, params=params)
    events_data = res_events.json() if res_events.status_code == 200 else []
    
    return wellness_data, events_data

# --- 4. GENEROVÁNÍ DOPORUČENÍ POMOCÍ GEMINI AI ---
def generate_ai_recommendation(wellness, events):
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
    
    prompt = f"""
    Jsi můj osobní vytrvalostní tréninkový AI kouč.
    
    **Můj kontext:**
    - Hlavní cíl: Maraton Luzern (25. 10. 2026) – cíl SUB 3:00 (Maratonské tempo MP: 4:12–4:18 min/km).
    - Doplňkové akce:
      * Uster Triatlon (23. 8. 2026 – 1.5 km OWS pod 30 min / 10 km RUN pod 40 min / tempo 4:00 min/km)
      * Bodensee Radmarathon (12. 9. 2026 – 220 km na kole v Z2 jako objem na Ironmana)
    - Tréninková filozofie: Ben Parkes Level 4 (vysoký objem, Easy běhy v Z2: 4:52–5:24 min/km, MP intervaly, Long runy).
    - Priorita: Běh má 100% prioritu. Kolo a plavání jsou doplňkový cross-training.
    
    **Aktuální data z mého účtu Intervals.icu (k dnešnímu dni):**
    - Form / TSB (Čerstvost/Únava): {wellness.get('form', 'N/A')}
    - Fitness / CTL: {wellness.get('ctl', 'N/A')}
    - Fatigue / ATL: {wellness.get('atl', 'N/A')}
    - Klidový tep (RHR): {wellness.get('restingHR', 'N/A')}
    
    **Historie tréninků a naplánované tréninky (Posledních 10 dní + Plán na dnešek a zítřek):**
    {json.dumps(events, indent=2, ensure_ascii=False)}
    
    **Tůj úkol:**
    1. Porovnej naplánované tréninky s reálně odtrénovanými aktivitami za poslední týden.
    2. Zhodnoť stav mé únavy (TSB/CTL/ATL) v kontextu blížícho se Uster Triatlonu a maratonského cyklu.
    3. Dej mi jasné, konkrétní a strukturované doporučení pro DNEŠNÍ DEN.
    4. Buď stručný, věcný, motivující a piš v češtině.
    """
    
    response = model.generate_content(prompt)
    return response.text

# --- 5. ODESLÁNÍ E-MAILU ---
def send_email(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER
    
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, [EMAIL_RECEIVER], msg.as_string())

# --- HLAVNÍ SPUŠTĚNÍ ---
if __name__ == "__main__":
    print("1. Synchronizuji plánované tréninky do Intervals.icu...")
    sync_plan_from_file("plan.json")
    
    print("2. Stahuji data o únavě a aktivitách...")
    wellness, events = get_intervals_data()
    
    print("3. Generuji AI doporučení...")
    report = generate_ai_recommendation(wellness, events)
    
    today_str = datetime.date.today().strftime("%d. %m. %Y")
    print("4. Odesílám e-mail...")
    send_email(f"🏃‍♂️ Tréninkový report [{today_str}]", report)
    print("🚀 Vše hotovo! Report byl úspěšně odeslán.")
