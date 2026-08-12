# intervals-ai-agent

This small agent synchronizes a planned training schedule (plan.json) into Intervals.icu, fetches recent wellness and activity data, asks Gemini (via google-genai) for a short training recommendation, and emails it.

Usage
-----

Required environment variables:

- INTERVALS_API_KEY — API key for intervals.icu (recommended to use as a Bearer token).
- GEMINI_API_KEY — API key for Gemini / Google generative AI SDK.
- EMAIL_SENDER — email address used to send the report (optional during development).
- EMAIL_PASSWORD — password / app password for the sending email account.
- EMAIL_RECEIVER — recipient email address for the report.

Optional:

- INTERVALS_ATHLETE_ID — athlete id used for Intervals.icu API. Defaults to "i510990".
- INTERVALS_USE_BASIC_AUTH — when set (to any value), the agent will use HTTP basic auth (requests' auth=(user,pass)) instead of the Authorization: Bearer header.

Run locally
-----------

1. Install dependencies:

    pip install -r requirements.txt

2. Set required environment variables.

3. Run the agent:

    python agent.py

Notes
-----
- The agent adds basic runtime validation and request timeouts. Check logs for details.
