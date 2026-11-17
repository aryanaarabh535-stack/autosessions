import asyncio
import contextlib
import time
import os
from typing import Optional, Dict, Any, List
from enum import Enum
from pydantic import BaseModel, Field
import config


def is_internal_cancellation() -> bool:
    return False


async def safe_telethon_connect(client, timeout: float = 15.0, settle_delay: float = 0.5) -> None:
    """Connects a Telethon client cleanly with timeout and settle delay."""
    await asyncio.wait_for(client.connect(), timeout=timeout)
    if getattr(client, "_no_updates", False):
        upd_handle = getattr(client, "_updates_handle", None)
        if upd_handle and hasattr(upd_handle, "done") and not upd_handle.done():
            upd_handle.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(upd_handle, timeout=1.0)
            try:
                client._updates_handle = None
            except Exception:
                pass
    if settle_delay and settle_delay > 0:
        await asyncio.sleep(settle_delay)


async def safe_telethon_disconnect(client) -> None:
    """Cleanly disconnects Telethon client without task leaks."""
    if not client:
        return
    try:
        sender = getattr(client, "_sender", None)
        if sender:
            for state in list(getattr(sender, "_pending_state", {}).values()):
                fut = getattr(state, "future", None)
                if fut and not fut.done():
                    fut.cancel()
    except Exception:
        pass

    try:
        await asyncio.wait_for(client.disconnect(), timeout=5.0)
    except Exception:
        pass

    for attr in ("_updates_handle", "_keepalive_handle"):
        t = getattr(client, attr, None)
        if t and hasattr(t, "done") and not t.done():
            t.cancel()


def build_desktop_telethon_client(
    session,
    api_id: int = config.API_ID,
    api_hash: str = config.API_HASH,
    client_cls=None,
    session_cls=None
):
    """Builds a Telethon client mimicking official Windows Telegram Desktop."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    c_cls = client_cls or TelegramClient
    s_cls = session_cls or StringSession
    
    sess_obj = s_cls(session) if isinstance(session, str) else session
    client = c_cls(
        sess_obj,
        api_id=api_id,
        api_hash=api_hash,
        device_model="Desktop",
        system_version="Windows 10",
        app_version="4.16.6",
        lang_code="en",
        system_lang_code="en",
        auto_reconnect=False,
        connection_retries=2,
        retry_delay=1.0,
        timeout=15.0,
        receive_updates=False
    )
    client._no_updates = True
    return client


class JobState(str, Enum):
    QUEUED = "QUEUED"
    CLEANING = "CLEANING"
    CLEANED = "CLEANED"
    SELLING = "SELLING"
    SUBMITTED = "SUBMITTED"
    OTP_SENT = "OTP_SENT"
    RECEIVER_LOGGED_IN = "RECEIVER_LOGGED_IN"
    VERIFIED_BY_ME = "VERIFIED_BY_ME"
    SESSION_TERMINATED = "SESSION_TERMINATED"
    AWAITING_PAYOUT = "AWAITING_PAYOUT"
    PAID = "PAID"
    FAILED = "FAILED"
    ASK_OWNER = "ASK_OWNER"


class ReceiverProfile(BaseModel):
    id: str
    name: str
    bot_username: str
    channel_username: Optional[str] = None
    enabled: bool = True
    sell_enabled: bool = True
    priority: int = 1
    start_cmd: str = "/start"
    cancel_cmd: str = "/cancel"
    rate_cmd: str = "/cap"
    balance_cmd: str = "/account"
    withdraw_cmd: str = "/withdraw"
    withdrawal_type: str = "usdt_bep20"
    number_format: str = "plain_with_plus"
    balance_regex: Optional[str] = None
    quote_regex: Optional[str] = None
    ask_otp_regex: Optional[str] = None
    logout_wait_regex: Optional[str] = None
    rejected_regex: Optional[str] = None
    default_wait_seconds: int = 600
    require_two_devices_before_logout: bool = True
    require_twofa_before_logout: bool = False
    rates: Dict[str, Any] = Field(default_factory=dict)


class AutoSellJob(BaseModel):
    job_id: str
    session_id: str
    phone: Optional[str] = None
    phone_masked: str = "Hidden"
    country_iso: str = "XX"
    session_encrypted: Optional[str] = None
    assigned_operator_id: Optional[str] = None
    target_receiver_id: Optional[str] = None
    target_receiver_username: Optional[str] = None
    state: JobState = JobState.QUEUED
    payout_usd: float = 0.0
    confirmed_payout_usd: float = 0.0
    payout_delay_seconds: int = 600
    twofa_verified: bool = False
    last_raw_message: Optional[str] = None
    error_msg: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
