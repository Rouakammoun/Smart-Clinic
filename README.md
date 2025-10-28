# Smart-Clinic

A modern, AI-powered medical clinic management system with integrated appointment scheduling and multilingual support.

## 🌟 Features

- **AI-Powered Triage Assistant**: Bilingual support for English and Roman Urdu
- **Smart Appointment Scheduling**: Automated booking system integrated with Google Calendar
- **Doctor Management**: Comprehensive doctor profiles and availability management
- **Real-time Chat Interface**: User-friendly web interface for patient interactions
- **Multi-specialty Support**: Handles various medical specialties with smart routing

## 🛠️ Technology Stack

- **Backend**: FastAPI (Python)
- **AI Models**:
  - DeepSeek-V3 for text processing
  - Qwen2-VL-7B-Instruct for vision tasks
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

```env
HUGGINGFACE_API_TOKEN=your_token_here
HF_MODEL=deepseek-ai/DeepSeek-V3
CLINIC_TIMEZONE=Asia/Karachi
```

4. Configure the `service-account.json` for Google Calendar integration

## 🚀 Running the Application

1. Start the server:

```bash
uvicorn api:app --reload
```

2. Access the web interface at:

```
http://localhost:8000
```

## 🏥 Project Structure

```
Smart-Clinic/
├── api.py              # FastAPI backend server
├── client.py           # AI client implementation
├── server.py           # MCP server implementation
├── requirements.txt    # Python dependencies
├── service-account.json# Google Calendar credentials
├── data/
│   └── doctors.json    # Doctor profiles and schedules
└── static/
    └── chat.html       # Web interface
```

## 💻 Features in Detail

### AI Triage Assistant

- Multilingual support (English and Roman Urdu)
- Smart specialty routing
- Medical terminology mapping

### Doctor Management

- Detailed doctor profiles
- Specialized fee structures
- Flexible scheduling
- Location-based services

### Appointment System

- Real-time availability checking
- Google Calendar integration
- Automatic confirmation
- Schedule management

## 👥 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.


