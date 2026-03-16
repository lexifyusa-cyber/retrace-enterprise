"""
SMTP mailer for invite and temp-password notifications.
"""

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import smtplib
import ssl
from typing import Optional

import structlog

logger = structlog.get_logger()


def send_smtp_email(
    smtp_config: dict,
    *,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: Optional[str] = None,
) -> None:
    if not smtp_config or not smtp_config.get("enabled", True):
        raise ValueError("SMTP is not enabled")

    host = (smtp_config.get("host") or "").strip()
    from_email = (smtp_config.get("from_email") or "").strip()
    if not host or not from_email:
        raise ValueError("SMTP host and from_email are required")

    port = int(smtp_config.get("port") or 587)
    use_ssl = bool(smtp_config.get("use_ssl", False))
    use_starttls = bool(smtp_config.get("use_starttls", not use_ssl))
    username = (smtp_config.get("username") or "").strip()
    password = smtp_config.get("password") or ""
    from_name = (smtp_config.get("from_name") or "").strip()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    msg["To"] = to_email
    msg.attach(MIMEText(text_body or "Please use an HTML-compatible mail client.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as server:
                if username:
                    server.login(username, password)
                server.sendmail(from_email, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.ehlo()
                if use_starttls:
                    server.starttls(context=context)
                    server.ehlo()
                if username:
                    server.login(username, password)
                server.sendmail(from_email, [to_email], msg.as_string())
    except Exception as exc:
        logger.error("smtp_send_failed", host=host, port=port, to_email=to_email, error=str(exc))
        raise

