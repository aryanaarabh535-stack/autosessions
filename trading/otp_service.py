import asyncio
import re
import time
import logging
from typing import Tuple, Optional
from telethon import TelegramClient
from trading.models import safe_telethon_connect, safe_telethon_disconnect, build_desktop_telethon_client
from encryption import decrypt_value

logger = logging.getLogger("tgbitz.autosell.otp")


class OtpService:
    def __init__(self, api_id: int, api_hash: str):
        self.api_id = api_id
        self.api_hash = api_hash

    async def fetch_fresh_otp(
        self,
        session_encrypted: str,
        phone_masked: str = "Unknown",
        requested_after_timestamp: float = 0.0,
        timeout_seconds: int = 75
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Polls official Telegram service notifications (777000) for a fresh login code.
        """
        raw_session = decrypt_value(session_encrypted)
        if not raw_session:
            return False, None, "Session decryption failed"

        client = build_desktop_telethon_client(raw_session, self.api_id, self.api_hash)
        code_pattern = re.compile(r"(?:login\s*code|code|telegram\s*code|c[oó]digo|passcode|de\s*connexion)[:\s]*([0-9]{5,6})", re.IGNORECASE)
        fallback_pattern = re.compile(r"\b([0-9]{5,6})\b")

        start_time = time.time()
        try:
            await safe_telethon_connect(client, timeout=15.0, settle_delay=0.3)
            if not await client.is_user_authorized():
                return False, None, "Account session revoked / unauthorized"

            logger.info(f"⏳ [{phone_masked}] Polling 777000 for fresh OTP (timeout={timeout_seconds}s)...")
            while (time.time() - start_time) < timeout_seconds:
                try:
                    msgs = await client.get_messages(777000, limit=3)
                    for m in msgs:
                        m_ts = m.date.timestamp() if m.date else 0
                        # Must be newer than request timestamp - 10s grace
                        if m_ts >= (requested_after_timestamp - 10):
                            txt = m.text or ""
                            # Primary pattern
                            match = code_pattern.search(txt)
                            if match:
                                code = match.group(1)
                                logger.info(f"🔑 [{phone_masked}] OTP code fetched successfully: {code}")
                                return True, code, txt
                            # Fallback pattern
                            f_match = fallback_pattern.search(txt)
                            if f_match:
                                code = f_match.group(1)
                                logger.info(f"🔑 [{phone_masked}] OTP fallback code fetched: {code}")
                                return True, code, txt
                except Exception as e:
                    logger.debug(f"[{phone_masked}] OTP poll notice: {e}")

                await asyncio.sleep(2.5)

            return False, None, "Timeout waiting for OTP from Telegram (777000)"

        except Exception as e:
            logger.error(f"[{phone_masked}] OTP fetch exception: {e}")
            return False, None, str(e)
        finally:
            await safe_telethon_disconnect(client)
