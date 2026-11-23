"""Transactional email delivery for customer account verification.

Uses Resend over HTTPS. No email credentials are stored in the repository.
"""
import os
import httpx


class EmailDeliveryError(RuntimeError):
    pass


async def send_verification_email(email: str, token: str) -> None:
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    sender = os.getenv("SIGNALFORCE_EMAIL_FROM", "").strip()
    base_url = os.getenv("SIGNALFORCE_PUBLIC_URL", "").strip().rstrip("/")
    if not api_key or not sender or not base_url:
        raise EmailDeliveryError("Email verification delivery is not configured.")
    verify_url = base_url + "/account?verify=" + token
    payload = {
        "from": sender,
        "to": [email],
        "subject": "Verify your SignalForce email",
        "html": (
            "<div style='font-family:Arial,sans-serif;max-width:560px;margin:auto;color:#17231a'>"
            "<h1>Verify your SignalForce email</h1>"
            "<p>Confirm this email address to activate your SignalForce account.</p>"
            "<p><a href='" + verify_url + "' style='display:inline-block;padding:12px 18px;"
            "background:#21c55d;color:#07110a;text-decoration:none;border-radius:6px'>Verify email</a></p>"
            "<p>This one-time link expires in one hour. If you did not create this account, ignore this message.</p>"
            "</div>"
        ),
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                json=payload,
            )
        if response.status_code >= 300:
            raise EmailDeliveryError("Email provider rejected the verification message.")
    except httpx.HTTPError as exc:
        raise EmailDeliveryError("Email provider is temporarily unavailable.") from exc
