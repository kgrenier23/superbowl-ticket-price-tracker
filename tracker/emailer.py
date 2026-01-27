"""Email sending with SMTP and attachments."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

from tracker.config import settings

logger = logging.getLogger(__name__)


def send_email(
    subject: str,
    body_text: str,
    attachments: list[Path] | None = None,
    html_body: str | None = None,
    inline_images: dict[str, Path] | None = None,
) -> None:
    """Send an email with optional file attachments.

    If DRY_RUN is set, prints to console instead.
    """
    if settings.dry_run:
        logger.info("DRY RUN — would send email:")
        logger.info("  Subject: %s", subject)
        logger.info("  To: %s", settings.email_to)
        logger.info("  Body:\n%s", body_text)
        if attachments:
            for a in attachments:
                logger.info("  Attachment: %s (%d bytes)", a.name, a.stat().st_size if a.exists() else 0)
        print(f"\n{'='*60}")
        print(f"EMAIL (dry run)")
        print(f"Subject: {subject}")
        print(f"To: {settings.email_to}")
        print(f"{'='*60}")
        print(body_text)
        if attachments:
            for a in attachments:
                print(f"  [Attached: {a.name}]")
        print(f"{'='*60}\n")
        return

    if not settings.smtp_host or not settings.email_from or not settings.email_recipients:
        logger.warning("SMTP not configured; skipping email send.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.email_from
    msg["To"] = ", ".join(settings.email_recipients)
    msg.set_content(body_text)

    if html_body:
        msg.add_alternative(html_body, subtype="html")
        # Embed inline images into the HTML part
        if inline_images:
            html_part = msg.get_payload()[-1]  # the HTML alternative
            for cid, img_path in inline_images.items():
                if not img_path.exists():
                    continue
                html_part.add_related(
                    img_path.read_bytes(),
                    maintype="image",
                    subtype=img_path.suffix.lstrip("."),
                    cid=cid,
                )

    if attachments:
        for fpath in attachments:
            if not fpath.exists():
                continue
            mime = "application/octet-stream"
            if fpath.suffix == ".csv":
                mime = "text/csv"
            elif fpath.suffix == ".png":
                mime = "image/png"
            maintype, subtype = mime.split("/", 1)
            msg.add_attachment(
                fpath.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=fpath.name,
            )

    try:
        if settings.smtp_tls:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(msg)
        logger.info("Email sent: %s", subject)
    except Exception:
        logger.exception("Failed to send email")
