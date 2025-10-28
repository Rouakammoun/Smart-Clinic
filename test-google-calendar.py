from google.oauth2 import service_account

SERVICE_ACCOUNT_FILE = 'service-account.json'
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

try:
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    print("✅ Service account is valid!")
except Exception as e:
    print("❌ Failed to load credentials:", e)
