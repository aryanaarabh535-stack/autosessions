import asyncio
import logging
from typing import Tuple, Optional
from telethon import TelegramClient
from telethon.tl.functions.auth import ResetAuthorizationsRequest
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest, GetPasswordRequest
from telethon.tl.functions.contacts import DeleteContactsRequest, GetContactsRequest
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import DeleteChatUserRequest
from telethon.tl.types import Channel, Chat
from trading.models import safe_telethon_connect, safe_telethon_disconnect, build_desktop_telethon_client
from trading.temp_mail import bind_free_temp_email
from encryption import decrypt_value

logger = logging.getLogger("tgbitz.autosell.cleaner")


class AccountCleaner:
    def __init__(self, api_id: int, api_hash: str):
        self.api_id = api_id
        self.api_hash = api_hash

    async def isolate_and_clean_session(
        self,
        session_encrypted: str,
        phone_masked: str = "Unknown",
        fast_mode: bool = True
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Connects cleanly to the account session, kicks other sessions,
        cleans dialogs/contacts, verifies @spambot, and binds temp email.
        """
        raw_session = decrypt_value(session_encrypted)
        if not raw_session:
            return False, "Failed to decrypt session", None

        # Build Telethon client
        client = build_desktop_telethon_client(raw_session, self.api_id, self.api_hash)
        bound_email = None

        try:
            await safe_telethon_connect(client, timeout=15.0, settle_delay=0.4)
            if not await client.is_user_authorized():
                return False, "Session unauthorized / revoked", None

            # 1. Isolate session: kick other devices
            auths = await client(GetAuthorizationsRequest())
            other_auths = [a for a in getattr(auths, "authorizations", []) if not getattr(a, "current", False)]
            if other_auths:
                try:
                    await client(ResetAuthorizationsRequest())
                except Exception:
                    for a in other_auths:
                        try:
                            await client(ResetAuthorizationRequest(hash=a.hash))
                        except Exception:
                            pass
                await asyncio.sleep(0.5)

            # 2. Check 2FA & Disable if active before sale
            pwd_info = await client(GetPasswordRequest())
            if getattr(pwd_info, "has_password", False):
                disabled = False
                for cand in ["Tgbitz", "123456", "admin123", "Bitz@1234"]:
                    try:
                        await client.edit_2fa(current_password=cand, new_password=None)
                        disabled = True
                        break
                    except Exception:
                        continue
                if not disabled:
                    logger.warning(f"[{phone_masked}] 2FA password could not be removed with standard passwords.")

            # 3. Clean Dialogs & Leave Channels
            async for d in client.iter_dialogs(limit=25):
                ent = d.entity
                ent_id = getattr(ent, "id", None)
                if ent_id == 777000:
                    continue
                try:
                    if d.is_channel or d.is_group:
                        if isinstance(ent, Channel):
                            await client(LeaveChannelRequest(d.input_entity))
                        elif isinstance(ent, Chat):
                            await client(DeleteChatUserRequest(chat_id=ent.id, user_id="me"))
                    await client.delete_dialog(d.input_entity)
                except Exception:
                    pass

            # 4. Delete Contacts
            try:
                c_resp = await client(GetContactsRequest(hash=0))
                contact_ids = [u.id for u in getattr(c_resp, "users", [])]
                if contact_ids:
                    await client(DeleteContactsRequest(id=contact_ids[:100]))
            except Exception:
                pass

            # 5. Bind Free Temp Email
            try:
                mail_ok, mail_val, _ = await bind_free_temp_email(
                    client,
                    max_attempts=3,
                    otp_timeout=35,
                    wait_on_rate_limit=False
                )
                if mail_ok and mail_val:
                    bound_email = mail_val
                    logger.info(f"[{phone_masked}] Temp email bound: {bound_email}")
            except Exception as e:
                logger.debug(f"[{phone_masked}] Temp email notice: {e}")

            # 6. Check @spambot
            spam_status = "Clean"
            try:
                sent = await client.send_message("spambot", "/start")
                await asyncio.sleep(1.5)
                msgs = await client.get_messages("spambot", limit=2)
                for m in msgs:
                    if m.id > sent.id and m.text:
                        if any(b in m.text.lower() for b in ("limited", "unfortunately", "blocked")):
                            spam_status = "Spam / Restricted"
                        break
            except Exception:
                pass

            # Final verify remaining devices
            auths_final = await client(GetAuthorizationsRequest())
            rem_count = len(getattr(auths_final, "authorizations", []))
            
            logger.info(f"✓ [{phone_masked}] Account cleaned: Devices={rem_count}, Spam={spam_status}")
            return True, f"Cleaned (Devices={rem_count}, Spam={spam_status})", bound_email

        except Exception as e:
            logger.error(f"Cleaning error for {phone_masked}: {e}")
            return False, f"Cleaning failed: {e}", None
        finally:
            await safe_telethon_disconnect(client)
