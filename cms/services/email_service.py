"""Email service for sending welcome emails to new users."""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from cms.config import get_settings

_log = logging.getLogger(__name__)


def send_welcome_email(
    to_email: str,
    display_name: str,
    temp_password: str,
    login_url: str | None = None,
) -> bool:
    """Send a welcome email with temporary credentials.

    Returns True if sent successfully, False if SMTP is not configured or fails.
    """
    settings = get_settings()

    if not settings.smtp_host or not settings.smtp_from_email:
        _log.warning("SMTP not configured — skipping welcome email to %s", to_email)
        return False

    login_url = login_url or settings.base_url or "http://localhost:8000"
    if not login_url.endswith("/login"):
        login_url = login_url.rstrip("/") + "/login"

    subject = "Welcome to Agora CMS"
    greeting = display_name or to_email

    html_body = f"""\
<html>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #333; max-width: 600px; margin: 0 auto;">
    <div style="background: #1a1a2e; color: #e0e0e0; padding: 2rem; border-radius: 8px;">
        <h1 style="color: #7c83ff; margin-top: 0;">Welcome to Agora CMS</h1>
        <p>Hi {greeting},</p>
        <p>An account has been created for you on Agora CMS. Here are your sign-in credentials:</p>
        <div style="background: #16213e; border: 1px solid #0f3460; border-radius: 6px; padding: 1rem; margin: 1.5rem 0;">
            <p style="margin: 0.25rem 0;"><strong>Email:</strong> <code style="background: #0f3460; padding: 2px 6px; border-radius: 3px;">{to_email}</code></p>
            <p style="margin: 0.25rem 0;"><strong>Temporary Password:</strong> <code style="background: #0f3460; padding: 2px 6px; border-radius: 3px;">{temp_password}</code></p>
        </div>
        <p>You will be asked to set a new password on your first sign-in.</p>
        <p style="margin-top: 1.5rem;">
            <a href="{login_url}" style="background: #7c83ff; color: #fff; padding: 0.6rem 1.5rem; border-radius: 4px; text-decoration: none; font-weight: 600;">Sign In</a>
        </p>
        <hr style="border: none; border-top: 1px solid #0f3460; margin: 2rem 0;">
        <p style="font-size: 0.85rem; color: #888;">This is an automated message from Agora CMS. Do not reply to this email.</p>
    </div>
</body>
</html>"""

    text_body = (
        f"Welcome to Agora CMS\n\n"
        f"Hi {greeting},\n\n"
        f"An account has been created for you.\n\n"
        f"Email: {to_email}\n"
        f"Temporary Password: {temp_password}\n\n"
        f"Sign in at: {login_url}\n"
        f"You will be asked to set a new password on your first sign-in.\n"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from_email
    msg["To"] = to_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        if settings.smtp_use_tls:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port)
            server.starttls()
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port)

        if settings.smtp_username and settings.smtp_password:
            server.login(settings.smtp_username, settings.smtp_password)

        server.sendmail(settings.smtp_from_email, [to_email], msg.as_string())
        server.quit()
        _log.info("Welcome email sent to %s", to_email)
        return True
    except Exception as e:
        _log.error("Failed to send welcome email to %s: %s", to_email, e)
        return False
