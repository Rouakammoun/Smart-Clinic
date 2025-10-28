# server.py
import os, json
from typing import List, Literal, Dict, Any, Optional
from datetime import datetime, timedelta, time
import pytz

from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import smtplib
from email.message import EmailMessage
from uuid import uuid4
from urllib.parse import urlencode

load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_PATH = os.getenv("HOSPITAL_DATA_PATH", "data/doctors.json")
DEFAULT_TZ = os.getenv("CLINIC_TIMEZONE", "Asia/Karachi")
DEFAULT_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_FROM = os.getenv("SMTP_FROM")
# New: configurable minimum lead time (in minutes) before an appointment can start
MIN_LEAD_MINUTES = int(os.getenv("MIN_LEAD_MINUTES", "0"))


def _abs(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


DATA_PATH = _abs(DEFAULT_DATA_PATH)
SERVICE_ACCOUNT_FILE = _abs(DEFAULT_SERVICE_ACCOUNT_FILE) if DEFAULT_SERVICE_ACCOUNT_FILE else None


def load_directory() -> Dict[str, Any]:
    try:
        if os.path.exists(DATA_PATH):
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        print(f"[server] WARN: Data file not found at {DATA_PATH}. Using empty DB.")
    except Exception as e:
        print(f"[server] ERROR: Failed loading {DATA_PATH}: {e}")
    return {"doctors": [], "condition_map": {}}


DB = load_directory()
app = FastMCP("Hospital-MCP-Server")


def _find_doctor(doctor_id: str) -> Optional[Dict[str, Any]]:
    for d in DB.get("doctors", []):
        if d.get("doctor_id") == doctor_id:
            return d
    return None


@app.tool()
def doctor_lookup(
    condition: str,
    visit_mode: Literal["online", "inperson", "any"] = "any",
) -> List[Dict[str, Any]]:
    """
    Return matching doctors for a given condition and visit mode.
    """
    ids = DB.get("condition_map", {}).get(condition, [])
    docs = [d for d in DB.get("doctors", []) if d.get("doctor_id") in ids]
    out = []
    for d in docs:
        out.append({
            "doctor_id": d.get("doctor_id"),
            "name": d.get("name"),
            "specialization": d.get("specialization"),
            "experience_years": d.get("experience_years"),
            "fees_pkr": {
                "online": d.get("fees", {}).get("online_pkr"),
                "inperson": d.get("fees", {}).get("inperson_pkr"),
            },
            "weekly_schedule": d.get("weekly_schedule", {}),
            "location": d.get("location"),
            "calendar_id": d.get("calendar_id"),
            "email": d.get("email"),
        })
    return out


@app.tool()
def list_doctors() -> List[Dict[str, Any]]:
    """Return the directory of doctors with basic details."""
    out: List[Dict[str, Any]] = []
    for d in DB.get("doctors", []):
        out.append({
            "doctor_id": d.get("doctor_id"),
            "name": d.get("name"),
            "specialization": d.get("specialization"),
            "experience_years": d.get("experience_years"),
            "fees_pkr": {
                "online": d.get("fees", {}).get("online_pkr"),
                "inperson": d.get("fees", {}).get("inperson_pkr"),
            },
            "weekly_schedule": d.get("weekly_schedule", {}),
            "location": d.get("location"),
            "calendar_id": d.get("calendar_id"),
            "email": d.get("email"),
        })
    return out


@app.tool()
def doctor_lookup_by_name(
    doctor_name: str,
) -> Optional[Dict[str, Any]]:
    """
    Find a doctor by name and return their details.
    """
    name_lower = doctor_name.lower().strip()
    
    for d in DB.get("doctors", []):
        doc_name = str(d.get("name", "")).lower().strip()
        # Exact match or partial match
        if name_lower in doc_name or doc_name in name_lower:
            return {
                "doctor_id": d.get("doctor_id"),
                "name": d.get("name"),
                "specialization": d.get("specialization"),
                "experience_years": d.get("experience_years"),
                "fees_pkr": {
                    "online": d.get("fees", {}).get("online_pkr"),
                    "inperson": d.get("fees", {}).get("inperson_pkr"),
                },
                "weekly_schedule": d.get("weekly_schedule", {}),
                "location": d.get("location"),
                "calendar_id": d.get("calendar_id"),
                "email": d.get("email"),
            }
    
    return None


@app.tool()
def doctor_weekly_availability(
    doctor_name: str,
    days: int = 7,
    slot_minutes: int = 30,
) -> Dict[str, Any]:
    """
    Get doctor's availability for the next N days based on their routine schedule + Google Calendar.
    """
    # First find the doctor
    name_lower = doctor_name.lower().strip()
    doctor = None
    
    for d in DB.get("doctors", []):
        doc_name = str(d.get("name", "")).lower().strip()
        if name_lower in doc_name or doc_name in name_lower:
            doctor = d
            break
    
    if not doctor:
        return {"error": f"Doctor '{doctor_name}' not found"}
    
    # Get next N days availability
    today = datetime.now(pytz.timezone(DEFAULT_TZ)).date()
    result_map = {}
    
    for i in range(days):
        check_date = today + timedelta(days=i)
        date_str = check_date.isoformat()
        slots, meta = _compute_free_slots_for_date(doctor, date_str, slot_minutes)
        
        result_map[date_str] = {
            "day_name": check_date.strftime("%A"),
            "slots": slots,
            "routine_hours": doctor.get("weekly_schedule", {}).get(check_date.strftime("%a"), []),
        }
        
        if meta:
            result_map[date_str]["warning"] = meta.get("warning")
    
    return {
        "doctor_id": doctor.get("doctor_id"),
        "doctor_name": doctor.get("name"),
        "specialization": doctor.get("specialization"),
        "fees_pkr": {
            "online": doctor.get("fees", {}).get("online_pkr"),
            "inperson": doctor.get("fees", {}).get("inperson_pkr"),
        },
        "location": doctor.get("location"),
        "days": result_map,
        "timezone": DEFAULT_TZ,
    }


def _google_service() -> Any:
    if not SERVICE_ACCOUNT_FILE or not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise RuntimeError("Google service account credentials not configured. Set GOOGLE_SERVICE_ACCOUNT_FILE env to a JSON file path.")
    scopes = ["https://www.googleapis.com/auth/calendar"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse_time(t: str) -> time:
    return time.fromisoformat(t)


def _localize(dt: datetime) -> datetime:
    return pytz.timezone(DEFAULT_TZ).localize(dt)


def _to_utc_iso(local_dt: datetime) -> str:
    return local_dt.astimezone(pytz.UTC).isoformat()


def _overlaps(slot_start: datetime, slot_end: datetime, busy: List[Dict[str, str]]) -> bool:
    for b in busy:
        b_start = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
        b_end = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
        if slot_start < b_end and slot_end > b_start:
            return True
    return False


def _send_plain_email(recipients: List[str], subject: str, body: str, ics_content: Optional[str] = None) -> tuple[bool, Optional[str]]:
    if not SMTP_HOST or not SMTP_FROM:
        return False, "SMTP not configured"
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = ", ".join(recipients)
        msg.set_content(body)
        if ics_content:
            msg.add_attachment(
                ics_content.encode("utf-8"),
                maintype="text",
                subtype="calendar",
                filename="appointment.ics",
                params={"method": "REQUEST", "charset": "UTF-8", "name": "appointment.ics"},
            )
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            if SMTP_USER and SMTP_PASS:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        return True, None
    except Exception as e:
        print(f"[smtp] ERROR: {e}")
        return False, str(e)


@app.tool()
def availability_tool(
    doctor_id: str,
    date: str,
    slot_minutes: int = 30,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return free appointment slots for a doctor on a given date or date range.
    - date: YYYY-MM-DD (clinic local timezone)
    - end_date: optional YYYY-MM-DD inclusive
    - slot_minutes: size of appointment slots
    """
    doctor = _find_doctor(doctor_id)
    if not doctor:
        return {"error": f"Unknown doctor_id: {doctor_id}"}

    # Default to today's date if date is missing/empty
    if not date or not str(date).strip():
        date = datetime.now(pytz.timezone(DEFAULT_TZ)).date().isoformat()

    if not end_date:
        slots, meta = _compute_free_slots_for_date(doctor, date, slot_minutes)
        resp = {
            "doctor_id": doctor_id,
            "doctor_name": doctor.get("name"),
            "date": date,
            "slot_minutes": slot_minutes,
            "slots": slots,
            "timezone": DEFAULT_TZ,
        }
        if meta:
            resp["warning_code"] = meta.get("code")
            resp["warning"] = meta.get("warning")
        return resp

    # Date range
    try:
        start_d = datetime.fromisoformat(date).date()
        end_d = datetime.fromisoformat(end_date).date()
    except Exception:
        return {"error": "Invalid date or end_date. Use YYYY-MM-DD"}
    if end_d < start_d:
        start_d, end_d = end_d, start_d
    day = start_d
    result_map: Dict[str, List[str]] = {}
    while day <= end_d:
        ds = str(day)
        slots, _meta_unused = _compute_free_slots_for_date(doctor, ds, slot_minutes)
        result_map[ds] = slots
        day += timedelta(days=1)
    return {
        "doctor_id": doctor_id,
        "doctor_name": doctor.get("name"),
        "date": date,
        "end_date": end_date,
        "slot_minutes": slot_minutes,
        "dates": result_map,
        "timezone": DEFAULT_TZ,
    }


def _compute_free_slots_for_date(doctor: Dict[str, Any], date: str, slot_minutes: int) -> tuple[List[str], Optional[Dict[str, str]]]:
    schedule = doctor.get("weekly_schedule", {})
    try:
        day_dt = datetime.fromisoformat(date)
        day_name = day_dt.strftime("%a")
    except Exception:
        return [], {"code": "invalid_date", "warning": "Invalid date format"}
    windows = schedule.get(day_name, [])
    if not windows:
        return [], {"code": "no_schedule", "warning": "No clinic hours for this day"}
    slot_ranges: List[tuple[datetime, datetime]] = []
    earliest = None
    latest = None
    for win in windows:
        try:
            start_s, end_s = win.split("-")
            start_t = _parse_time(start_s)
            end_t = _parse_time(end_s)
        except Exception:
            continue
        day = datetime.fromisoformat(date)
        start_local = _localize(datetime.combine(day.date(), start_t))
        end_local = _localize(datetime.combine(day.date(), end_t))
        if earliest is None or start_local < earliest:
            earliest = start_local
        if latest is None or end_local > latest:
            latest = end_local
        cursor = start_local
        while cursor + timedelta(minutes=slot_minutes) <= end_local:
            slot_ranges.append((cursor, cursor + timedelta(minutes=slot_minutes)))
            cursor += timedelta(minutes=slot_minutes)
    if not earliest or not latest:
        return [], {"code": "no_schedule", "warning": "No clinic hours for this day"}
    try:
        service = _google_service()
        fb = service.freebusy().query(body={
            "timeMin": _to_utc_iso(earliest),
            "timeMax": _to_utc_iso(latest),
            "timeZone": DEFAULT_TZ,
            "items": [{"id": doctor.get("calendar_id")}],
        }).execute()
        cal_data = fb.get("calendars", {}).get(doctor.get("calendar_id"), {})
        # Check for calendar-level errors
        if cal_data.get("errors"):
            error_details = "; ".join([f"{e.get('reason')}: {e.get('domain')}" for e in cal_data.get("errors", [])])
            print(f"[freebusy] Calendar access error for {doctor.get('calendar_id')}: {error_details}")
            return [], {"code": "calendar_access_error", "warning": f"Calendar access denied. Please ensure calendar is shared with service account."}
        busy_list = cal_data.get("busy", [])
    except Exception as e:
        print(f"[freebusy] ERROR: {e}")
        import traceback
        traceback.print_exc()
        return [], {"code": "freebusy_error", "warning": f"Calendar query failed: {str(e)[:100]}"}
    free_slots: List[str] = []
    now_local = datetime.now(pytz.timezone(DEFAULT_TZ))
    is_today = day_dt.date() == now_local.date()
    lead_delta = timedelta(minutes=MIN_LEAD_MINUTES)
    for s_start, s_end in slot_ranges:
        if is_today and s_start < (now_local + lead_delta):
            continue
        if not _overlaps(s_start, s_end, busy_list):
            free_slots.append(s_start.strftime("%H:%M"))
    if not free_slots:
        # Distinguish cause (no future slots vs fully booked)
        any_future_candidates = any((not is_today or (rng[0] >= (now_local + lead_delta))) for rng in slot_ranges)
        code = "no_future_slots" if not any_future_candidates else "fully_booked"
        return [], {"code": code, "warning": "No available slots for the selected day"}
    return free_slots, None


def _within_schedule(doctor: Dict[str, Any], start_local: datetime, end_local: datetime) -> bool:
    schedule = doctor.get("weekly_schedule", {})
    day_name = start_local.strftime("%a")
    windows = schedule.get(day_name, [])
    for win in windows:
        try:
            start_s, end_s = win.split("-")
            w_start = _localize(datetime.combine(start_local.date(), _parse_time(start_s)))
            w_end = _localize(datetime.combine(start_local.date(), _parse_time(end_s)))
        except Exception:
            continue
        if start_local >= w_start and end_local <= w_end:
            return True
    return False


def _slot_conflicts_with_calendar(doctor: Dict[str, Any], start_local: datetime, end_local: datetime) -> bool:
    try:
        service = _google_service()
        fb = service.freebusy().query(body={
            "timeMin": _to_utc_iso(start_local),
            "timeMax": _to_utc_iso(end_local),
            "timeZone": DEFAULT_TZ,
            "items": [{"id": doctor.get("calendar_id")}],
        }).execute()
        busy_list = fb.get("calendars", {}).get(doctor.get("calendar_id"), {}).get("busy", [])
    except Exception as e:
        print(f"[freebusy-preflight] ERROR: {e}")
        # If we cannot check, be conservative and say it conflicts to avoid double bookings
        return True
    return _overlaps(start_local, end_local, busy_list)


@app.tool()
def appointment_book_tool(
    doctor_id: str,
    start: str,
    end: str,
    patient_name: str,
    patient_email: str,
    patient_phone: Optional[str] = None,
    patient_age: Optional[int] = None,
    patient_sex: Optional[str] = None,
    visit_mode: Optional[str] = None,
    condition: Optional[str] = None,
    send_invitations: Optional[bool] = False,
    create_meet: Optional[bool] = False,
) -> Dict[str, Any]:
    """
    Book an appointment in the doctor's Google Calendar.
    - start/end: ISO-like local (YYYY-MM-DDTHH:MM)
    """
    doctor = _find_doctor(doctor_id)
    if not doctor:
        return {"error": f"Unknown doctor_id: {doctor_id}"}

    try:
        start_local = _localize(datetime.fromisoformat(start))
        end_local = _localize(datetime.fromisoformat(end))
    except Exception:
        return {"error": "Invalid start/end format. Use YYYY-MM-DDTHH:MM"}
    if end_local <= start_local:
        return {"error": "End time must be after start time."}

    # Basic email validation
    if not isinstance(patient_email, str) or "@" not in patient_email or "." not in patient_email.split("@")[-1]:
        return {"error": "Invalid patient_email."}

    # Reject booking inside the lead window
    now_local = datetime.now(pytz.timezone(DEFAULT_TZ))
    lead_cutoff = now_local + timedelta(minutes=MIN_LEAD_MINUTES)
    if start_local < lead_cutoff:
        return {"error": f"Appointments must be booked at least {MIN_LEAD_MINUTES} minutes in advance."}

    # Enforce schedule hours
    if not _within_schedule(doctor, start_local, end_local):
        return {"error": "Selected time is outside clinic hours for this doctor."}

    # Preflight conflict check
    if _slot_conflicts_with_calendar(doctor, start_local, end_local):
        return {"error": "Requested time is no longer available. Please pick another slot."}

    # Guard: requested start must match an availability_tool slot for that day
    try:
        slot_minutes = int((end_local - start_local).total_seconds() // 60)
        slots_for_day, _meta = _compute_free_slots_for_date(doctor, start_local.date().isoformat(), slot_minutes)
        requested_hm = start_local.strftime("%H:%M")
        if requested_hm not in slots_for_day:
            return {"error": "Requested time is not an available slot for this doctor/day. Please pick a listed slot from availability."}
    except Exception:
        # If guard check fails unexpectedly, continue; calendar conflict check above still protects double-booking
        pass

    summary = f"Consultation: {doctor.get('name')} ↔ {patient_name}"
    description_lines = [
        f"Visit mode: {visit_mode or 'unspecified'}",
        f"Condition: {condition or 'unspecified'}",
        "Created via Hospital MCP assistant.",
        f"Patient: {patient_name} <{patient_email}>",
    ]
    if patient_phone:
        description_lines.append(f"Phone: {patient_phone}")
    if patient_age is not None:
        description_lines.append(f"Age: {patient_age}")
    if patient_sex:
        description_lines.append(f"Sex: {patient_sex}")

    event: Dict[str, Any] = {
        "summary": summary,
        "description": "\n".join(description_lines),
        "start": {"dateTime": start_local.isoformat(), "timeZone": DEFAULT_TZ},
        "end": {"dateTime": end_local.isoformat(), "timeZone": DEFAULT_TZ},
        "reminders": {"useDefault": True},
    }

    params: Dict[str, Any] = {}

    # Only include attendees and sendUpdates if explicit opt-in
    if send_invitations:
        event["attendees"] = [
            {"email": doctor.get("email")},
            {"email": patient_email},
        ]
        params["sendUpdates"] = "all"

    vm = (visit_mode or "").lower().strip()
    if vm == "online" and create_meet:
        event["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        params["conferenceDataVersion"] = 1
    elif vm == "inperson":
        if doctor.get("location"):
            event["location"] = doctor.get("location")

    try:
        service = _google_service()
        created = service.events().insert(
            calendarId=doctor.get("calendar_id"),
            body=event,
            **params,
        ).execute()
    except Exception as e:
        # If the error is due to conference creation, retry without conference
        msg = str(e)
        if "Invalid conference type" in msg or "conferenceData" in msg:
            try:
                event.pop("conferenceData", None)
                params.pop("conferenceDataVersion", None)
                created = service.events().insert(
                    calendarId=doctor.get("calendar_id"),
                    body=event,
                    **params,
                ).execute()
            except Exception as e2:
                return {"error": f"Google Calendar insert error: {e2}"}
        else:
            return {"error": f"Google Calendar insert error: {e}"}

    # Try to send clinic confirmation email (best-effort)
    fee_online = (doctor.get("fees", {}) or {}).get("online_pkr")
    fee_inperson = (doctor.get("fees", {}) or {}).get("inperson_pkr")
    fee_str = f"PKR {fee_online if vm=='online' else fee_inperson}"
    human_date = start_local.strftime('%a, %d %b %Y')
    human_time = f"{start_local.strftime('%H:%M')} - {end_local.strftime('%H:%M')} ({DEFAULT_TZ})"
    specialization = doctor.get('specialization') or "General"
    clinic_line = doctor.get('location') or "Unity Care Clinic, Karachi"
    meet_link = created.get('hangoutLink') or (created.get('conferenceData', {}) or {}).get('entryPoints', [{}])[0].get('uri') if isinstance(created.get('conferenceData'), dict) else None
    mode_label = "Online (Google Meet)" if vm == "online" else "In-person"
    # Patient email
    patient_subject = "Appointment Confirmation – Unity Care Clinic"
    patient_lines = [
        f"Dear {patient_name},\n\n",
        f"Your appointment to consult for {condition or 'unspecified'} has been scheduled. Please find the details below:\n\n",
        f"Doctor: {doctor.get('name')}\n",
        f"Specialization: {specialization}\n",
        f"Date: {human_date}\n",
        f"Time: {human_time}\n",
        f"Mode: {mode_label}\n",
        f"Fee: {fee_str}\n",
        f"Clinic: {clinic_line}\n",
    ]
    if vm == "online" and meet_link:
        patient_lines.append(f"Google Meet: {meet_link}\n")
    add_link = _google_add_to_calendar_link(summary, start_local, end_local, details="Consultation scheduled via Unity Care Clinic.", location=doctor.get("location") if vm == "inperson" else None)
    patient_lines.extend([
        "\nPlease join 15 minutes early. A calendar invite is attached; kindly add it to your calendar and join on time.\n",
        f"Add to Google Calendar: {add_link}\n\n",
        "Thank you,\n",
        "Unity Care Clinic",
    ])
    patient_body = "".join(patient_lines)

    # Doctor email
    doctor_subject = "New Appointment Scheduled – Unity Care Clinic"
    doctor_lines = [
        f"Dear {doctor.get('name')},\n\n",
        "A new appointment has been booked for a patient consultation. Details are as follows:\n\n",
        f"Patient: {patient_name}\n",
    ]
    if patient_phone:
        doctor_lines.append(f"Patient phone: {patient_phone}\n")
    if patient_age is not None:
        doctor_lines.append(f"Patient age: {patient_age}\n")
    if patient_sex:
        doctor_lines.append(f"Patient sex: {patient_sex}\n")
    doctor_lines.extend([
        f"Condition: {condition or 'Unspecified'}\n",
        f"Date: {human_date}\n",
        f"Time: {human_time}\n",
        f"Mode: {mode_label}\n",
        f"Fee: {fee_str}\n",
        f"Clinic: {clinic_line}\n",
    ])
    if vm == "online" and meet_link:
        doctor_lines.append(f"Google Meet: {meet_link}\n")
    doctor_lines.extend([
        "\nPlease be available on time. A calendar event is attached; kindly add it to your calendar.\n\n",
        "Thank you,\n",
        "Unity Care Clinic",
    ])
    doctor_body = "".join(doctor_lines)

    ics = _build_ics(
        summary=summary,
        start_local=start_local,
        end_local=end_local,
        description="Consultation scheduled via Unity Care Clinic.",
        location=doctor.get("location") if vm == "inperson" else None,
        organizer_email=SMTP_FROM,
        attendee_emails=[patient_email, doctor.get("email")],
        uid=created.get("id"),
    )
    # Send to patient
    smtp_ok1, smtp_err1 = _send_plain_email([patient_email], patient_subject, patient_body, ics_content=ics)
    # Send to doctor (if email available)
    smtp_ok2, smtp_err2 = (True, None)
    if doctor.get("email"):
        smtp_ok2, smtp_err2 = _send_plain_email([doctor.get("email")], doctor_subject, doctor_body, ics_content=ics)
    smtp_ok = smtp_ok1 and smtp_ok2
    smtp_err = smtp_err1 or smtp_err2

    return {
        "message": "Appointment booked successfully." + (" Google invites sent." if send_invitations else ""),
        "event_link": created.get("htmlLink"),
        "event_id": created.get("id"),
        "doctor_id": doctor_id,
        "patient_email": patient_email,
        "smtp_status": "sent" if smtp_ok else "failed",
        "smtp_error": smtp_err,
    }


def _google_add_to_calendar_link(summary: str, start_local: datetime, end_local: datetime, details: str = "", location: Optional[str] = None) -> str:
    start_utc = start_local.astimezone(pytz.UTC).strftime("%Y%m%dT%H%M%SZ")
    end_utc = end_local.astimezone(pytz.UTC).strftime("%Y%m%dT%H%M%SZ")
    params = {
        "action": "TEMPLATE",
        "text": summary,
        "dates": f"{start_utc}/{end_utc}",
        "details": details,
    }
    if location:
        params["location"] = location
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def _build_ics(
    summary: str,
    start_local: datetime,
    end_local: datetime,
    description: str = "",
    location: Optional[str] = None,
    organizer_email: Optional[str] = None,
    attendee_emails: Optional[list[str]] = None,
    uid: Optional[str] = None,
) -> str:
    start_utc = start_local.astimezone(pytz.UTC).strftime("%Y%m%dT%H%M%SZ")
    end_utc = end_local.astimezone(pytz.UTC).strftime("%Y%m%dT%H%M%SZ")
    dtstamp = datetime.now(pytz.UTC).strftime("%Y%m%dT%H%M%SZ")
    uid_val = uid or f"{uuid4()}@unity-care"
    desc = (description or "").replace("\n", "\\n")
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//Unity Care Clinic//MCP//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid_val}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{start_utc}",
        f"DTEND:{end_utc}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{desc}",
    ]
    if location:
        lines.append(f"LOCATION:{location}")
    if organizer_email:
        lines.append(f"ORGANIZER;CN=Unity Care Clinic:MAILTO:{organizer_email}")
    for e in (attendee_emails or []):
        lines.append(f"ATTENDEE;CN={e};RSVP=FALSE:MAILTO:{e}")
    lines.extend(["END:VEVENT", "END:VCALENDAR"])
    return "\r\n".join(lines) + "\r\n"


@app.tool()
def now_tool() -> Dict[str, Any]:
    """Return the current date/time in clinic timezone and UTC."""
    tz = pytz.timezone(DEFAULT_TZ)
    now_local = datetime.now(tz)
    now_utc = datetime.now(pytz.UTC)
    return {
        "timezone": DEFAULT_TZ,
        "date": now_local.date().isoformat(),
        "time": now_local.strftime("%H:%M:%S"),
        "iso_local": now_local.isoformat(),
        "iso_utc": now_utc.isoformat(),
        "weekday_index": now_local.weekday(),
        "weekday_short": now_local.strftime("%a"),
        "weekday_long": now_local.strftime("%A"),
    }


def _event_contains_patient(ev: Dict[str, Any], patient_email: str) -> bool:
    if not patient_email:
        return False
    pe = patient_email.lower().strip()
    # Check attendees
    for a in ev.get("attendees", []) or []:
        try:
            email = str(a.get("email", "")).lower().strip()
            if email == pe:
                return True
        except Exception:
            continue
    # Fallback: check description lines
    desc = (ev.get("description") or "").lower()
    return pe in desc


@app.tool()
def list_appointments_tool(
    doctor_id: Optional[str] = None,
    patient_email: Optional[str] = None,
    window_days: int = 30,
) -> Dict[str, Any]:
    """
    List upcoming appointments for the patient for the next N days.
    doctor_id is optional to scope to one calendar. Patient email is REQUIRED to protect privacy.
    """
    if not patient_email:
        return {"error": "patient_email is required"}
    try:
        service = _google_service()
    except Exception as e:
        return {"error": str(e)}
    calendars: List[Dict[str, Any]] = []
    if doctor_id:
        d = _find_doctor(doctor_id)
        if not d:
            return {"error": f"Unknown doctor_id: {doctor_id}"}
        calendars.append({"id": d.get("calendar_id"), "doctor": d})
    else:
        for d in DB.get("doctors", []):
            calendars.append({"id": d.get("calendar_id"), "doctor": d})
    now_local = datetime.now(pytz.timezone(DEFAULT_TZ))
    time_min = now_local.isoformat()
    time_max = (now_local + timedelta(days=window_days)).isoformat()
    results: List[Dict[str, Any]] = []
    for c in calendars:
        try:
            events = service.events().list(
                calendarId=c["id"], timeMin=time_min, timeMax=time_max, singleEvents=True, orderBy="startTime"
            ).execute().get("items", [])
        except Exception as e:
            print(f"[list_appointments] ERROR: {e}")
            continue
        for ev in events:
            if not _event_contains_patient(ev, patient_email):
                continue
            start_dt = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
            end_dt = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date")
            results.append({
                "doctor_id": c["doctor"].get("doctor_id"),
                "doctor_name": c["doctor"].get("name"),
                "event_id": ev.get("id"),
                "htmlLink": ev.get("htmlLink"),
                "start": start_dt,
                "end": end_dt,
                "summary": ev.get("summary"),
                # Do not leak attendees list
            })
    return {"events": results}


@app.tool()
def cancel_appointment_tool(
    doctor_id: str,
    event_id: str,
    patient_email: str,
    notify_attendees: bool = True,
) -> Dict[str, Any]:
    """Cancel an event by id for the patient. Requires patient_email match."""
    d = _find_doctor(doctor_id)
    if not d:
        return {"error": f"Unknown doctor_id: {doctor_id}"}
    try:
        service = _google_service()
        ev = service.events().get(calendarId=d.get("calendar_id"), eventId=event_id).execute()
        if not _event_contains_patient(ev, patient_email):
            return {"error": "Unauthorized: event does not belong to the requesting patient."}
        
        # Extract appointment details for email
        start_dt_str = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
        end_dt_str = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date")
        summary = ev.get("summary", "")
        description = ev.get("description", "")
        
        # Parse dates for email formatting
        try:
            if "T" in start_dt_str:
                start_local = datetime.fromisoformat(start_dt_str.replace("Z", "+00:00")).astimezone(pytz.timezone(DEFAULT_TZ))
                end_local = datetime.fromisoformat(end_dt_str.replace("Z", "+00:00")).astimezone(pytz.timezone(DEFAULT_TZ))
                human_date = start_local.strftime('%a, %d %b %Y')
                human_time = f"{start_local.strftime('%H:%M')} - {end_local.strftime('%H:%M')} ({DEFAULT_TZ})"
            else:
                human_date = start_dt_str
                human_time = "All day"
        except Exception:
            human_date = start_dt_str
            human_time = ""
        
        # Extract patient name from summary or description
        patient_name = "Patient"
        if "↔" in summary:
            parts = summary.split("↔")
            if len(parts) > 1:
                patient_name = parts[1].strip()
        
        params = {}
        if notify_attendees:
            params["sendUpdates"] = "all"
        service.events().delete(calendarId=d.get("calendar_id"), eventId=event_id, **params).execute()
        
        # Send cancellation emails
        # Patient email
        patient_subject = "Appointment Cancelled – Unity Care Clinic"
        patient_body = f"""Dear {patient_name},

Your appointment has been cancelled as requested. Here are the details:

Doctor: {d.get('name')}
Specialization: {d.get('specialization') or 'General'}
Date: {human_date}
Time: {human_time}

If you wish to reschedule, please contact us or book a new appointment through our system.

Thank you,
Unity Care Clinic"""
        
        # Doctor email
        doctor_subject = "Appointment Cancelled – Unity Care Clinic"
        doctor_body = f"""Dear {d.get('name')},

An appointment has been cancelled by the patient. Details are as follows:

Patient: {patient_name}
Patient email: {patient_email}
Date: {human_date}
Time: {human_time}

This time slot is now available for other bookings.

Thank you,
Unity Care Clinic"""
        
        # Send emails (best-effort)
        smtp_ok1, smtp_err1 = _send_plain_email([patient_email], patient_subject, patient_body)
        smtp_ok2, smtp_err2 = (True, None)
        if d.get("email"):
            smtp_ok2, smtp_err2 = _send_plain_email([d.get("email")], doctor_subject, doctor_body)
        
        smtp_ok = smtp_ok1 and smtp_ok2
        smtp_err = smtp_err1 or smtp_err2
        
        return {
            "ok": True,
            "message": "Appointment cancelled successfully.",
            "smtp_status": "sent" if smtp_ok else "failed",
            "smtp_error": smtp_err,
        }
    except Exception as e:
        return {"error": f"Cancel error: {e}"}


@app.tool()
def reschedule_tool(
    doctor_id: str,
    event_id: str,
    new_start: str,
    new_end: str,
    patient_email: str,
) -> Dict[str, Any]:
    """Reschedule an event for the patient. Requires patient_email match."""
    d = _find_doctor(doctor_id)
    if not d:
        return {"error": f"Unknown doctor_id: {doctor_id}"}
    try:
        start_local = _localize(datetime.fromisoformat(new_start))
        end_local = _localize(datetime.fromisoformat(new_end))
    except Exception:
        return {"error": "Invalid new_start/new_end format. Use YYYY-MM-DDTHH:MM"}
    now_local = datetime.now(pytz.timezone(DEFAULT_TZ))
    lead_cutoff = now_local + timedelta(minutes=MIN_LEAD_MINUTES)
    if start_local < lead_cutoff:
        return {"error": f"Appointments must be rescheduled at least {MIN_LEAD_MINUTES} minutes in advance."}
    if not _within_schedule(d, start_local, end_local):
        return {"error": "Selected time is outside clinic hours for this doctor."}
    if _slot_conflicts_with_calendar(d, start_local, end_local):
        return {"error": "Requested time is no longer available. Please pick another slot."}
    try:
        service = _google_service()
        ev = service.events().get(calendarId=d.get("calendar_id"), eventId=event_id).execute()
        if not _event_contains_patient(ev, patient_email):
            return {"error": "Unauthorized: event does not belong to the requesting patient."}
        
        # Extract old appointment details
        old_start_dt_str = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
        old_end_dt_str = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date")
        summary = ev.get("summary", "")
        
        # Parse old dates for email
        try:
            if "T" in old_start_dt_str:
                old_start = datetime.fromisoformat(old_start_dt_str.replace("Z", "+00:00")).astimezone(pytz.timezone(DEFAULT_TZ))
                old_end = datetime.fromisoformat(old_end_dt_str.replace("Z", "+00:00")).astimezone(pytz.timezone(DEFAULT_TZ))
                old_date = old_start.strftime('%a, %d %b %Y')
                old_time = f"{old_start.strftime('%H:%M')} - {old_end.strftime('%H:%M')}"
            else:
                old_date = old_start_dt_str
                old_time = "All day"
        except Exception:
            old_date = old_start_dt_str
            old_time = ""
        
        # Extract patient name from summary
        patient_name = "Patient"
        if "↔" in summary:
            parts = summary.split("↔")
            if len(parts) > 1:
                patient_name = parts[1].strip()
        
        ev["start"] = {"dateTime": start_local.isoformat(), "timeZone": DEFAULT_TZ}
        ev["end"] = {"dateTime": end_local.isoformat(), "timeZone": DEFAULT_TZ}
        updated = service.events().update(
            calendarId=d.get("calendar_id"), eventId=event_id, body=ev, sendUpdates="all"
        ).execute()
        
        # Format new dates for email
        new_date = start_local.strftime('%a, %d %b %Y')
        new_time = f"{start_local.strftime('%H:%M')} - {end_local.strftime('%H:%M')} ({DEFAULT_TZ})"
        
        # Send reschedule emails
        # Patient email
        patient_subject = "Appointment Rescheduled – Unity Care Clinic"
        patient_body = f"""Dear {patient_name},

Your appointment has been successfully rescheduled. Here are the updated details:

Doctor: {d.get('name')}
Specialization: {d.get('specialization') or 'General'}

Previous Date: {old_date}
Previous Time: {old_time}

New Date: {new_date}
New Time: {new_time}

If you have any questions or need to make further changes, please contact us.

Thank you,
Unity Care Clinic"""
        
        # Doctor email
        doctor_subject = "Appointment Rescheduled – Unity Care Clinic"
        doctor_body = f"""Dear {d.get('name')},

An appointment has been rescheduled by the patient. Details are as follows:

Patient: {patient_name}
Patient email: {patient_email}

Previous Date: {old_date}
Previous Time: {old_time}

New Date: {new_date}
New Time: {new_time}

Please update your schedule accordingly.

Thank you,
Unity Care Clinic"""
        
        # Send emails (best-effort)
        smtp_ok1, smtp_err1 = _send_plain_email([patient_email], patient_subject, patient_body)
        smtp_ok2, smtp_err2 = (True, None)
        if d.get("email"):
            smtp_ok2, smtp_err2 = _send_plain_email([d.get("email")], doctor_subject, doctor_body)
        
        smtp_ok = smtp_ok1 and smtp_ok2
        smtp_err = smtp_err1 or smtp_err2
        
        return {
            "ok": True,
            "event_link": updated.get("htmlLink"),
            "message": "Appointment rescheduled successfully.",
            "smtp_status": "sent" if smtp_ok else "failed",
            "smtp_error": smtp_err,
        }
    except Exception as e:
        return {"error": f"Reschedule error: {e}"}


def main():
    app.run()  # stdio transport


if __name__ == "__main__":
    main()
