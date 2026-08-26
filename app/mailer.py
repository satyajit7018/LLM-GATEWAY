"""Outbound email for password resets (hybrid build, Phase 4).

Uses stdlib smtplib — no extra dependency. If SMTP_HOST/SMTP_USER/SMTP_PASS
aren't configured, mail is never fabricated or silently dropped: the reset
link is logged to the server console instead, which is fine for local/demo use
but is NOT a substitute for real email delivery in any public deployment.
"""
import logging
import smtplib
from email.message import EmailMessage

from . import config

log = logging.getLogger("llm_gateway.mailer")


def smtp_configured() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASS)


def send_reset_email(to_email: str, reset_link: str):
    """Send (or, without SMTP configured, log) a password-reset email."""
    if not smtp_configured():
        log.warning("SMTP not configured — password reset link for %s: %s", to_email, reset_link)
        print(f"[mailer] SMTP not configured. Reset link for {to_email}: {reset_link}")
        return

    msg = EmailMessage()
    msg["Subject"] = "Reset your LLM Gateway password"
    msg["From"] = config.SMTP_FROM
    msg["To"] = to_email
    msg.set_content(
        "We received a request to reset your LLM Gateway password.\n\n"
        f"Reset it here (expires in 1 hour): {reset_link}\n\n"
        "If you didn't request this, you can safely ignore this email."
    )
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as smtp:
        smtp.starttls()
        smtp.login(config.SMTP_USER, config.SMTP_PASS)
        smtp.send_message(msg)


def send_verification_email(to_email: str, verify_link: str):
    """Send (or, without SMTP configured, log) an email-verification link."""
    if not smtp_configured():
        log.warning("SMTP not configured — verification link for %s: %s", to_email, verify_link)
        print(f"[mailer] SMTP not configured. Verification link for {to_email}: {verify_link}")
        return

    msg = EmailMessage()
    msg["Subject"] = "Verify your LLM Gateway email"
    msg["From"] = config.SMTP_FROM
    msg["To"] = to_email
    msg.set_content(
        "Confirm this is your email address to finish setting up your LLM Gateway account.\n\n"
        f"Verify it here (expires in 24 hours): {verify_link}\n\n"
        "If you didn't create this account, you can safely ignore this email."
    )
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as smtp:
        smtp.starttls()
        smtp.login(config.SMTP_USER, config.SMTP_PASS)
        smtp.send_message(msg)
