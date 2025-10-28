import smtplib

# Email credentials (replace with your own)
EMAIL = "kammounroua.2002@gmail.com"
APP_PASSWORD = "snfy jrye cilf edwd"  # 16-character App Password

try:
    # Connect to Gmail's SMTP server
    server = smtplib.SMTP("smtp.gmail.com", 587)
    server.starttls()
    server.login(EMAIL, APP_PASSWORD)
    print("✅ Login success!")

    # Test sending an email
    msg = "Subject: Test Email\n\nThis is a test email from MedBridge AI."
    server.sendmail(EMAIL, EMAIL, msg)
    print("📩 Test email sent successfully!")

except Exception as e:
    print("❌ Login or send failed:", e)

finally:
    server.quit()
