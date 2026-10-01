# intervals-ai-agent

This small agent fetches recent wellness, activity, and planned workout data
from Intervals.icu, asks Gemini (via google-genai) for a training
recommendation, and emails it.

Usage
-----

Required environment variables:

- INTERVALS_API_KEY — API key for intervals.icu (recommended to use as a Bearer token).
- GEMINI_API_KEY — API key for Gemini / Google generative AI SDK.
- EMAIL_SENDER — email address used to send the report (optional during development).
- EMAIL_PASSWORD — password / app password for the sending email account.
- EMAIL_RECEIVER — recipient email address for the report.

Optional:

- INTERVALS_ATHLETE_ID — athlete id used for Intervals.icu API.
- INTERVALS_USE_BASIC_AUTH — when set (to any value), the agent will use HTTP basic auth (requests' auth=(user,pass)) instead of the Authorization: Bearer header.
- TRAINING_GOAL — the goal assessed by the report. Leave empty if no goal is configured.

The GitHub Actions workflow runs every morning at 01:23 UTC. Wellness is
selected per metric: a value from the evaluation date is preferred, and a
missing value falls back to the previous date. The report includes the source
date for every selected metric, so previous-day resting heart rate is not
mistaken for a pre-workout measurement. Steps and other end-of-day metrics may
still be incomplete in a morning report.

Run locally
-----------

1. Install dependencies:

    pip install -r requirements.txt

2. Create a local environment file:

    cp .env.example .env

3. Fill in the credentials in `.env`. The file is ignored by Git.

4. Run the agent:

    python agent.py

Notes
-----
- The agent adds basic runtime validation and request timeouts. Check logs for details.
