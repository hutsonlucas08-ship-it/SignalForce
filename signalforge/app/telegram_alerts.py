"""Private Telegram notifications. Bot token and chat ID stay server-side."""
import os
import httpx


def status():
    configured = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    return {"configured": configured, "destination": "private chat" if configured else None,
            "note": "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as server-side environment secrets. Never expose either value in the browser."}


async def send_test():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise ValueError("Telegram is not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in server-side secrets.")
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.post(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            json={"chat_id": chat_id,
                  "text": "SIGNALFORGE CONNECTED\\nPrivate paper-trading and wallet research alerts are enabled. No live trades are executed.",
                  "disable_web_page_preview": True},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise ValueError("Telegram did not accept the test message.")
    return {"sent": True, "message": "Test alert sent to the configured private chat."}
