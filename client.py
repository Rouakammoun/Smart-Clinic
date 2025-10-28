# client.py
import os, json, asyncio
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession
from dotenv import load_dotenv
import re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

# Load environment variables from .env
load_dotenv()

from huggingface_hub import InferenceClient
# Use consistent env vars with api.py
HF_TOKEN = os.getenv("HUGGINGFACE_API_TOKEN")
HF_MODEL = os.getenv("HF_MODEL", "deepseek-ai/DeepSeek-V3")
client = InferenceClient(token=HF_TOKEN)

MODEL_REPO = HF_MODEL
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PATH = os.path.join(PROJECT_ROOT, "server.py")
CLINIC_TZ = os.getenv("CLINIC_TIMEZONE", "Asia/Karachi")
## duplicate definitions removed

SYSTEM_PROMPT = """You are Medbridge AI, a multilingual, concise triage & booking assistant.

MANDATORY DOCTOR DISPLAY FORMAT:
ALWAYS display doctors in this format: "Dr. [Name] - [Specialization]"
Examples:
- Dr. Eric - Ophthalmology
- Dr. Diego - Dermatology  
- Dr. Ali - General Medicine

Flow:
1) infer likely condition; 2) call doctor_lookup; 3) offer to check availability; 4) call availability_tool and show slots; 
5) when user picks a date, ALWAYS ask them to choose a specific time from slots; 
6) collect required fields (name, email) and optional (phone, age, sex);
7) BEFORE BOOKING, present a COMPLETE Review with ALL collected information in this exact format:

Here's a summary of your appointment details:
- Doctor: Dr. [Name] - [Specialization]
- Patient Name: [name]
- Patient Email: [email]
- Patient Phone: [phone if provided]
- Patient Age: [age if provided]
- Patient Sex: [sex if provided]
- Date: [full date with day name]
- Time: [HH:MM]
- Mode: [online/in-person]
- Fee: PKR [amount]
- Clinic: [location]

Then ask: "Please confirm to proceed with booking. Reply 'confirm' or 'yes' to book."
Only on explicit yes/confirm/book/go ahead call appointment_book_tool.

HARD REQUIREMENTS: 
- NEVER display doctor names without their specialization
- NEVER claim availability without availability_tool
- NEVER book without user confirmation and appointment_book_tool
STYLE: plain text; no asterisks; labeled lines; slot lists as '- HH:MM'; keep responses short.
Use the current date context provided in the system messages for all date calculations and references.
"""

FUNCTIONS = [
    {
        "name": "doctor_lookup",
        "description": "Find matching doctors for a condition and visit mode.",
        "parameters": {
            "type": "object",
            "properties": {
                "condition": {
                    "type": "string",
                    "enum": ["fever","headache","flu","eye_issue","skin_rash"]
                },
                "visit_mode": {
                    "type": "string",
                    "enum": ["online","inperson","any"],
                    "default": "any"
                }
            },
            "required": ["condition"]
        }
    },
    {
        "name": "availability_tool",
        "description": "List free appointment slots for a doctor on a date or date range.",
        "parameters": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "slot_minutes": {"type": "integer", "default": 30},
                "end_date": {"type": "string", "description": "Optional YYYY-MM-DD"}
            },
            "required": ["doctor_id", "date"]
        }
    },
    {
        "name": "appointment_book_tool",
        "description": "Book an appointment with the doctor.",
        "parameters": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM (local)"},
                "end": {"type": "string", "description": "YYYY-MM-DDTHH:MM (local)"},
                "patient_name": {"type": "string"},
                "patient_email": {"type": "string"},
                "visit_mode": {"type": "string", "enum": ["online", "inperson", "any"]},
                "condition": {"type": "string"},
                "patient_phone": {"type": "string"},
                "patient_age": {"type": "integer"},
                "patient_sex": {"type": "string"}
            },
            "required": ["doctor_id", "start", "end", "patient_name", "patient_email"]
        }
    },
    {
        "name": "now_tool",
        "description": "Get current date/time in clinic timezone for phrase parsing.",
        "parameters": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "list_appointments_tool",
        "description": "List upcoming appointments by doctor and patient email.",
        "parameters": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "patient_email": {"type": "string"},
                "window_days": {"type": "integer", "default": 30}
            },
            "required": ["patient_email"]
        }
    },
    {
        "name": "cancel_appointment_tool",
        "description": "Cancel an appointment by event_id for the patient.",
        "parameters": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "event_id": {"type": "string"},
                "patient_email": {"type": "string"},
                "notify_attendees": {"type": "boolean", "default": True}
            },
            "required": ["doctor_id", "event_id", "patient_email"]
        }
    },
    {
        "name": "reschedule_tool",
        "description": "Reschedule an appointment to a new start/end for the patient.",
        "parameters": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "event_id": {"type": "string"},
                "new_start": {"type": "string", "description": "YYYY-MM-DDTHH:MM (local)"},
                "new_end": {"type": "string", "description": "YYYY-MM-DDTHH:MM (local)"},
                "patient_email": {"type": "string"}
            },
            "required": ["doctor_id", "event_id", "new_start", "new_end", "patient_email"]
        }
    }
]

CLINIC_TZ = os.getenv("CLINIC_TIMEZONE", "Asia/Karachi")

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def contains_weekday_phrase(text: str) -> bool:
    t = (text or "").lower()
    if any(k in t for k in WEEKDAYS.keys()):
        return True
    if any(k in t for k in ["today", "tomorrow", "tmrw", "tmr", "tommorrow", "upcoming"]):
        return True
    return False


def next_weekday(target_idx: int, today: date | None = None) -> date:
    today = today or datetime.now(ZoneInfo(CLINIC_TZ)).date()
    delta = (target_idx - today.weekday()) % 7
    if delta == 0:
        delta = 7
    return today + timedelta(days=delta)


def to_iso_date_from_phrase(text: str, today: date | None = None) -> str | None:
    t = text.lower().strip()
    base_today = today or datetime.now(ZoneInfo(CLINIC_TZ)).date()
    if t in ("today",):
        return base_today.isoformat()
    if t in ("tomorrow",):
        return (base_today + timedelta(days=1)).isoformat()
    for key, idx in WEEKDAYS.items():
        if key in t:
            return next_weekday(idx, today=base_today).isoformat()
    # Already ISO?
    try:
        _ = datetime.fromisoformat(t)
        if len(t) == 10:
            return t
    except Exception:
        return None
    return None


def ensure_iso_date(input_value: str, fallback_text: str, today: date | None = None) -> str | None:
    # If already YYYY-MM-DD
    try:
        if len(input_value) == 10:
            datetime.fromisoformat(input_value)
            return input_value
    except Exception:
        pass
    # Try phrase using provided 'today'
    guess = to_iso_date_from_phrase(input_value, today=today)
    if guess:
        return guess
    # Try fallback text (e.g., user message)
    guess2 = to_iso_date_from_phrase(fallback_text, today=today)
    return guess2


class SessionState:
    def __init__(self) -> None:
        self.doctors_by_id: dict[str, dict] = {}
        self.doctors_by_name: dict[str, dict] = {}
        self.last_condition: str | None = None
        self.last_mode: str | None = None

    def update_from_doctors(self, doctors: list[dict]):
        self.doctors_by_id = {}
        self.doctors_by_name = {}
        for d in doctors:
            if not isinstance(d, dict):
                continue
            did = d.get("doctor_id")
            name = str(d.get("name", "")).lower().strip()
            if did:
                self.doctors_by_id[str(did)] = d
            if name:
                self.doctors_by_name[name] = d

    def resolve_doctor_id(self, args: dict, last_user_text: str | None = None) -> str | None:
        if "doctor_id" in args and args["doctor_id"] in self.doctors_by_id:
            return args["doctor_id"]
        name = (args.get("doctor_name") or args.get("name") or args.get("doctor") or "").lower().strip()
        if name and name in self.doctors_by_name:
            return self.doctors_by_name[name].get("doctor_id")
        if last_user_text:
            t = last_user_text.lower()
            # simple partial match on known doctor names
            for full_name, doc in self.doctors_by_name.items():
                parts = [p for p in full_name.split() if p]
                if any(p in t for p in parts):
                    return doc.get("doctor_id")
        if len(self.doctors_by_id) == 1:
            # Single result context
            return next(iter(self.doctors_by_id.keys()))
        return None


STATE = SessionState()

async def mcp_call(tool_name: str, args: dict):
    server_params = StdioServerParameters(
        command="python",
        args=[SERVER_PATH],
        env=os.environ.copy()
    )
    async with stdio_client(server_params) as (read_stream, write_stream):
        session = ClientSession(read_stream, write_stream)
        async with session:
            await session.initialize()
            result = await session.call_tool(name=tool_name, arguments=args)
            # Prefer structured content if present
            if getattr(result, "structuredContent", None) is not None:
                return result.structuredContent
            # Otherwise parse text content; if multiple items, aggregate JSON elements
            items = []
            if getattr(result, "content", None):
                for content_item in result.content:
                    if hasattr(content_item, 'text') and content_item.text:
                        try:
                            parsed = json.loads(content_item.text)
                            items.append(parsed)
                        except Exception:
                            items.append({"raw_text": content_item.text})
            if len(items) == 1:
                return items[0]
            if items:
                return items
            return {"ok": True}


def normalize_tool_args(fn: str, args: dict, last_user_text: str, last_assistant_text: str = "") -> dict:
    a = dict(args)
    if fn == "availability_tool":
        # Ensure doctor_id
        did = STATE.resolve_doctor_id(a, last_user_text=last_user_text)
        if did:
            a["doctor_id"] = did
        # Normalize date
        d = a.get("date")
        # Detect weekday phrase presence in user text
        has_weekday_phrase = any(k in last_user_text.lower() for k in WEEKDAYS.keys()) or any(w in last_user_text.lower() for w in ["today", "tomorrow"]) 
        if isinstance(d, str):
            # If user provided a weekday phrase, prefer recomputing from that phrase even if model provided ISO
            if has_weekday_phrase:
                guessed = ensure_iso_date("", last_user_text)
                if guessed:
                    a["date"] = guessed
                else:
                    fixed = ensure_iso_date(d, last_user_text)
                    if fixed:
                        a["date"] = fixed
            else:
                fixed = ensure_iso_date(d, last_user_text)
                if fixed:
                    a["date"] = fixed
        elif not d:
            # Try infer from user text first, then assistant hint (e.g., "Monday")
            guess = ensure_iso_date("", last_user_text)
            if not guess and last_assistant_text:
                guess = to_iso_date_from_phrase(last_assistant_text)
            if guess:
                a["date"] = guess
        # Default slot minutes
        a.setdefault("slot_minutes", 30)
    elif fn == "appointment_book_tool":
        did = STATE.resolve_doctor_id(a, last_user_text=last_user_text)
        if did:
            a["doctor_id"] = did
        # If only start given, compute end default 30m
        if a.get("start") and not a.get("end"):
            try:
                end_dt = datetime.fromisoformat(a["start"]).replace(second=0, microsecond=0) + timedelta(minutes=30)
                a["end"] = end_dt.strftime("%Y-%m-%dT%H:%M")
            except Exception:
                pass
        # Ensure minimal fields exist; model should fill name/email, but we won't inject fake data
    return a


def get_current_date_context() -> str:
    """Get current date context for the system prompt."""
    try:
        import pytz
        from datetime import datetime
        tz = pytz.timezone(CLINIC_TZ)
        now = datetime.now(tz)
        return f"Current date: {now.strftime('%A, %B %d, %Y')} ({now.strftime('%Y-%m-%d')})\nCurrent time: {now.strftime('%H:%M:%S')} ({now.tzinfo})\nToday is {now.strftime('%A')} (weekday index: {now.weekday()})"
    except Exception as e:
        return f"Current date: Unable to get date ({str(e)})"

def run_chat():
    if not HF_TOKEN:
        raise RuntimeError("HUGGINGFACE_API_TOKEN missing in .env")
    client = InferenceClient(token=HF_TOKEN)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.append({"role": "system", "content": get_current_date_context()})

    print("Assistant ready. Type your message (Ctrl+C to exit).\n")

    # Prefetch doctors
    try:
        init_doctors = asyncio.run(mcp_call("list_doctors", {}))
        payload = init_doctors.get("result") if isinstance(init_doctors, dict) else init_doctors
        if isinstance(payload, list): STATE.update_from_doctors(payload)
    except Exception: pass

    last_assistant_textual = ""

    while True:
        try: user_msg = input("You: ").strip()
        except (EOFError, KeyboardInterrupt): print("\nBye!"); break
        if not user_msg: continue

        today_override = None
        try:
            now_payload = asyncio.run(mcp_call("now_tool", {}))
            now_data = now_payload.get("result") if isinstance(now_payload, dict) else now_payload
            if isinstance(now_data, dict) and isinstance(now_data.get("date"), str):
                today_override = datetime.fromisoformat(now_data["date"]).date()
        except Exception: pass

        messages.append({"role": "user", "content": user_msg})

        nudge_attempts = 0
        while True:
            # --- Prepare prompt text for DeepSeek ---
            prompt_text = "\n".join([f"{m['role']}: {m['content']}" for m in messages])

            # Stream the assistant response
            print("Assistant: ", end="", flush=True)
            generated_text = ""
            resp = client.chat.completions.create(
                model=HF_MODEL,
                messages=messages
            )
            generated_text = resp['choices'][0]['message']['content']
            print(generated_text)

            messages.append({"role": "assistant", "content": generated_text})
            last_assistant_textual = generated_text
            break

if __name__ == "__main__":
    run_chat()