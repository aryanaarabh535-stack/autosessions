import os
import re
import time
import json
import asyncio
import logging
import unicodedata
from typing import Dict, Optional, Tuple, List, Any
from telethon import TelegramClient
from trading.models import ReceiverProfile
from trading.otp_service import OtpService
from trading.verifier import AccountVerifier

logger = logging.getLogger("tgbitz.autosell.receiver_adapter")

DEFAULT_ASK_OTP_REGEX = (
    r"(?:(?:enter|send|input|type|provide)\s+(?:the\s+)?(?:login\s+|verification\s+|confirmation\s+)?(?:otp|code)|"
    r"(?:the\s+)?code\s+(?:was\s+)?sent(?:\s+to)?|telegram\s+code|777000|verification\s+code|"
    r"please\s+enter\s+the\s+code|كود|رمز)"
)

DEFAULT_LOGOUT_WAIT_REGEX = (
    r"(?:account\s+(?:\+?\d+\s+)?received(?:\s+completed)?\b|login\s+received|received[,\s]+wait\s+\d+|"
    r"(?:was|is)\s+successfull?y?(?:\s+received)?\)?|received\s+the\s+account|"
    r"will\s+be\s+(?:automatically\s+)?(?:verified|confirmed)\s+(?:automatically\s+)?(?:after|in)|"
    r"please\s+log\s*out\s+of\s+your\s+account\s+on\s+all\s+other\s+devices|"
    r"account\s+accepted|account\s+confirmed|congratulations[!,\s]+account)"
)

INLINE_QUOTE_RE = re.compile(
    r"(?:price\s*\(\s*\$?\s*([0-9]+(?:\.[0-9]+)?)\s*\)"
    r"|you\s+will\s+get\s*\$?\s*([0-9]+(?:\.[0-9]+)?)\$?"
    r"|sell\s+price\s*:\s*\$?\s*([0-9]+(?:\.[0-9]+)?)"
    r"|price\s*:\s*\$?\s*([0-9]+(?:\.[0-9]+)?))",
    re.IGNORECASE
)


def _norm_text(txt: Optional[str]) -> str:
    return unicodedata.normalize("NFKC", txt or "")


class ReceiverAdapter:
    def __init__(self, otp_service: OtpService, verifier: AccountVerifier, receivers_dir: Optional[str] = None):
        self.otp_service = otp_service
        self.verifier = verifier
        self.receivers_dir = receivers_dir or os.path.join(os.path.dirname(__file__), "receivers")
        self.receivers: Dict[str, ReceiverProfile] = {}
        self.load_receivers()

    def load_receivers(self):
        if not os.path.exists(self.receivers_dir):
            return
        for file in os.listdir(self.receivers_dir):
            if file.endswith(".json"):
                full_p = os.path.join(self.receivers_dir, file)
                try:
                    with open(full_p, "r", encoding="utf-8") as jf:
                        data = json.load(jf)
                        prof = ReceiverProfile.model_validate(data)
                        self.receivers[prof.id] = prof
                except Exception as e:
                    logger.debug(f"Error loading receiver {file}: {e}")
        logger.info(f"✓ Loaded {len(self.receivers)} receiver profile(s) from {self.receivers_dir}")

    def get_best_receiver_for_country(self, country_iso: str) -> Optional[Tuple[ReceiverProfile, float]]:
        """Finds active receiver offering the highest price for country_iso."""
        candidates = []
        for r in self.receivers.values():
            if not r.enabled or not r.sell_enabled:
                continue
            rt = r.rates.get(country_iso.upper())
            if rt:
                p = float(rt.get("price_usd", 0.0) or 0.0)
                if p > 0:
                    candidates.append((r, p))
        if not candidates:
            return None
        # Sort highest price, tie breaker by priority
        candidates.sort(key=lambda c: (-c[1], c[0].priority))
        return candidates[0]

    async def execute_sell_flow(
        self,
        operator_client: TelegramClient,
        receiver: ReceiverProfile,
        session_encrypted: str,
        phone: str,
        phone_masked: str
    ) -> Tuple[bool, float, str]:
        """
        Executes full live sale to receiver bot using operator client:
        - Submits phone number
        - Waits for OTP prompt & submits fresh OTP code
        - Waits for receiver acknowledgment
        - Strictly verifies 2 active sessions (ours + receiver)
        - Logs out our session only
        - Clicks inline confirmation button if needed (e.g. Star Bot)
        """
        bot_uname = receiver.bot_username
        start_cmd = receiver.start_cmd or "/start"
        clean_digits = re.sub(r"[^\d]", "", phone)
        phone_to_send = f"+{clean_digits}" if receiver.number_format == "plain_with_plus" else clean_digits

        otp_ask_re = re.compile(receiver.ask_otp_regex or DEFAULT_ASK_OTP_REGEX, re.IGNORECASE)
        logout_re = re.compile(receiver.logout_wait_regex or DEFAULT_LOGOUT_WAIT_REGEX, re.IGNORECASE)

        logger.info(f"🛒 [{phone_masked}] Starting sale to @{bot_uname} ({receiver.name})...")

        try:
            # 1. Send /start
            start_msg = await operator_client.send_message(bot_uname, start_cmd)
            await asyncio.sleep(2.0)

            # 2. Submit phone number
            sub_time = time.time()
            phone_msg = await operator_client.send_message(bot_uname, phone_to_send)

            quoted_price = 0.0
            otp_prompt_received = False

            # 3. Poll for OTP request (up to 35 seconds)
            for _ in range(14):
                await asyncio.sleep(2.5)
                recent = await operator_client.get_messages(bot_uname, limit=5)
                for m in reversed(recent):
                    if m.id > phone_msg.id and m.text:
                        txt = _norm_text(m.text)

                        # Extract price quote if present
                        q_match = INLINE_QUOTE_RE.search(txt)
                        if q_match:
                            val = next((g for g in q_match.groups() if g), None)
                            if val:
                                try:
                                    quoted_price = float(val)
                                except ValueError:
                                    pass

                        # Check for rejections
                        if receiver.rejected_regex and re.search(receiver.rejected_regex, txt, re.IGNORECASE):
                            return False, 0.0, f"Rejected by @{bot_uname}: {txt[:80]}"

                        # Check for OTP prompt
                        if otp_ask_re.search(txt):
                            otp_prompt_received = True
                            break

                if otp_prompt_received:
                    break

            if not otp_prompt_received:
                return False, 0.0, f"No OTP prompt received from @{bot_uname}"

            # 4. Fetch fresh OTP from account
            otp_ok, otp_code, _ = await self.otp_service.fetch_fresh_otp(
                session_encrypted, phone_masked=phone_masked, requested_after_timestamp=sub_time, timeout_seconds=70
            )
            if not otp_ok or not otp_code:
                try:
                    await operator_client.send_message(bot_uname, receiver.cancel_cmd or "/cancel")
                except Exception:
                    pass
                return False, 0.0, f"Failed to retrieve OTP code from Telegram (777000)"

            # 5. Send OTP to receiver bot
            otp_sent_ts = time.time()
            otp_sent_msg = await operator_client.send_message(bot_uname, otp_code)
            logger.info(f"🚀 [{phone_masked}] Dispatched OTP {otp_code} to @{bot_uname}")

            # 6. Wait for receiver acknowledgment
            login_ack = False
            login_ack_msg = None
            wait_seconds = receiver.default_wait_seconds or 600

            for _ in range(16):
                await asyncio.sleep(2.5)
                recent = await operator_client.get_messages(bot_uname, limit=5)
                for m in reversed(recent):
                    if m.id > otp_sent_msg.id and m.text:
                        txt = _norm_text(m.text)

                        # Extract price if quoted in acknowledgment
                        q_match = INLINE_QUOTE_RE.search(txt)
                        if q_match:
                            val = next((g for g in q_match.groups() if g), None)
                            if val:
                                try:
                                    quoted_price = float(val)
                                except ValueError:
                                    pass

                        # Strict rejection check
                        if receiver.rejected_regex and re.search(receiver.rejected_regex, txt, re.IGNORECASE):
                            return False, 0.0, f"Rejected post-OTP by @{bot_uname}: {txt[:80]}"

                        # Acceptance check
                        if logout_re.search(txt) or "account received" in txt.lower() or "successfull" in txt.lower():
                            login_ack = True
                            login_ack_msg = m
                            break

                if login_ack:
                    break

            if not login_ack:
                return False, 0.0, f"Login acknowledgment missing from @{bot_uname}"

            logger.info(f"✓ [{phone_masked}] Login acknowledged by @{bot_uname}. Price: ${quoted_price:.2f}")

            # 7. Post-Login 2-Device Verification & Logout OUR session
            req_two = bool(getattr(receiver, "require_two_devices_before_logout", True))
            req_twofa = bool(getattr(receiver, "require_twofa_before_logout", False))

            v_ok, v_meta, v_desc = await self.verifier.verify_post_login_state(
                session_encrypted,
                phone_masked=phone_masked,
                otp_sent_at=otp_sent_ts,
                terminate_on_success=True,
                require_two_devices=req_two,
                require_twofa=req_twofa
            )
            if not v_ok:
                return False, 0.0, f"Post-login verification failed: {v_desc}"

            # 8. Handle Star Bot / inline confirmation buttons after local logout
            try:
                candidate_msgs = [login_ack_msg] if login_ack_msg else []
                recent_msgs = await operator_client.get_messages(bot_uname, limit=4)
                for rm in recent_msgs:
                    if rm and not getattr(rm, "out", False) and getattr(rm, "buttons", None):
                        candidate_msgs.append(rm)

                clicked_btn = False
                for c_msg in candidate_msgs:
                    if clicked_btn or not getattr(c_msg, "buttons", None):
                        continue
                    for r_idx, row in enumerate(c_msg.buttons):
                        for c_idx, btn in enumerate(row):
                            b_txt = (getattr(btn, "text", "") or "").lower()
                            if re.search(r"confirm|check|done|verified|logged\s*out|you\s+will\s+(?:get|receive)|\$\s*\d+|\d+\s*\$", b_txt):
                                if hasattr(c_msg, "click"):
                                    await c_msg.click(r_idx, c_idx)
                                elif hasattr(btn, "click"):
                                    await btn.click()
                                logger.info(f"🔘 Clicked confirmation button '{btn.text}' on @{bot_uname}")
                                clicked_btn = True
                                await asyncio.sleep(1.0)
                                break
                        if clicked_btn:
                            break
            except Exception as b_err:
                logger.debug(f"Button click note: {b_err}")

            logger.info(f"🎉 [{phone_masked}] Sold successfully to @{bot_uname}! Payout: ${quoted_price:.2f}")
            return True, quoted_price, f"Sold to @{bot_uname} (${quoted_price:.2f})"

        except Exception as e:
            logger.error(f"Sale error for {phone_masked} on @{bot_uname}: {e}")
            return False, 0.0, str(e)
