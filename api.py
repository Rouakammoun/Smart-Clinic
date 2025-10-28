from fastapi import FastAPI, HTTPException
from fastapi import UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import os, json, asyncio, uuid, re
from typing import Optional, Dict, Any
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession
import server as mcp_tools
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
# Ensure environment variables from .env are loaded for local/dev usage
load_dotenv()
SERVER_PATH = os.path.join(PROJECT_ROOT, "server.py")

app = FastAPI(title="Medbridge AI API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Serve the static UI at /ui (serve index.html on /ui/)
app.mount("/ui", StaticFiles(directory=os.path.join(PROJECT_ROOT, "static"), html=True), name="ui")

# Redirect root to chat UI by default
@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/ui/chat.html")


from huggingface_hub import InferenceClient
HF_TOKEN = os.getenv("HUGGINGFACE_API_TOKEN")
HF_MODEL = os.getenv("HF_MODEL", "deepseek-ai/DeepSeek-V3")
HF_VISION_MODEL = os.getenv("HF_VISION_MODEL", "Qwen/Qwen2-VL-7B-Instruct")
client = InferenceClient(token=HF_TOKEN)

SYSTEM_PROMPT = """You are Medbridge AI, a bilingual triage & booking assistant supporting English and French only.

MEDICAL TERMINOLOGY MAPPING (English & French):
- "skin specialist"/"dermatologue"/"spécialiste de la peau" = dermatologist (Dr. Diego - Dermatology)
- "dermatologist"/"dermatologue" = skin doctor (Dr. Diego - Dermatology)  
- "eye specialist"/"ophtalmologue"/"spécialiste des yeux" = ophthalmologist (Dr. Eric - Ophthalmology)
- "ophthalmologist"/"ophtalmologue" = eye doctor (Dr. Eric - Ophthalmology)
- "general doctor"/"médecin généraliste"/"docteur généraliste" = family physician (Dr. Ali - General Medicine)
- "family doctor"/"médecin de famille" = general practitioner (Dr. Ali - General Medicine)

MANDATORY DOCTOR DISPLAY FORMAT:
ALWAYS display doctors in this format: "Dr. [Name] - [Specialization]"
Examples:
- Dr. Eric - Ophthalmology
- Dr. Diego - Dermatology  
- Dr. Ali - General Medicine

Flow:
1) If user asks "any doctor available" or "what doctors do you have", call list_doctors to show ALL doctors with format "Dr. [Name] - [Specialization]";
2) If user asks about specific doctor by name, call doctor_lookup_by_name then doctor_weekly_availability;
3) If user has symptoms or asks for specialist type, infer likely condition and call doctor_lookup; 
4) offer to check availability; 5) call availability_tool and show slots;
6) when user picks a date, ALWAYS ask them to choose a specific time from slots;
7) collect required fields (name, email) and optional (phone, age, sex);
8) BEFORE BOOKING, present a COMPLETE Review with ALL collected information in this exact format:

Here's a summary of your appointment details (Voici un résumé des détails de votre rendez-vous):
- Doctor (Médecin): Dr. [Name] - [Specialization]
- Patient Name (Nom du patient): [name]
- Patient Email (Email du patient): [email]
- Patient Phone (Téléphone): [phone if provided]
- Patient Age (Âge): [age if provided]
- Patient Sex (Sexe): [sex if provided]
- Date (Date): [full date with day name]
- Time (Heure): [HH:MM]
- Mode (Mode): [online/in-person (en ligne/en personne)]
- Fee (Frais): PKR [amount]
- Clinic (Clinique): [location]

Then ask: "Please confirm to proceed with booking. Reply 'confirm' or 'yes' to book."
Only on explicit yes/confirm/book/go ahead call appointment_book_tool.

HARD REQUIREMENTS: 
- NEVER display doctor names without their specialization
- NEVER claim availability without availability_tool
- NEVER book without user confirmation and appointment_book_tool
STYLE: plain text; no asterisks; labeled lines; slot lists as '- HH:MM'; keep responses short.
LANGUAGE: Only respond in English or French. Adapt responses based on the language used by the user.

CRITICAL AVAILABILITY FORMAT - THIS IS MANDATORY:
When showing doctor availability, you MUST format it EXACTLY like this:
Tuesday, September 09
- 11:00
- 11:30
- 12:00
- 12:30
- 16:00
- 16:30
- 17:00
- 17:30

Thursday, September 11
- 11:00
- 11:30
- 12:00
- 12:30

NEVER put all time slots on the same line with hyphens. Each time slot must be on its own line with a dash prefix. This is MANDATORY formatting.
"""

# --- French-English medical terminology mapping ---
_FRENCH_MEDICAL_MAP = {
    "fièvre": "fever",
    "mal de tête": "headache",
    "migraine": "headache",
    "grippe": "flu",
    "toux": "cough",
    "problème oculaire": "eye_issue",
    "problème aux yeux": "eye_issue",
    "dermatologue": "dermatologist",
    "problème de peau": "skin_issue",
    "éruption cutanée": "skin_rash",
    "acné": "acne",
    "démangeaison": "itch",
    "allergie": "allergy",
    "médecin généraliste": "general_physician",
    "ophtalmologue": "ophthalmologist"
}

def _normalization_hint(text: str) -> str | None:
    t = (text or "").lower()
    hits = []
    for k, v in _FRENCH_MEDICAL_MAP.items():
        if k in t:
            hits.append(f"{k}→{v}")
    if hits:
        return "Normalization hint: interpret French medical terms as → " + ", ".join(sorted(set(hits)))
    return None

def _detect_language(text: str) -> str:
    """
    Detect if text is in French or English.
    Returns 'french' for French, 'english' for English.
    """
    # Check for French patterns and accented characters
    french_indicators = [
        "é", "è", "ê", "ë", "à", "â", "ù", "û", "ç", "î", "ï",
        "fièvre", "mal de tête", "grippe", "toux", "dermatologue",
        "ophtalmologue", "médecin", "rendez-vous", "docteur",
        "bonjour", "merci", "s'il vous plaît", "je", "vous", "le", "la", "les"
    ]
    
    text_lower = text.lower()
    french_count = sum(1 for indicator in french_indicators if indicator in text_lower)
    
    # If we find French indicators, classify as French
    if french_count > 0:
        return "french"
    
    # Default to English
    return "english"

def _translate_to_english(text: str) -> Dict[str, str]:
    """
    Translate French to English, or keep English as is.
    Only supports English and French.
    """
    detected_lang = _detect_language(text)
    
    if detected_lang == "english":
        return {"lang": "english", "english": text}
    
    # For French, translate to English
    try:
        prompt = (
            "Translate French text to English for medical scheduling context. "
            "Only translate if the text contains French words. "
            "If it's already English, return as is. "
            "Reply strictly as JSON: {\"lang\": \"french\" or \"english\", \"english\": \"<english_translation>\"}. "
            "Do not add any extra text.\n\n"
            f"Text: {text}"
        )
        resp = client.chat.completions.create(
            model=HF_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )
        data = json.loads(resp['choices'][0]['message']['content'])
        if isinstance(data, dict) and data.get("english"):
            return {"lang": str(data.get("lang") or "french"), "english": str(data.get("english"))}
    except Exception:
        pass
    
    # Fallback: treat as English
    return {"lang": "english", "english": text}


def _classify_intent_condition(text: str) -> str | None:
    """
    Use the LLM to map user text to medical conditions or doctor types.
    Returns the condition string or None.
    """
    try:
        prompt = (
            "Classify the user's health concern or doctor request into one of these categories:\n"
            "GENERAL CONDITIONS: fever, headache, flu, general, family_doctor, primary_care\n"
            "SKIN CONDITIONS: skin_rash, dermatology, skin_specialist, skin_problem, skin_disease, acne, eczema, psoriasis\n"
            "EYE CONDITIONS: eye_issue, ophthalmology, eye_specialist, eye_problem, vision\n\n"
            "Examples:\n"
            "- 'skin specialist' -> dermatology\n"
            "- 'dermatologist' -> dermatology\n"
            "- 'skin problem' -> skin_problem\n"
            "- 'eye doctor' -> eye_specialist\n"
            "- 'ophthalmologist' -> ophthalmology\n"
            "- 'general doctor' -> general_physician\n"
            "- 'family doctor' -> family_doctor\n\n"
            "If none applies, return none. Reply strictly as JSON: {\"condition\": \"<one_of_above_or_none>\"}.\n\n"
            f"User text: {text}"
        )
        resp = client.chat.completions.create(
            model=HF_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )
        data = json.loads(resp['choices'][0]['message']['content'])
        cond = (data or {}).get("condition")
        if isinstance(cond, str):
            cond = cond.strip().lower()
            valid_conditions = {
                "fever", "headache", "flu", "eye_issue", "skin_rash",
                "dermatology", "skin_specialist", "skin_problem", "skin_disease",
                "acne", "eczema", "psoriasis", "ophthalmology", "eye_specialist",
                "eye_problem", "vision", "general", "general_physician",
                "family_doctor", "primary_care"
            }
            if cond in valid_conditions:
                return cond
    except Exception:
        pass
    return None


def _detect_general_doctor_query(text: str) -> bool:
    """
    Detect if user is asking about general doctor availability.
    """
    text_lower = text.lower().strip()
    general_queries = [
        "is there any doctor available",
        "are there any doctors available",
        "what doctors are available",
        "which doctors are available",
        "show me all doctors",
        "list all doctors",
        "what doctors do you have",
        "available doctors",
        "doctors available",
        "any doctor",
        "any doctors"
    ]
    return any(query in text_lower for query in general_queries)

def _detect_doctor_name(text: str) -> str | None:
    """
    Detect if user is asking about a specific doctor by name.
    Returns doctor name if found, None otherwise.
    """
    # Check for known doctor names from the database
    known_doctors = ["Dr. Eric", "Dr. Diego", "Dr. Ali", "Eric", "Diego", "Ali"]
    
    text_lower = text.lower()
    for doctor in known_doctors:
        if doctor.lower() in text_lower:
            # Return the proper name format
            if doctor.startswith("Dr."):
                return doctor
            else:
                return f"Dr. {doctor}"
    
    # Check for general doctor patterns
    import re
    patterns = [
        r"dr\.?\s+(\w+)",
        r"doctor\s+(\w+)",
        r"(\w+)\s+doctor"
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            name = match.group(1).title()
            return f"Dr. {name}"
    
    return None

def _build_session_hint(state: Dict[str, Any]) -> str:
    parts: list[str] = []
    cond = state.get("last_condition")
    if isinstance(cond, str) and cond:
        parts.append(f"condition={cond}")
    doctors = state.get("last_doctor_options") or []
    if isinstance(doctors, list) and doctors:
        try:
            names = ", ".join([f"{d.get('name')}({d.get('doctor_id')})" for d in doctors if isinstance(d, dict)])
            parts.append(f"doctor_options=[{names}]")
        except Exception:
            pass
    avail = state.get("last_availability") or {}
    if isinstance(avail, dict) and avail.get("doctor_name") and avail.get("date"):
        parts.append(f"last_availability={avail.get('doctor_name')} on {avail.get('date')}")
    lang = state.get("lang")
    if isinstance(lang, str) and lang:
        parts.append(f"user_lang={lang}")
    summary = "; ".join(parts) or "none"
    return (
        "Session memory: " + summary + ". "
        "When helpful, begin your reply with a one-line recap of the plan before proceeding."
    )

def _get_current_date_context() -> str:
    """Get current date context for the system prompt."""
    try:
        import pytz
        from datetime import datetime
        tz = pytz.timezone("Asia/Karachi")
        now = datetime.now(tz)
        return f"Current date: {now.strftime('%A, %B %d, %Y')} ({now.strftime('%Y-%m-%d')})\nCurrent time: {now.strftime('%H:%M:%S')} ({now.tzinfo})\nToday is {now.strftime('%A')} (weekday index: {now.weekday()})"
    except Exception as e:
        return f"Current date: Unable to get date ({str(e)})"

def _force_proper_formatting(content: str) -> str:
    """
    Force proper formatting for availability information regardless of AI output.
    This is a post-processing step to ensure clean formatting.
    """
    if not content:
        return content
    
    # Pattern to match the messy format: "**Tuesday, September 09** - 11:00 - 11:30 - 12:00..."
    messy_pattern = r'\*\*([^*]+?)\*\*\s*-\s*([^*-]+?)(?=\*\*|$)'
    
    def replace_messy_format(match):
        date_part = match.group(1).strip()
        time_part = match.group(2).strip()
        
        # Extract individual time slots
        time_slots = re.findall(r'\d{1,2}:\d{2}', time_part)
        
        # Build properly formatted result
        result = f"{date_part}\n"
        for time_slot in time_slots:
            result += f"- {time_slot}\n"
        
        return result
    
    # Apply the formatting fix
    formatted_content = re.sub(messy_pattern, replace_messy_format, content)
    
    # Also handle cases where there might be multiple dates in sequence
    # Look for patterns like "Date: Tuesday, September 09 - 11:00 - 11:30..."
    date_pattern = r'Date:\s*([^-\n]+?)\s*-\s*([^D]+?)(?=Date:|$)'
    
    def replace_date_format(match):
        date_part = match.group(1).strip()
        time_part = match.group(2).strip()
        
        # Extract individual time slots
        time_slots = re.findall(r'\d{1,2}:\d{2}', time_part)
        
        # Build properly formatted result
        result = f"{date_part}\n"
        for time_slot in time_slots:
            result += f"- {time_slot}\n"
        
        return result
    
    formatted_content = re.sub(date_pattern, replace_date_format, formatted_content)
    
    return formatted_content

FUNCTIONS = [
    {
        "name": "doctor_lookup",
        "description": "Find matching doctors for a condition and visit mode.",
        "parameters": {
            "type": "object",
            "properties": {
                "condition": {"type": "string"},
                "visit_mode": {"type": "string", "enum": ["online", "inperson", "any"], "default": "any"}
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
                "date": {"type": "string"},
                "slot_minutes": {"type": "integer", "default": 30},
                "end_date": {"type": "string"}
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
                "start": {"type": "string"},
                "end": {"type": "string"},
                "patient_name": {"type": "string"},
                "patient_email": {"type": "string"},
                "visit_mode": {"type": "string"},
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
                "new_start": {"type": "string"},
                "new_end": {"type": "string"},
                "patient_email": {"type": "string"}
            },
            "required": ["doctor_id", "event_id", "new_start", "new_end", "patient_email"]
        }
    },
    {
        "name": "doctor_lookup_by_name",
        "description": "Find a doctor by name and return their details.",
        "parameters": {
            "type": "object",
            "properties": {
                "doctor_name": {"type": "string"}
            },
            "required": ["doctor_name"]
        }
    },
    {
        "name": "doctor_weekly_availability",
        "description": "Get doctor availability for the next N days based on routine schedule + Google Calendar.",
        "parameters": {
            "type": "object",
            "properties": {
                "doctor_name": {"type": "string"},
                "days": {"type": "integer", "default": 7},
                "slot_minutes": {"type": "integer", "default": 30}
            },
            "required": ["doctor_name"]
        }
    },
    {
        "name": "list_doctors",
        "description": "List all available doctors with their specializations, experience, and fees.",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
]

SESSIONS: Dict[str, Dict[str, Any]] = {}

# In-process tool map
TOOL_MAP = {
    "doctor_lookup": mcp_tools.doctor_lookup,
    "availability_tool": mcp_tools.availability_tool,
    "appointment_book_tool": mcp_tools.appointment_book_tool,
    "list_appointments_tool": mcp_tools.list_appointments_tool,
    "cancel_appointment_tool": mcp_tools.cancel_appointment_tool,
    "reschedule_tool": mcp_tools.reschedule_tool,
    "doctor_lookup_by_name": mcp_tools.doctor_lookup_by_name,
    "doctor_weekly_availability": mcp_tools.doctor_weekly_availability,
    "list_doctors": mcp_tools.list_doctors,
    "now_tool": mcp_tools.now_tool,
}

async def mcp_call(tool_name: str, args: dict):
    func = TOOL_MAP.get(tool_name)
    if func:
        try:
            # Run the sync tool in a thread to avoid blocking the event loop
            return await asyncio.to_thread(func, **args)
        except Exception as e:
            # Fall back to stdio approach if direct call fails for any reason
            pass
    # Fallback: spawn MCP server via stdio
    params = StdioServerParameters(command="python", args=[SERVER_PATH], env=os.environ.copy())
    async with stdio_client(params) as (r, w):
        session = ClientSession(r, w)
        async with session:
            await session.initialize()
            res = await session.call_tool(name=tool_name, arguments=args)
            if getattr(res, "structuredContent", None) is not None:
                payload = res.structuredContent
            else:
                items = []
                if getattr(res, "content", None):
                    for c in res.content:
                        if getattr(c, "text", None):
                            try:
                                items.append(json.loads(c.text))
                            except Exception:
                                items.append({"raw_text": c.text})
                payload = items[0] if len(items) == 1 else (items or {"ok": True})
            return payload

class ChatIn(BaseModel):
    session_id: Optional[str] = None
    user: str
    language: Optional[str] = "en"

@app.post("/chat")
async def chat(body: ChatIn):
    if not HF_TOKEN:
        raise HTTPException(status_code=500, detail="HUGGINGFACE_API_TOKEN missing")
    
    session_id = body.session_id or str(uuid.uuid4())
    session = SESSIONS.setdefault(session_id, {"messages": [{"role": "system", "content": SYSTEM_PROMPT}]})
    messages = session["messages"]

    # Inject current date context
    date_context = _get_current_date_context()
    messages.append({"role": "system", "content": date_context})

    # Inject normalization hint if french tokens detected
    hint = _normalization_hint(body.user)
    if hint:
        messages.append({"role": "system", "content": hint})

    # Session memory hint
    session_hint = _build_session_hint(session)
    messages.append({"role": "system", "content": session_hint})

    # Translate user message to English
    translated_user_message = _translate_to_english(body.user)
    messages.append({
        "role": "system",
        "content": f"User message in {translated_user_message['lang']}: {body.user}\n"
                   f"English translation: {translated_user_message['english']}"
    })

    # Intent classification hint
    _intent = _classify_intent_condition(translated_user_message['english'])
    if _intent:
        messages.append({
            "role": "system",
            "content": f"Intent condition: {_intent}. If symptoms are present, call doctor_lookup with condition='{_intent}'. "
                       "When showing results, ALWAYS display as 'Dr. [Name] - [Specialization]' format."
        })
        session["last_condition"] = _intent

    # Update session with language
    session["lang"] = translated_user_message['lang']

    # General doctor availability detection
    if _detect_general_doctor_query(translated_user_message['english']):
        messages.append({
            "role": "system",
            "content": "User is asking about general doctor availability. Call list_doctors to show all available doctors. "
                       "MANDATORY: Display each doctor as 'Dr. [Name] - [Specialization]' with experience, fees, and schedule."
        })
        session["last_query_type"] = "general_doctors"

    # Doctor name detection
    detected_doctor = _detect_doctor_name(translated_user_message['english'])
    if detected_doctor:
        messages.append({
            "role": "system",
            "content": f"User asking about specific doctor: {detected_doctor}. Call doctor_lookup_by_name with doctor_name='{detected_doctor}' "
                       "then doctor_weekly_availability to show schedule. Always display as 'Dr. [Name] - [Specialization]'."
        })
        session["last_doctor_query"] = detected_doctor

    # Reply language hint
    if translated_user_message['lang'] == 'french':
        messages.append({"role": "system", "content": "Respond in French. Keep tool arguments in English."})
    else:
        messages.append({"role": "system", "content": "Respond in English. Keep tool arguments in English."})

    # Strict tool usage
    messages.append({
        "role": "system",
        "content": "For any slots/dates/times, call availability_tool and only present what it returns. "
                   "Use the current date context for calculations."
    })

    # Fallback rule
    messages.append({"role": "system", "content": "If doctor_lookup returns empty, suggest a General Physician (Dr. Ali) instead."})

    # Booking summary hint
    messages.append({
        "role": "system",
        "content": "CRITICAL: Before booking, display a COMPLETE summary showing all patient info, doctor details, date, time, visit mode, fee, and clinic. "
                   "If visit mode is not provided, ASK the user to choose. NEVER skip the summary. NEVER book without confirmation."
    })

    # Add user message
    messages.append({"role": "user", "content": body.user})

    # Build single prompt for DeepSeek
    prompt = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in messages])

    try:
        resp = client.chat.completions.create(
            model=HF_MODEL,
            messages=messages
        )
        reply = resp['choices'][0]['message']['content']
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DeepSeek API error: {str(e)}")

    return {"session_id": session_id, "reply": reply}

    # Tool loop with retry logic
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                tools=[{"type": "function", "function": f} for f in FUNCTIONS],
                tool_choice="auto",
                temperature=0.3,
            )
            msg = resp.choices[0].message
            if msg.tool_calls:
                messages.append({
                    "role": "assistant",
                    "tool_calls": [tc.model_dump() if hasattr(tc, 'model_dump') else {
                        "id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                    } for tc in msg.tool_calls]
                })
                for tc in msg.tool_calls:
                    fn = tc.function.name
                    try:
                        raw_args = json.loads(tc.function.arguments or "{}")
                    except Exception:
                        raw_args = {}
                    try:
                        result_json = await mcp_call(fn, raw_args)
                        # Update session state based on tool results
                        if fn == "doctor_lookup" and isinstance(result_json, dict):
                            doctors = result_json.get("result", [])
                            if isinstance(doctors, list):
                                session["last_doctor_options"] = doctors
                        elif fn == "availability_tool" and isinstance(result_json, dict):
                            avail_data = result_json.get("result", {})
                            if isinstance(avail_data, dict):
                                session["last_availability"] = {
                                    "doctor_name": avail_data.get("doctor_name"),
                                    "date": avail_data.get("date"),
                                    "slots": avail_data.get("slots", [])
                                }
                    except Exception as e:
                        result_json = {"error": str(e)}
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": fn,
                        "content": json.dumps(result_json)
                    })
                continue
            # final
            content = msg.content or ""
            # Apply forced formatting fix
            content = _force_proper_formatting(content)
            messages.append({"role": "assistant", "content": content})
            return {"session_id": session_id, "assistant": content}
            
        except Exception as e:
            retry_count += 1
            if retry_count >= max_retries:
                raise HTTPException(status_code=500, detail=f"Failed after {max_retries} retries: {str(e)}")
            # Wait before retry with exponential backoff
            await asyncio.sleep(2 ** retry_count)

@app.post("/chat/stream")
async def chat_stream(body: ChatIn):
    """Streaming chat endpoint using DeepSeek-V3 (simulated streaming)"""
    if not HF_TOKEN:
        raise HTTPException(status_code=500, detail="HUGGINGFACE_API_TOKEN missing")
    
    session_id = body.session_id or str(uuid.uuid4())
    session = SESSIONS.setdefault(session_id, {"messages": [{"role": "system", "content": SYSTEM_PROMPT}]})
    messages = session["messages"]

    # --- Inject system hints, translation, intent, doctor info ---
    date_context = _get_current_date_context()
    messages.append({"role": "system", "content": date_context})

    hint = _normalization_hint(body.user)
    if hint:
        messages.append({"role": "system", "content": hint})

    session_hint = _build_session_hint(session)
    messages.append({"role": "system", "content": session_hint})

    translated_user_message = _translate_to_english(body.user)
    messages.append({"role": "system", "content": f"User message in {translated_user_message['lang']}: {body.user}\nEnglish translation: {translated_user_message['english']}"})

    _intent = _classify_intent_condition(translated_user_message['english'])
    if _intent:
        messages.append({"role": "system", "content": f"Intent condition: {_intent}. When showing results, ALWAYS display as 'Dr. [Name] - [Specialization]' format."})
        session["last_condition"] = _intent

    session["lang"] = translated_user_message['lang']

    if _detect_general_doctor_query(translated_user_message['english']):
        messages.append({"role": "system", "content": "User asking about general doctor availability. Display all doctors as 'Dr. [Name] - [Specialization]' with experience, fees, schedule."})
        session["last_query_type"] = "general_doctors"

    detected_doctor = _detect_doctor_name(translated_user_message['english'])
    if detected_doctor:
        messages.append({"role": "system", "content": f"User asking about specific doctor: {detected_doctor}. Show schedule for next 7 days."})
        session["last_doctor_query"] = detected_doctor

    if translated_user_message['lang'] == 'french':
        messages.append({"role": "system", "content": "Respond in French. Keep tool arguments in English."})
    else:
        messages.append({"role": "system", "content": "Respond in English. Keep tool arguments in English."})

    messages.append({"role": "system", "content": "For any slots/dates/times, call availability_tool and only present what it returns."})
    messages.append({"role": "system", "content": "If doctor_lookup returns empty, suggest a General Physician (Dr. Ali)."})
    messages.append({"role": "system", "content": "CRITICAL: Before booking, display a COMPLETE summary showing all patient info, doctor details, date, time, visit mode, fee, clinic. NEVER book without confirmation."})

    messages.append({"role": "user", "content": body.user})

    # Build single prompt for DeepSeek
    prompt = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in messages])

    async def generate():
      try:
        # --- Step 1: detect tool calls (doctor_lookup, availability_tool) ---
        tool_results = {}
        for f in FUNCTIONS:  # FUNCTIONS = list of MCP tool definitions
            fn_name = f.get("name")
            # Example: detect if function should be called based on user intent
            if fn_name == "doctor_lookup" and session.get("last_condition"):
                args = {"condition": session["last_condition"]}
            elif fn_name == "availability_tool" and session.get("last_doctor_query"):
                args = {"doctor_name": session["last_doctor_query"], "date": _get_current_date_context()}
            else:
                continue

            try:
                result = await mcp_call(fn_name, args)
                tool_results[fn_name] = result
                # Inject tool result back into session/messages
                messages.append({
                    "role": "tool",
                    "name": fn_name,
                    "content": json.dumps(result)
                })
            except Exception as e:
                messages.append({
                    "role": "tool",
                    "name": fn_name,
                    "content": json.dumps({"error": str(e)})
                })

        # --- Step 2: build prompt for DeepSeek ---
        prompt = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in messages])

        # --- Step 3: call DeepSeek ---
        resp = client.chat.completions.create(
            model=HF_MODEL,
            messages=messages
        )
        content = resp['choices'][0]['message']['content']
        content = _force_proper_formatting(content)
        messages.append({"role": "assistant", "content": content})

        # --- Step 4: simulate streaming ---
        words = content.split()
        for word in words:
            yield f"data: {json.dumps({'content': word + ' ', 'session_id': session_id})}\n\n"
            await asyncio.sleep(0.05)

        yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"

      except Exception as e:
        yield f"data: {json.dumps({'error': str(e), 'session_id': session_id})}\n\n"

    return StreamingResponse(generate(), media_type="text/plain")

class DoctorLookupIn(BaseModel):
    condition: str
    visit_mode: Optional[str] = "any"

class AvailabilityIn(BaseModel):
    doctor_id: str
    date: str
    slot_minutes: Optional[int] = 30
    end_date: Optional[str] = None

class BookIn(BaseModel):
    doctor_id: str
    start: str
    end: str
    patient_name: str
    patient_email: str
    visit_mode: Optional[str] = None
    condition: Optional[str] = None
    patient_phone: Optional[str] = None
    patient_age: Optional[int] = None
    patient_sex: Optional[str] = None
    send_invitations: Optional[bool] = False
    create_meet: Optional[bool] = False

class ListApptsIn(BaseModel):
    patient_email: str
    doctor_id: Optional[str] = None
    window_days: Optional[int] = 30

class CancelIn(BaseModel):
    doctor_id: str
    event_id: str
    patient_email: str
    notify_attendees: Optional[bool] = True

class RescheduleIn(BaseModel):
    new_start: str
    new_end: str
    patient_email: str

class RehydrateIn(BaseModel):
    session_id: str
    messages: list[dict]

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/doctor-lookup")
async def doctor_lookup(body: DoctorLookupIn):
    return await mcp_call("doctor_lookup", body.model_dump())

@app.post("/availability")
async def availability(body: AvailabilityIn):
    return await mcp_call("availability_tool", body.model_dump())

@app.post("/book")
async def book(body: BookIn):
    return await mcp_call("appointment_book_tool", body.model_dump())

@app.post("/appointments")
async def list_appts(body: ListApptsIn):
    return await mcp_call("list_appointments_tool", body.model_dump())

@app.post("/cancel")
async def cancel(body: CancelIn):
    return await mcp_call("cancel_appointment_tool", body.model_dump())

@app.post("/reschedule")
async def reschedule(body: RescheduleIn):
    return await mcp_call("reschedule_tool", body.model_dump())

@app.post("/rehydrate")
async def rehydrate(body: RehydrateIn):
    # Rebuild session messages with system prompt and provided history
    session = SESSIONS.setdefault(body.session_id, {"messages": [{"role": "system", "content": SYSTEM_PROMPT}]})
    # Filter only valid roles/content
    cleaned = []
    for m in body.messages:
        try:
            role = m.get("role")
            content = m.get("content")
            if role in {"user", "assistant", "system"} and isinstance(content, str) and content.strip():
                cleaned.append({"role": role, "content": content})
        except Exception:
            continue
    # Keep only last 50 messages to bound memory
    session["messages"] = session["messages"][:1] + cleaned[-50:]
    return {"ok": True, "session_id": body.session_id, "count": len(cleaned)}


@app.post("/explain-chart-image")
async def explain_chart_image(file: UploadFile = File(...)):
    """
    Explain a chart/curve from an uploaded image using a vision-capable model.
    1) Try multimodal chat on a vision model (e.g., Qwen2-VL).
    2) If not supported, fall back: caption image -> analyze caption via text model.
    Returns concise explanation with trends, peaks, and anomalies.
    """
    if not HF_TOKEN:
        raise HTTPException(status_code=500, detail="HUGGINGFACE_API_TOKEN missing")
    try:
        content = await file.read()
        import base64
        b64 = base64.b64encode(content).decode("utf-8")
        # Attempt multimodal chat
        try:
            vision_messages = [
                {"role": "system", "content": "You are an expert at reading charts. Be precise and concise."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Explain this chart: trend, peaks, anomalies, and 2-3 insights."},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                },
            ]
            resp = InferenceClient(token=HF_TOKEN).chat.completions.create(
                model=HF_VISION_MODEL,
                messages=vision_messages,
            )
            explanation = resp['choices'][0]['message']['content']
            return {"explanation": explanation, "model": HF_VISION_MODEL}
        except Exception:
            # Fallback: basic captioning + text analysis
            try:
                caption = InferenceClient(token=HF_TOKEN).image_to_text(content)
            except Exception as e2:
                raise HTTPException(status_code=500, detail=f"Vision fallback failed: {str(e2)}")
            analysis_messages = [
                {"role": "system", "content": "You analyze chart descriptions into insights."},
                {"role": "user", "content": f"Image description: {caption}. Explain trend, peaks, anomalies, and provide 2-3 insights."},
            ]
            resp2 = client.chat.completions.create(model=HF_MODEL, messages=analysis_messages)
            explanation2 = resp2['choices'][0]['message']['content']
            return {"explanation": explanation2, "model": HF_MODEL, "fallback": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Chart image explanation failed: {str(e)}")


def main():
    import uvicorn, webbrowser, threading, time
    port = int(os.getenv("PORT", "8000"))
    # Default to chat page
    url = os.getenv("APP_OPEN_URL", f"http://localhost:{port}/ui/chat.html")
    if os.getenv("OPEN_BROWSER", "1") != "0":
        def _open():
            time.sleep(1.5)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()
    uvicorn.run("api:app", host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
