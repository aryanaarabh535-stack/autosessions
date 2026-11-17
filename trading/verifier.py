import asyncio
import time
import logging
from typing import Tuple, Dict, Any, Optional
from telethon import TelegramClient
from telethon.tl.functions.account import GetPasswordRequest, GetAuthorizationsRequest, ResetAuthorizationRequest
from trading.models import safe_telethon_connect, safe_telethon_disconnect, build_desktop_telethon_client
from encryption import decrypt_value

logger = logging.getLogger("tgbitz.autosell.verifier")


class AccountVerifier:
    def __init__(self, api_id: int, api_hash: str):
        self.api_id = api_id
        self.api_hash = api_hash

    async def verify_post_login_state(
        self,
        session_encrypted: str,
        phone_masked: str = "Unknown",
        otp_sent_at: float = 0.0,
        terminate_on_success: bool = True,
        require_two_devices: bool = True,
        require_twofa: bool = False
    ) -> Tuple[bool, Dict[str, Any], str]:
        """
        Verifies receiver logged in (2-device check: ours + receiver),
        revokes any unexpected 3rd device, and terminates OUR local session via log_out().
        NEVER revokes the receiver's session!
        """
        raw_session = decrypt_value(session_encrypted)
        if not raw_session:
            return False, {}, "Missing decrypted session string"

        client = build_desktop_telethon_client(raw_session, self.api_id, self.api_hash)
        result = {
            "session_count": 0,
            "twofa_enabled": False,
            "session_terminated": False
        }

        try:
            await safe_telethon_connect(client, timeout=15.0, settle_delay=0.4)
            if not await client.is_user_authorized():
                return False, result, "Session unauthorized / already logged out"

            # 1. 2FA Check
            try:
                pwd_info = await client(GetPasswordRequest())
                result["twofa_enabled"] = bool(pwd_info.has_password)
            except Exception:
                pass

            # 2. Authorizations Check
            auths = await client(GetAuthorizationsRequest())
            # Poll up to 6s for receiver's session to appear
            for _ in range(3):
                if len(auths.authorizations) >= 2:
                    break
                await asyncio.sleep(2.0)
                auths = await client(GetAuthorizationsRequest())

            non_current = [a for a in auths.authorizations if not getattr(a, "current", False)]
            
            # If 3+ sessions exist, keep the one closest to otp_sent_at and revoke extra rogue devices
            if len(non_current) >= 2:
                ref_ts = otp_sent_at if otp_sent_at else time.time()
                def _diff(a):
                    dc = getattr(a, "date_created", None) or getattr(a, "date_active", None)
                    return abs(dc.timestamp() - ref_ts) if dc else 999999.0
                
                sorted_auths = sorted(non_current, key=_diff)
                safe_rec = sorted_auths[0]
                logger.info(f"🛡️ [{phone_masked}] Multiple devices found! Keeping receiver ({getattr(safe_rec, 'device_model', 'Bot')}), revoking {len(sorted_auths)-1} rogue session(s)...")
                for rogue in sorted_auths[1:]:
                    try:
                        await client(ResetAuthorizationRequest(hash=rogue.hash))
                    except Exception:
                        pass
                await asyncio.sleep(1.0)
                auths = await client(GetAuthorizationsRequest())

            result["session_count"] = len(auths.authorizations)

            # 3. Check requirements
            sessions_ok = (result["session_count"] == 2) if require_two_devices else (result["session_count"] >= 1)
            twofa_ok = result["twofa_enabled"] if require_twofa else True

            is_valid = sessions_ok and twofa_ok

            # 4. Terminate OUR session cleanly via log_out()
            if is_valid and terminate_on_success:
                try:
                    await client.log_out()
                    result["session_terminated"] = True
                    logger.info(f"🚪 [{phone_masked}] Local session logged out cleanly (Receiver retained access).")
                except Exception as lo_err:
                    err_s = str(lo_err).lower()
                    if "unregistered" in err_s or "unauthorized" in err_s:
                        result["session_terminated"] = True
                    else:
                        logger.warning(f"[{phone_masked}] log_out notice: {lo_err}")

            status_desc = f"Sessions={result['session_count']}, 2FA={'YES' if result['twofa_enabled'] else 'NO'}, Terminated={'YES' if result['session_terminated'] else 'NO'}"
            return is_valid, result, status_desc

        except Exception as e:
            logger.error(f"[{phone_masked}] Verification exception: {e}")
            return False, result, str(e)
        finally:
            await safe_telethon_disconnect(client)

    async def terminate_our_session(self, session_encrypted: str, phone_masked: str = "Unknown") -> Tuple[bool, str]:
        raw_session = decrypt_value(session_encrypted)
        if not raw_session:
            return False, "Decryption failed"
        client = build_desktop_telethon_client(raw_session, self.api_id, self.api_hash)
        try:
            await safe_telethon_connect(client, timeout=12.0, settle_delay=0.3)
            if await client.is_user_authorized():
                await client.log_out()
                logger.info(f"🚪 [{phone_masked}] Local session terminated cleanly.")
                return True, "Session logged out."
            return True, "Session was already logged out."
        except Exception as e:
            return False, f"Termination error: {e}"
        finally:
            await safe_telethon_disconnect(client)
