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


def _send_email(smtp_cfg: dict, to_email: str, subject: str, html_body: str, text_body: str) -> tuple[bool, str]:
    """Send an email using the given SMTP config dict. Returns (success, error_message)."""
    if not smtp_cfg.get("host") or not smtp_cfg.get("from_email"):
        msg = "SMTP not configured — skipping email"
        _log.warning("%s to %s", msg, to_email)
        return False, msg

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
        return True, ""
    except Exception as e:
        error = str(e)
        _log.error("Failed to send email to %s: %s", to_email, error)
        return False, error


def _create_notification_sync(level: str, title: str, message: str, details: dict | None = None):
    """Create a system notification using a synchronous DB connection.

    Used from background tasks that run in a thread pool.
    """
    from cms.database import _engine
    if _engine is None:
        _log.warning("No DB engine — cannot create notification")
        return
    from sqlalchemy.orm import Session
    from cms.models.notification import Notification

    # Create a sync connection from the async engine's pool
    sync_engine = _engine.sync_engine
    try:
        with Session(sync_engine) as session:
            notif = Notification(
                scope="system",
                level=level,
                title=title,
                message=message,
                details=details,
            )
            session.add(notif)
            session.commit()
    except Exception as exc:
        _log.error("Failed to create notification: %s", exc)


def send_welcome_email_sync(
    smtp_cfg: dict,
    to_email: str,
    display_name: str,
    temp_password: str,
    setup_url: str,
) -> bool:
    """Send a welcome email synchronously (for use in BackgroundTasks)."""
    greeting = display_name or to_email

    html_body = f"""\
<html>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #333; max-width: 600px; margin: 0 auto;">
    <div style="background: #1a1a2e; color: #e0e0e0; padding: 2rem; border-radius: 8px;">
        <h1 style="color: #7c83ff; margin-top: 0;">Welcome to Agora CMS</h1>
        <p>Hi {greeting},</p>
        <p>An account has been created for you on Agora CMS.</p>
        <p>Click the button below to set your password and get started:</p>
        <p style="margin-top: 1.5rem;">
            <a href="{setup_url}" style="background: #7c83ff; color: #fff; padding: 0.6rem 1.5rem; border-radius: 4px; text-decoration: none; font-weight: 600;">Set Up My Account</a>
        </p>
        <p style="margin-top: 1.5rem; font-size: 0.9rem; color: #aaa;">If the button doesn't work, copy and paste this link into your browser:</p>
        <p style="font-size: 0.85rem; word-break: break-all;"><a href="{setup_url}" style="color: #7c83ff;">{setup_url}</a></p>
        <hr style="border: none; border-top: 1px solid #0f3460; margin: 2rem 0;">
        <p style="font-size: 0.85rem; color: #888;">This link is single-use and will expire once you set your password.</p>
        <p style="font-size: 0.85rem; color: #888;">This is an automated message from Agora CMS. Do not reply to this email.</p>
    </div>
</body>
</html>"""

    text_body = (
        f"Welcome to Agora CMS\n\n"
        f"Hi {greeting},\n\n"
        f"An account has been created for you.\n\n"
        f"Set up your account by visiting:\n{setup_url}\n\n"
        f"This link is single-use and will expire once you set your password.\n"
    )

    ok, _ = _send_email(smtp_cfg, to_email, "Welcome to Agora CMS", html_body, text_body)
    return ok


def send_welcome_email_background(
    smtp_cfg: dict,
    to_email: str,
    display_name: str,
    temp_password: str,
    setup_url: str,
) -> None:
    """Send welcome email and create a notification on failure."""
    ok = send_welcome_email_sync(
        smtp_cfg=smtp_cfg,
        to_email=to_email,
        display_name=display_name,
        temp_password=temp_password,
        setup_url=setup_url,
    )
    if not ok:
        _create_notification_sync(
            level="error",
            title="Welcome email failed",
            message=f"Failed to send welcome email to {to_email}. "
                    "The user account was created but the activation link was not delivered. "
                    "Use the Resend Invite button on the Users page to retry.",
            details={"to_email": to_email, "display_name": display_name},
        )


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
        ok, error = _send_email(smtp_cfg, test_to_email, "Agora CMS — SMTP Test", html_body, text_body)
        if ok:
            return True, "Test email sent successfully"
        return False, error or "SMTP not configured (host or from_email missing)"
    except Exception as e:
        return False, str(e)
