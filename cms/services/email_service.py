"""Email service for sending welcome emails to new users."""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import (
    SETTING_SMTP_FROM_EMAIL,
    SETTING_SMTP_HOST,
    SETTING_SMTP_PASSWORD,
    SETTING_SMTP_PORT,

    SETTING_SMTP_USERNAME,
    get_setting,
)

_log = logging.getLogger(__name__)


async def get_smtp_settings(db: AsyncSession) -> dict:
    """Read SMTP configuration from the database."""
    return {
        "host": await get_setting(db, SETTING_SMTP_HOST),
        "port": int(await get_setting(db, SETTING_SMTP_PORT) or "587"),
        "username": await get_setting(db, SETTING_SMTP_USERNAME),
        "password": await get_setting(db, SETTING_SMTP_PASSWORD),
        "from_email": await get_setting(db, SETTING_SMTP_FROM_EMAIL),
        "use_tls": True,
    }


def _send_email(smtp_cfg: dict, to_email: str, subject: str, html_body: str, text_body: str) -> bool:
    """Send an email using the given SMTP config dict. Returns True on success."""
    if not smtp_cfg.get("host") or not smtp_cfg.get("from_email"):
        _log.warning("SMTP not configured — skipping email to %s", to_email)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_cfg["from_email"]
    msg["To"] = to_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        if smtp_cfg.get("use_tls", True):
            server = smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"])
            server.starttls()
        else:
            server = smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"])

        if smtp_cfg.get("username") and smtp_cfg.get("password"):
            server.login(smtp_cfg["username"], smtp_cfg["password"])

        server.sendmail(smtp_cfg["from_email"], [to_email], msg.as_string())
        server.quit()
        _log.info("Email sent to %s: %s", to_email, subject)
        return True
    except Exception as e:
        _log.error("Failed to send email to %s: %s", to_email, e)
        return False


def send_welcome_email_sync(
    smtp_cfg: dict,
    to_email: str,
    display_name: str,
    temp_password: str,
    login_url: str,
) -> bool:
    """Send a welcome email synchronously (for use in BackgroundTasks)."""
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

    return _send_email(smtp_cfg, to_email, "Welcome to Agora CMS", html_body, text_body)


def test_smtp_connection(smtp_cfg: dict, test_to_email: str) -> tuple[bool, str]:
    """Test SMTP connection by sending a test email. Returns (success, message)."""
    html_body = """\
<html>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #333; max-width: 600px; margin: 0 auto;">
    <div style="background: #1a1a2e; color: #e0e0e0; padding: 2rem; border-radius: 8px;">
        <h1 style="color: #7c83ff; margin-top: 0;">SMTP Test</h1>
        <p>This is a test email from Agora CMS to verify your SMTP configuration is working correctly.</p>
        <p style="color: #4caf50; font-weight: 600;">✓ If you received this email, your SMTP settings are configured correctly.</p>
    </div>
</body>
</html>"""

    text_body = "SMTP Test\n\nThis is a test email from Agora CMS.\nIf you received this, SMTP is configured correctly."

    try:
        ok = _send_email(smtp_cfg, test_to_email, "Agora CMS — SMTP Test", html_body, text_body)
        if ok:
            return True, "Test email sent successfully"
        return False, "SMTP not configured (host or from_email missing)"
    except Exception as e:
        return False, str(e)
