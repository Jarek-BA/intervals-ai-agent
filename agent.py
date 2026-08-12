import os
import datetime
import smtplib
from email.mime.text import MIMEText
import requests
from google import genai

# --- 1. KONFIGURACE Z PROSTŘEDÍ (ENV VARIABLES) ---
ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "i510990")
INTERVALS_API_KEY = os.environ.get("INTERVALS_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Email konfigurace
EMAIL_SENDER = os.environ.get("EMAIL_SENDER")      # Tůj Gmail / SMTP email
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")  # Heslo aplikace pro Gmail
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")  # Kam má e-mail dojít

# --- 2. ZÍSKÁNÍ DAT Z INTERVALS.ICU ---
def get_intervals_data():
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    
    auth = ("API_KEY", INTERVALS_API_KEY)
    
    # a) Stáhnutí Wellness data (CTL, ATL, TSB / Form)
    wellness_url = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/wellness/{today.isoformat()}"
    res_wellness = requests.get(wellness_url, auth=auth)
    wellness_data = res_wellness.json() if res_wellness.status_code == 200 else {}
    
    # b) Stáhnutí Eventů/Tréninků (Včera a Dnes)
    events_url = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/events"
    params = {
        "oldest": yesterday.isoformat(),
        "newest": today.isoformat()
    }
    res_events = requests.get(events_url, auth=auth, params=params)
    events_data = res_events.json() if res_events.status_code == 200 else []
    
    return wellness_data, events_data

# --- 3. GENEROVÁNÍ DOPORUČENÍ POMOCÍ GEMINI AI ---
def generate_ai_recommendation(wellness, events):
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    Jsi můj osobní vytrvalostní tréninkový AI kouč.
    
    **Můj kontext:**
    - Hlavní cíle: Maraton Luzern (cíl pod 3h), Uster Triatlon (1.5 km plavání / 10 km běh), 220 km Bodensee na kole.
    - Metodika: Držím se tréninkové filosofie Bena Parkse (Easy runs Z2, MP intervaly, Long runy).
    
    **Aktuální data z mého účtu Intervals.icu (k dnešnímu dni):**
    - Form / TSB (Čerstvost/Únava): {wellness.get('form', 'N/A')}
    - Fitness / CTL: {wellness.get('ctl', 'N/A')}
    - Fatigue / ATL: {wellness.get('atl', 'N/A')}
    - Klidový tep (RHR): {wellness.get('restingHR', 'N/A')}
    
    **Aktivity a tréninky (Včera a Dnes):**
    {events}
    
    **Tůj úkol:**
    1. Stručně zhodnoť včerejší trénink a můj aktuální stav únavy (TSB/CTL/ATL).
    2. Prohlédni si, co mám naplánované na dnešek.
    3. Dej mi jasné, konkrétní doporučení pro dnešní den (zda trénink odtrénovat podle plánu, upravit tempa/intenzitu, nebo zařadit volno/regeneraci).
    4. Buď stručný, věcný, motivující a piš v češtině.
    """
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text

# --- 4. ODESLÁNÍ E-MAILU ---
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
    wellness, events = get_intervals_data()
    report = generate_ai_recommendation(wellness, events)
    
    today_str = datetime.date.today().strftime("%d. %m. %Y")
    send_email(f"🏃‍♂️ Tréninkový report [{today_str}]", report)
    print("Report byl úspěšně odeslán!")
