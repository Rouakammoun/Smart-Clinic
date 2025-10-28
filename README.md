# Smart-Clinic

A modern, AI-powered medical clinic management system with integrated appointment scheduling and multilingual support.

## 🌟 Features

- **AI-Powered Triage Assistant**: Bilingual support for English and French
- **Smart Appointment Scheduling**: Automated booking system integrated with Google Calendar
- **Doctor Management**: Comprehensive doctor profiles and availability management
- **Real-time Chat Interface**: User-friendly web interface for patient interactions
- **Multi-specialty Support**: Handles various medical specialties with smart routing

## 🛠️ Technology Stack

- **Backend**: FastAPI (Python)
- **AI Models**:
  - DeepSeek-V3 for text processing
- **Calendar Integration**: Google Calendar API
- **Frontend**: HTML/Static files
- **Data Storage**: JSON-based data management

## 📋 Prerequisites

- Python 3.x
- Hugging Face API Token
- Google Calendar API credentials
- Environment variables configuration

## 🔧 Installation

1. Clone the repository:

```bash
git clone [repository-url]
cd Smart-Clinic
```

2. Install required packages:

```bash
pip install -r requirements.txt
```

3. Set up environment variables in `.env`:

````env
# Smart-Clinic

A lightweight AI-assisted clinic assistant that provides triage, doctor lookup, availability checking, and appointment booking backed by Google Calendar. The assistant supports English and French.

## Key features

- AI-powered triage and doctor routing (via Hugging Face model)
- Google Calendar-backed appointment booking with optional Google Meet creation
- Email notifications (patient and doctor) via configurable SMTP (best-effort)
- Doctor directory driven by `data/doctors.json`
- Web chat UI served from `static/chat.html`

## Quick start (development)

1. Clone and install dependencies

```powershell
git clone <repo-url>
cd Smart-Clinic
pip install -r requirements.txt
````

2. Provide credentials and environment variables

- Create a `.env` file (example below) or set environment vars in your environment.

Example `.env` (fill values):

```env
HUGGINGFACE_API_TOKEN=your_hf_token
HF_MODEL=deepseek-ai/DeepSeek-V3
GOOGLE_SERVICE_ACCOUNT_FILE=service-account.json
CLINIC_TIMEZONE=Asia/Karachi
HOSPITAL_DATA_PATH=data/doctors.json
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@example.com
SMTP_PASS=app_password_or_secret
SMTP_FROM=Unity Care Clinic <no-reply@yourdomain.com>
MIN_LEAD_MINUTES=30
```

Notes:

- `GOOGLE_SERVICE_ACCOUNT_FILE` (or `GOOGLE_APPLICATION_CREDENTIALS`) must point to a service account JSON with Google Calendar API access.
- `HOSPITAL_DATA_PATH` defaults to `data/doctors.json`.
- `MIN_LEAD_MINUTES` (optional) enforces a minimum lead time for booking/rescheduling.

3. Run the server

You can either run the FastAPI app directly with Uvicorn or use the provided entrypoint in `api.py`:

```powershell
# option A (recommended during development)
uvicorn api:app --reload

# option B (api.py main also runs uvicorn)
python api.py
```

Open the UI at: http://localhost:8000/ui/chat.html

## Important environment variables and configuration

- HUGGINGFACE_API_TOKEN — required for LLM calls
- HF_MODEL, HF_VISION_MODEL — model repo names (defaults in code)
- GOOGLE_SERVICE_ACCOUNT_FILE / GOOGLE_APPLICATION_CREDENTIALS — service account JSON for calendar operations
- SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM — SMTP configuration for sending appointment emails
- HOSPITAL_DATA_PATH — JSON file that contains doctors and condition_map
- CLINIC_TIMEZONE — timezone used for scheduling (default Asia/Karachi)
- MIN_LEAD_MINUTES — minimum advance minutes required to book/reschedule (default 0)

## How booking and notifications work (behavior summary)

- Availability is computed from each doctor's `weekly_schedule` in `data/doctors.json` combined with the doctor's Google Calendar free/busy data.
- Booking an appointment (`appointment_book_tool` exposed via `/book`) will:
  - Validate requested slot is within clinic hours and not conflicting with calendar busy times
  - Insert an event into the doctor's Google Calendar (optionally creating a Google Meet link when `visit_mode=online` and `create_meet=true`)
  - Optionally include event attendees and ask Google to send invites when `send_invitations=true`
  - Build a human-readable confirmation message and an .ics calendar attachment
  - Attempt to send confirmation emails (best-effort) via SMTP to:
    - Patient: the `patient_email` provided in the booking request
    - Doctor: the email address found in the doctor's entry from `data/doctors.json` (if present)

Response from the booking tool includes `smtp_status` ("sent" or "failed") and `smtp_error` (if any).

## HTTP endpoints (via `api.py`)

The FastAPI wrapper exposes simple JSON endpoints that call the internal MCP tools. Key endpoints:

- POST /chat — conversational entrypoint for the AI assistant (body: { user, session_id?, language? })
- POST /chat/stream — streaming variant
- POST /doctor-lookup — body: { condition, visit_mode? }
- POST /availability — body: { doctor_id, date, slot_minutes?, end_date? }
- POST /book — body: booking fields: { doctor_id, start, end, patient_name, patient_email, ... }
- POST /appointments — list upcoming appointments for a patient (body: { patient_email, doctor_id?, window_days? })
- POST /cancel — cancel an appointment (body: { doctor_id, event_id, patient_email, notify_attendees? })
- POST /reschedule — reschedule appointment (body: { doctor_id, event_id, new_start, new_end, patient_email })
- POST /rehydrate — rebuild session state from history
- POST /explain-chart-image — upload an image to get chart explanation (vision model)
- GET /health — simple health check

Use the chat endpoints to interact with the assistant; the assistant uses the MCP tools (in `server.py`) to perform lookups and bookings.

## Data: `data/doctors.json`

- Doctors are defined with fields such as `doctor_id`, `name`, `specialization`, `fees`, `weekly_schedule`, `calendar_id`, `location`, `email`.
- `weekly_schedule` is a mapping of day abbreviations (e.g., `Mon`, `Tue`, `Wed`, `Thu`, `Fri`, `Sat`, `Sun` or `%a` form) to lists of windows in the format `HH:MM-HH:MM`.

Example fragment (see full file in `data/doctors.json`):

```json
{
  "doctor_id": "dr_eric",
  "name": "Dr. Eric",
  "specialization": "Ophthalmology",
  "fees": { "online_pkr": 2500, "inperson_pkr": 3500 },
  "weekly_schedule": { "Mon": ["10:00-12:00", "15:00-17:00"] },
  "calendar_id": "eric@example.com",
  "email": "eric@example.com"
}
```

## Google Calendar service account & sharing

- Create a Google service account and enable the Calendar API. Download the JSON key and set `GOOGLE_SERVICE_ACCOUNT_FILE` to its path.
- The service account must have access to the doctors' calendars. You can either:
  - Share each doctor's calendar with the service account email (recommended), or
  - Use a shared clinic calendar and configure `calendar_id` accordingly.

Required OAuth scopes used by the server:

```
https://www.googleapis.com/auth/calendar
```

## SMTP / Email

- The server will only attempt to send emails if `SMTP_HOST` and `SMTP_FROM` are configured.
- If `SMTP_USER` and `SMTP_PASS` are provided, the server will authenticate to the SMTP server.
- There are two places where email sending occurs:
  - After booking: sends confirmation emails with an .ics attachment to patient and doctor (best-effort)
  - After cancellation/reschedule: sends notification emails to both patient and doctor

## Troubleshooting & common errors

- "HUGGINGFACE_API_TOKEN missing": set `HUGGINGFACE_API_TOKEN` in `.env` or environment
- Google service account errors / calendar access denied: ensure the service account JSON path is set and the calendars are shared with the service account
- SMTP not configured / `SMTP not configured`: set `SMTP_HOST` and `SMTP_FROM` (and auth if needed)
- Booking errors such as "Requested time is not an available slot": verify the doctor's `weekly_schedule` and that the requested start time matches a returned availability slot
- If calendar queries fail, inspect the logs for `[freebusy]` messages printed by the server

## Developer notes

- Language support: assistant handles English and French. Input is detected and (when French) translated to English for intent classification; replies are given in the user's language when possible.
- The MCP server exposes tools that are called by the FastAPI wrapper (`api.py`). The tools' names and expected parameters are defined in `server.py`.
- Tests and UI are intentionally minimal; this repo is a solid starting point to integrate more robust persistence, authentication, and UI.
