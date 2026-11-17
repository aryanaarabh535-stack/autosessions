import asyncio
import logging
from typing import Dict, Optional, Tuple, List
from telethon import TelegramClient
from telethon.sessions import StringSession
from database.connection import get_sync_database
from database.models import OperatorRecord
from encryption import decrypt_value
import config

logger = logging.getLogger("tgbitz.autosell.operators")


class OperatorPool:
    def __init__(self, api_id: int = config.API_ID, api_hash: str = config.API_HASH):
        self.api_id = api_id
        self.api_hash = api_hash
        self._clients: Dict[str, TelegramClient] = {}
        self._lock = asyncio.Lock()

    def list_registered_operators(self) -> List[Dict[str, Any]]:
        db = get_sync_database()
        ops = []
        # Check 'operators'
        for doc in db["operators"].find({"is_active": True}):
            ops.append({
                "operator_id": doc.get("operator_id"),
                "phone": doc.get("phone", "Unknown"),
                "daily_sales": doc.get("daily_sales", 0),
                "source": "operators"
            })
        # Also check 'trading_operator_sessions' (shared pool)
        for doc in db["trading_operator_sessions"].find({}):
            op_id = doc.get("operator_id")
            if not any(o["operator_id"] == op_id for o in ops):
                ops.append({
                    "operator_id": op_id,
                    "phone": doc.get("phone", "Unknown"),
                    "daily_sales": 0,
                    "source": "trading_operator_sessions"
                })
        return ops

    async def get_client_for_operator(self, operator_id: str) -> Tuple[Optional[TelegramClient], Optional[str]]:
        async with self._lock:
            if operator_id in self._clients:
                cl = self._clients[operator_id]
                if cl.is_connected() and await cl.is_user_authorized():
                    return cl, None
                else:
                    try:
                        await cl.connect()
                        if await cl.is_user_authorized():
                            return cl, None
                    except Exception:
                        pass

            # Load session string from DB
            db = get_sync_database()
            doc = db["operators"].find_one({"operator_id": operator_id})
            sess_enc = None
            if doc:
                sess_enc = doc.get("session_encrypted") or doc.get("session_string_encrypted")
            if not sess_enc:
                doc = db["trading_operator_sessions"].find_one({"operator_id": operator_id})
                if doc:
                    sess_enc = doc.get("session_string_encrypted") or doc.get("session_encrypted")

            if not sess_enc:
                return None, f"No encrypted session found for operator {operator_id}"

            raw_str = decrypt_value(sess_enc)
            if not raw_str:
                return None, f"Failed to decrypt session for operator {operator_id}"

            try:
                client = TelegramClient(
                    StringSession(raw_str),
                    self.api_id,
                    self.api_hash,
                    device_model="OperatorPC",
                    system_version="Windows 10",
                    app_version="4.16.6",
                    auto_reconnect=True,
                    connection_retries=3
                )
                await client.connect()
                if not await client.is_user_authorized():
                    return None, f"Operator {operator_id} session unauthorized"

                self._clients[operator_id] = client
                logger.info(f"✓ Connected operator client: {operator_id}")
                return client, None
            except Exception as e:
                logger.error(f"Error connecting operator {operator_id}: {e}")
                return None, str(e)

    async def disconnect_all(self):
        async with self._lock:
            for op_id, cl in self._clients.items():
                try:
                    await cl.disconnect()
                except Exception:
                    pass
            self._clients.clear()
