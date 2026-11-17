"""
TG BITZ — Free Automated Temp Mail & Telegram Email Binder
Ported and hardened from Session-Bot (mail_engine.py + operations.py) for standalone use in Dollerminnerbot.
Supports Mail.tm (primary, rate-limit aware), Tempmail.plus (active domains only), and GuerrillaMail (tertiary fallback).
Handles both existing Login Email overwrite (EmailVerifyPurposeLoginChange) and initial 2FA Recovery Email setup
when Telegram returns EMAIL_NOT_SETUP.
"""

import asyncio
import json
import logging
import os
import random
import re
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Tuple

from telethon import TelegramClient, errors as tele_errors, password as tele_password
from telethon.errors import FloodWaitError
from telethon.tl import functions as tele_functions, types as tele_types

logger = logging.getLogger("tgbitz.trading.temp_mail")

_MAIL_HTTP_LIMITER = threading.BoundedSemaphore(6)
_EMAIL_SEND_LOCK: Optional[asyncio.Lock] = None
_LAST_EMAIL_SEND_AT: float = 0.0
_TG_EMAIL_COOLDOWN_UNTIL: float = 0.0


def _get_email_send_lock() -> asyncio.Lock:
    """Lazy event-loop-safe lock helper for serializing Telegram verification email sends."""
    global _EMAIL_SEND_LOCK
    if _EMAIL_SEND_LOCK is None:
        _EMAIL_SEND_LOCK = asyncio.Lock()
    return _EMAIL_SEND_LOCK


def extract_telegram_code(text: str, expected_length: Optional[int] = None) -> Optional[str]:
    """
    Extract Telegram 5-6 digit email verification code from subject/body.
    """
    if not text:
        return None

    cleaned = text.replace("&nbsp;", " ").replace("\u00a0", " ").replace("\u200b", " ")
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)

    context_patterns = [
        r"(?:your\s+(?:telegram\s+)?code|verification\s+code|login\s+code|telegram\s+code|security\s+code|confirm(?:ation)?\s+code|código|код|رمز\w*)(?:\s+(?:is|was|to|هو))?[\s:=-]{1,10}(\d{3})[\s-](\d{3})\b",
        r"(?:your\s+(?:telegram\s+)?code|verification\s+code|login\s+code|telegram\s+code|security\s+code|confirm(?:ation)?\s+code|código|код|رمز\w*)(?:\s+(?:is|was|to|هو))?[\s:=-]{1,10}(\d{5,6})\b",
        r"\b(\d{3})[\s-](\d{3})\b\s+(?:is\s+your\s+telegram|is\s+your\s+verification|is\s+your\s+login|is\s+your\s+code)",
        r"\b(\d{5,6})\b\s+(?:is\s+your\s+telegram|is\s+your\s+verification|is\s+your\s+login|is\s+your\s+code)",
    ]
    for pat in context_patterns:
        m = re.search(pat, cleaned, re.IGNORECASE)
        if m:
            cand = "".join(m.groups())
            if expected_length is None or len(cand) == expected_length:
                return cand

    split_match = re.search(r"\b(\d{3})[\s-](\d{3})\b", cleaned)
    if split_match:
        cand = f"{split_match.group(1)}{split_match.group(2)}"
        if expected_length is None or len(cand) == expected_length:
            return cand

    all_codes = re.findall(r"\b(\d{5,6})\b", cleaned)
    if all_codes:
        if expected_length:
            for c in all_codes:
                if len(c) == expected_length:
                    return c
        return all_codes[0]

    return None


class TempMailClient:
    """
    Free temporary email client (Mail.tm primary, Tempmail.plus secondary, GuerrillaMail tertiary).
    Designed to stay within Mail.tm's 10 req / 60s window and avoid deprecated domains.
    """

    MAILTM_BASE = "https://api.mail.tm"
    TEMPMAIL_PLUS_BASE = "https://tempmail.plus/api"
    GUERRILLAMAIL_BASE = "https://api.guerrillamail.com/ajax.php"
    CODE_REGEX = re.compile(r"\b(\d{5,6})\b")

    # Verified active domains listed on https://tempmail.plus/en/ (removed dead/blocked chitthi.in, merepost.com, anyalias.com, worldmail.fun)
    TEMPMAIL_PLUS_DOMAINS = [
        "fexbox.org",
        "mailto.plus",
        "fexpost.com",
        "fextemp.com",
        "mailbox.in.ua",
        "rover.info",
    ]

    GUERRILLA_DOMAINS = [
        "guerrillamailblock.com",
        "sharklasers.com",
        "grr.la",
        "guerrillamail.com",
        "spam4.me",
    ]

    _mailtm_cooldown_until: float = 0.0
    _tempmail_plus_cooldown_until: float = 0.0
    _guerrilla_cooldown_until: float = 0.0
    _mailtm_domains_cache: list = []
    _mailtm_domains_cached_at: float = 0.0

    def __init__(self):
        self._active_email: Optional[str] = None
        self._active_password: Optional[str] = None
        self._active_token: Optional[str] = None
        self._active_account_id: Optional[str] = None
        self._guerrilla_sid: Optional[str] = None
        self._provider: Optional[str] = None
        self._local_poll_backoff_until: float = 0.0

    def _http_request(self, url: str, data=None, headers=None, method=None):
        hdrs = {"User-Agent": "Mozilla/5.0 (Bitz/1.0)", "Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        if data and isinstance(data, dict):
            data = json.dumps(data).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with _MAIL_HTTP_LIMITER:
                with urllib.request.urlopen(req, timeout=12) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 429:
                try:
                    retry_after = int(e.headers.get("Retry-After", 0) or 0)
                except (TypeError, ValueError):
                    retry_after = 0
                if "api.mail.tm" in url:
                    # Mail.tm ratelimit-policy is 10; w=60 — keep polling backoff short, cap creation cooldown at 60s
                    if "/messages" in url:
                        wait = min(max(retry_after or 6, 4), 15)
                        self._local_poll_backoff_until = time.time() + wait
                    else:
                        wait = min(max(retry_after or 25, 10), 65)
                        TempMailClient._mailtm_cooldown_until = max(TempMailClient._mailtm_cooldown_until, time.time() + wait)
                elif "tempmail.plus" in url:
                    wait = min(max(retry_after or 20, 5), 120)
                    TempMailClient._tempmail_plus_cooldown_until = max(TempMailClient._tempmail_plus_cooldown_until, time.time() + wait)
                elif "guerrillamail.com" in url:
                    wait = min(max(retry_after or 20, 5), 120)
                    TempMailClient._guerrilla_cooldown_until = max(TempMailClient._guerrilla_cooldown_until, time.time() + wait)
            logger.warning(f"[TEMPMAIL] HTTP {e.code} from {url}: {body[:160]}")
            raise

    async def _async_http(self, url: str, data=None, headers=None, method=None):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._http_request, url, data, headers, method)

    async def get_available_domains(self) -> list:
        """Fetch and cache active Mail.tm domains for 10 minutes to conserve rate-limit quota."""
        now = time.time()
        if TempMailClient._mailtm_domains_cache and (now - TempMailClient._mailtm_domains_cached_at) < 600.0:
            return list(TempMailClient._mailtm_domains_cache)
        try:
            result = await self._async_http(f"{self.MAILTM_BASE}/domains")
            members = result.get("hydra:member", [])
            domains = [d["domain"] for d in members if d.get("isActive")]
            if domains:
                TempMailClient._mailtm_domains_cache = domains
                TempMailClient._mailtm_domains_cached_at = now
            return domains
        except Exception as e:
            logger.warning(f"[TEMPMAIL] Failed to fetch Mail.tm domains: {e}")
            return list(TempMailClient._mailtm_domains_cache)

    async def create_temp_email(self, exclude_domains: Optional[list] = None, prefer_provider: Optional[str] = None) -> str:
        exclude = {d.lower().strip() for d in (exclude_domains or []) if d}
        now = time.time()
        self._local_poll_backoff_until = 0.0

        # 1. Primary: Mail.tm (fastest & cleanest HTML/intro previews when quota available)
        if prefer_provider in (None, "mailtm") and now >= TempMailClient._mailtm_cooldown_until:
            domains = await self.get_available_domains()
            valid_mailtm = [d for d in domains if d.lower() not in exclude]
            if valid_mailtm:
                for _mt_try in range(2):
                    domain = random.choice(valid_mailtm)
                    username = "tg" + "".join(random.choices(string.ascii_lowercase + string.digits, k=11))
                    email_addr = f"{username}@{domain}"
                    password = "Bitz_" + "".join(random.choices(string.ascii_letters + string.digits, k=14))
                    try:
                        acc_resp = await self._async_http(
                            f"{self.MAILTM_BASE}/accounts",
                            data={"address": email_addr, "password": password}
                        )
                        await asyncio.sleep(0.35)
                        token_resp = await self._async_http(
                            f"{self.MAILTM_BASE}/token",
                            data={"address": email_addr, "password": password}
                        )
                        self._active_email = email_addr
                        self._active_password = password
                        self._active_token = token_resp.get("token")
                        self._active_account_id = acc_resp.get("id") if isinstance(acc_resp, dict) else None
                        self._guerrilla_sid = None
                        self._provider = "mailtm"
                        logger.info(f"[TEMPMAIL] Created Mail.tm inbox: {email_addr}")
                        return email_addr
                    except Exception as e:
                        if "422" in str(e) and _mt_try == 0:
                            # Regenerate fresh username/domain cleanly if Mail.tm rejected validation on first try
                            valid_mailtm = [d for d in valid_mailtm if d != domain] or valid_mailtm
                            await asyncio.sleep(0.3)
                            continue
                        logger.warning(f"[TEMPMAIL] Mail.tm creation failed, failing over: {e}")
                        break

        # 2. Secondary: Tempmail.plus (active domains only)
        valid_plus = [d for d in self.TEMPMAIL_PLUS_DOMAINS if d.lower() not in exclude]
        if prefer_provider in (None, "tempmail_plus") and valid_plus and now >= TempMailClient._tempmail_plus_cooldown_until:
            domain = random.choice(valid_plus)
            username = "tg_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=11))
            email_addr = f"{username}@{domain}"
            self._active_email = email_addr
            self._active_token = None
            self._active_account_id = None
            self._guerrilla_sid = None
            self._provider = "tempmail_plus"
            logger.info(f"[TEMPMAIL] Using Tempmail.plus inbox: {email_addr}")
            return email_addr

        # 3. Tertiary: GuerrillaMail (fresh external domain pool when Mail.tm & Tempmail.plus blocked)
        if now >= TempMailClient._guerrilla_cooldown_until:
            try:
                gm_res = await self._async_http(f"{self.GUERRILLAMAIL_BASE}?f=get_email_address")
                raw_addr = gm_res.get("email_addr")
                sid = gm_res.get("sid_token")
                if raw_addr and sid:
                    local = raw_addr.split("@")[0]
                    gm_domain = raw_addr.split("@")[-1].lower()
                    if gm_domain in exclude:
                        alt_gm = [d for d in self.GUERRILLA_DOMAINS if d not in exclude]
                        if alt_gm:
                            raw_addr = f"{local}@{random.choice(alt_gm)}"
                    self._active_email = raw_addr
                    self._guerrilla_sid = sid
                    self._active_token = None
                    self._active_account_id = None
                    self._provider = "guerrillamail"
                    logger.info(f"[TEMPMAIL] Using GuerrillaMail inbox: {raw_addr}")
                    return raw_addr
            except Exception as gm_err:
                logger.warning(f"[TEMPMAIL] GuerrillaMail creation failed: {gm_err}")

        # Final fallback to any Tempmail.plus domain
        fallback_plus = valid_plus or list(self.TEMPMAIL_PLUS_DOMAINS)
        domain = random.choice(fallback_plus)
        username = "tg_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=11))
        email_addr = f"{username}@{domain}"
        self._active_email = email_addr
        self._active_token = None
        self._active_account_id = None
        self._guerrilla_sid = None
        self._provider = "tempmail_plus"
        logger.info(f"[TEMPMAIL] Using fallback Tempmail.plus inbox: {email_addr}")
        return email_addr

    async def fetch_verification_code(
        self,
        target_email: Optional[str] = None,
        timeout: int = 40,
        poll_interval: Optional[float] = None,
        expected_length: Optional[int] = None
    ) -> str:
        target = target_email or self._active_email
        if not target:
            raise ValueError("No active email to check")

        interval = poll_interval or (5.5 if self._provider == "mailtm" else 3.0)
        # Allow email provider 2.5s initial delivery time before burning a poll request
        await asyncio.sleep(min(2.5, max(0.5, timeout * 0.1)))

        start = time.time()
        while time.time() - start < timeout:
            now = time.time()
            if now < self._local_poll_backoff_until:
                wait_for = min(timeout - (now - start), self._local_poll_backoff_until - now)
                if wait_for > 0:
                    await asyncio.sleep(wait_for)
                continue

            if self._provider == "tempmail_plus" and now < TempMailClient._tempmail_plus_cooldown_until:
                wait_for = min(timeout - (now - start), TempMailClient._tempmail_plus_cooldown_until - now)
                if wait_for > 0:
                    await asyncio.sleep(wait_for)
                continue

            try:
                code = await self._check_inbox(target, expected_length=expected_length)
                if code:
                    return code
            except Exception as e:
                logger.warning(f"[TEMPMAIL] Inbox check notice ({target}): {e}")
            await asyncio.sleep(interval)

        raise TimeoutError(f"No verification code received within {timeout}s for {target}")

    async def _check_inbox(self, target_email: str, expected_length: Optional[int] = None) -> Optional[str]:
        if self._provider == "guerrillamail" and self._guerrilla_sid:
            return await self._check_guerrillamail_inbox(expected_length=expected_length)
        target_domain = target_email.split("@")[-1].lower() if "@" in target_email else ""
        if any(target_domain == d.lower() for d in self.TEMPMAIL_PLUS_DOMAINS):
            return await self._check_tempmail_plus_inbox(target_email, expected_length=expected_length)
        if self._provider == "mailtm" and self._active_token:
            return await self._check_mailtm_inbox(expected_length=expected_length)
        return await self._check_tempmail_plus_inbox(target_email, expected_length=expected_length)

    async def _check_mailtm_inbox(self, expected_length: Optional[int] = None) -> Optional[str]:
        try:
            messages = await self._async_http(
                f"{self.MAILTM_BASE}/messages",
                headers={"Authorization": f"Bearer {self._active_token}"}
            )
            for msg in messages.get("hydra:member", []):
                # Always inspect subject + intro from list response FIRST so we save /messages/{id} quota!
                preview_text = f"{msg.get('subject', '')} {msg.get('intro', '')}"
                code = extract_telegram_code(preview_text, expected_length=expected_length)
                if code:
                    return code

                msg_id = msg.get("id")
                if not msg_id:
                    continue
                await asyncio.sleep(0.5)
                full_msg = await self._async_http(
                    f"{self.MAILTM_BASE}/messages/{msg_id}",
                    headers={"Authorization": f"Bearer {self._active_token}"}
                )
                combined = f"{full_msg.get('subject', '')} {full_msg.get('text', '') or full_msg.get('html', '')}"
                code = extract_telegram_code(combined, expected_length=expected_length)
                if not code:
                    m = self.CODE_REGEX.search(combined)
                    if m:
                        code = m.group(1)
                if code:
                    return code
        except Exception as e:
            logger.warning(f"[TEMPMAIL] Mail.tm inbox check failed: {e}")
        return None

    async def _check_tempmail_plus_inbox(self, email_addr: str, expected_length: Optional[int] = None) -> Optional[str]:
        try:
            q_email = urllib.parse.quote(email_addr, safe="@")
            result = await self._async_http(f"{self.TEMPMAIL_PLUS_BASE}/mails?email={q_email}&limit=5")
            for mail in result.get("mail_list", []):
                subject = mail.get("subject", "")
                body = mail.get("text", "") or mail.get("body", "")
                code = extract_telegram_code(f"{subject} {body}", expected_length=expected_length)
                if code:
                    return code
                msg_id = mail.get("mail_id") or mail.get("id")
                if msg_id:
                    detail = await self._async_http(f"{self.TEMPMAIL_PLUS_BASE}/mails/{msg_id}?email={q_email}")
                    d_text = detail.get("text", "") or detail.get("html", "")
                    d_code = extract_telegram_code(f"{subject} {d_text}", expected_length=expected_length)
                    if not d_code:
                        m = self.CODE_REGEX.search(d_text)
                        if m:
                            d_code = m.group(1)
                    if d_code:
                        return d_code
        except Exception as e:
            logger.warning(f"[TEMPMAIL] Tempmail.plus inbox check failed: {e}")
        return None

    async def _check_guerrillamail_inbox(self, expected_length: Optional[int] = None) -> Optional[str]:
        try:
            q_sid = urllib.parse.quote(self._guerrilla_sid or "")
            res = await self._async_http(f"{self.GUERRILLAMAIL_BASE}?f=get_email_list&offset=0&sid_token={q_sid}")
            for mail in res.get("list", []):
                mail_id = str(mail.get("mail_id") or "")
                sender = str(mail.get("mail_from") or "").lower()
                # Skip GuerrillaMail's default welcome email
                if mail_id == "1" or "no-reply@guerrillamail.com" in sender:
                    continue
                combined = f"{mail.get('mail_subject', '')} {mail.get('mail_excerpt', '')} {mail.get('mail_body', '')}"
                code = extract_telegram_code(combined, expected_length=expected_length)
                if code:
                    return code
                if mail_id:
                    detail = await self._async_http(
                        f"{self.GUERRILLAMAIL_BASE}?f=fetch_email&email_id={urllib.parse.quote(mail_id)}&sid_token={q_sid}"
                    )
                    d_comb = f"{detail.get('mail_subject', '')} {detail.get('mail_body', '')}"
                    d_code = extract_telegram_code(d_comb, expected_length=expected_length)
                    if d_code:
                        return d_code
        except Exception as e:
            logger.warning(f"[TEMPMAIL] GuerrillaMail inbox check failed: {e}")
        return None

    async def cleanup(self):
        """Best-effort delete temporary Mail.tm account."""
        if self._provider == "mailtm" and self._active_token and self._active_account_id:
            try:
                await self._async_http(
                    f"{self.MAILTM_BASE}/accounts/{self._active_account_id}",
                    headers={"Authorization": f"Bearer {self._active_token}"},
                    method="DELETE"
                )
            except Exception:
                pass
        self._active_email = None
        self._active_password = None
        self._active_token = None
        self._active_account_id = None
        self._guerrilla_sid = None
        self._provider = None


async def _guarded_telegram_email_send(coro_factory):
    """Serialize Telegram email-verification RPC calls with light pacing and shared FloodWait respect."""
    global _LAST_EMAIL_SEND_AT, _TG_EMAIL_COOLDOWN_UNTIL
    lock = _get_email_send_lock()
    async with lock:
        rem = _TG_EMAIL_COOLDOWN_UNTIL - time.time()
        if rem > 0:
            raise FloodWaitError(request=None, capture=max(1, int(rem)))
        gap = 2.5 - (time.monotonic() - _LAST_EMAIL_SEND_AT)
        if gap > 0:
            await asyncio.sleep(gap)
        try:
            return await coro_factory()
        except FloodWaitError as fw:
            secs = max(1, int(getattr(fw, "seconds", 30)))
            _TG_EMAIL_COOLDOWN_UNTIL = max(_TG_EMAIL_COOLDOWN_UNTIL, time.time() + min(secs, 300))
            raise
        finally:
            _LAST_EMAIL_SEND_AT = time.monotonic()


async def _bind_via_login_change(
    client: TelegramClient,
    temp_client: TempMailClient,
    target_email: str,
    otp_timeout: int
) -> Tuple[bool, Optional[str], str]:
    """
    Overwrite existing Telegram login email (purpose=EmailVerifyPurposeLoginChange).
    Returns: (ok, reason_code, message) where reason_code triggers fallback/retry when needed.
    """
    sent_code = await _guarded_telegram_email_send(
        lambda: client(tele_functions.account.SendVerifyEmailCodeRequest(
            purpose=tele_types.EmailVerifyPurposeLoginChange(),
            email=target_email
        ))
    )
    exp_len = getattr(sent_code, "length", None)
    code = await temp_client.fetch_verification_code(
        target_email=target_email,
        timeout=otp_timeout,
        expected_length=exp_len
    )
    await client(tele_functions.account.VerifyEmailRequest(
        purpose=tele_types.EmailVerifyPurposeLoginChange(),
        verification=tele_types.EmailVerificationCode(code=code)
    ))
    return True, "ok", f"Added ({target_email})"


async def _bind_via_2fa_recovery(
    client: TelegramClient,
    temp_client: TempMailClient,
    target_email: str,
    otp_timeout: int,
    known_2fa_password: Optional[str] = "Tgbitz"
) -> Tuple[bool, Optional[str], str]:
    """
    Bind or overwrite 2FA Recovery Email using account.UpdatePasswordSettingsRequest +
    account.ConfirmPasswordEmailRequest (works when login_email_pattern is NOT set / EMAIL_NOT_SETUP).
    """
    pwd = await client(tele_functions.account.GetPasswordRequest())
    if hasattr(pwd.new_algo, "salt1"):
        pwd.new_algo.salt1 += os.urandom(32)

    has_pw = bool(getattr(pwd, "has_password", False))
    if has_pw and known_2fa_password:
        pw_check = tele_password.compute_check(pwd, known_2fa_password)
        new_hash = tele_password.compute_digest(pwd.new_algo, known_2fa_password)
    elif not has_pw:
        pw_check = tele_types.InputCheckPasswordEmpty()
        new_hash = tele_password.compute_digest(pwd.new_algo, known_2fa_password) if known_2fa_password else b""
    else:
        return False, "fatal", "Existing 2FA password unknown"

    try:
        await _guarded_telegram_email_send(
            lambda: client(tele_functions.account.UpdatePasswordSettingsRequest(
                password=pw_check,
                new_settings=tele_types.account.PasswordInputSettings(
                    new_algo=pwd.new_algo,
                    new_password_hash=new_hash,
                    hint="",
                    email=target_email,
                    new_secure_settings=None,
                )
            ))
        )
        return True, "ok", f"Recovery Added ({target_email})"
    except tele_errors.EmailUnconfirmedError as euc:
        exp_len = getattr(euc, "code_length", None)
        try:
            code = await temp_client.fetch_verification_code(
                target_email=target_email,
                timeout=otp_timeout,
                expected_length=exp_len
            )
        except TimeoutError:
            try:
                await client(tele_functions.account.CancelPasswordEmailRequest())
            except Exception:
                pass
            raise
        await client(tele_functions.account.ConfirmPasswordEmailRequest(code=str(code)))
        return True, "ok", f"Recovery Added ({target_email})"


async def _sleep_with_keepalive(client: Optional[TelegramClient], wait_secs: float):
    """Sleeps through FloodWait / 429 cooldowns in 10s chunks while keeping the session alive."""
    remaining = max(0.0, float(wait_secs))
    while remaining > 0:
        chunk = min(10.0, remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk
        if remaining > 0 and client is not None:
            try:
                is_conn = client.is_connected()
                if not is_conn and hasattr(client, "connect"):
                    await client.connect()
            except Exception:
                pass


async def bind_free_temp_email(
    client: TelegramClient,
    max_attempts: int = 6,
    otp_timeout: int = 40,
    known_2fa_password: Optional[str] = "Tgbitz",
    wait_on_rate_limit: bool = True,
    max_rate_limit_wait: int = 900,
) -> Tuple[bool, Optional[str], str]:
    """
    Attempts to bind/overwrite Telegram email using free automated temp mail:
    - If `login_email_pattern` already exists on the account, overwrites it via `EmailVerifyPurposeLoginChange`.
    - If `login_email_pattern` is absent (or Telegram returns `EMAIL_NOT_SETUP`), binds a verified 2FA Recovery Email
      via `UpdatePasswordSettingsRequest` + `ConfirmPasswordEmailRequest` instead of failing with `EMAIL_NOT_SETUP`.
    - When `wait_on_rate_limit=True`, waits out Telegram FloodWait and HTTP 429 cooldowns up to
      `max_rate_limit_wait` seconds instead of skipping email binding.

    Returns: (bound_ok, email_or_pattern, status_label)
    """
    prev_pattern = None
    try:
        pwd = await client(tele_functions.account.GetPasswordRequest())
        prev_pattern = getattr(pwd, "login_email_pattern", None)
    except Exception:
        pass

    use_recovery_mode = not bool(prev_pattern)
    temp_client = TempMailClient()
    rejected_domains = set()
    last_err = "Skipped"
    bound_ok = False
    deadline = time.time() + 110.0
    waited_rate_limit = 0.0
    rate_limit_retries = 0
    attempt = 0

    async def _maybe_wait_rate_limit(secs: float, reason_label: str) -> bool:
        global _TG_EMAIL_COOLDOWN_UNTIL
        nonlocal deadline, waited_rate_limit, rate_limit_retries, last_err
        sleep_s = max(1.5, float(secs) + 1.5)
        if not wait_on_rate_limit or rate_limit_retries >= 6 or (waited_rate_limit + sleep_s) > max_rate_limit_wait:
            last_err = reason_label
            return False
        rate_limit_retries += 1
        waited_rate_limit += sleep_s
        logger.warning(f"[TEMPMAIL] {reason_label} — waiting {sleep_s:.1f}s to complete mandatory email change (retry {rate_limit_retries}/6)...")
        await _sleep_with_keepalive(client, sleep_s)
        if "floodwait" in reason_label.lower():
            _TG_EMAIL_COOLDOWN_UNTIL = 0.0
        deadline = max(deadline, time.time() + 55.0)
        return True

    try:
        while attempt < max_attempts:
            remaining_total = deadline - time.time()
            if remaining_total < 8.0:
                if wait_on_rate_limit and rate_limit_retries > 0 and waited_rate_limit < max_rate_limit_wait:
                    deadline = time.time() + 55.0
                    remaining_total = 55.0
                else:
                    break

            # Respect active Telegram email send cooldown by waiting cleanly first
            tg_cd = _TG_EMAIL_COOLDOWN_UNTIL - time.time()
            if tg_cd > 0:
                if not await _maybe_wait_rate_limit(tg_cd, f"FloodWait {int(tg_cd)}s"):
                    break

            # Respect HTTP provider cooldowns if ALL providers are simultaneously throttled
            now = time.time()
            min_http_cd = min(
                TempMailClient._mailtm_cooldown_until,
                TempMailClient._tempmail_plus_cooldown_until,
                TempMailClient._guerrilla_cooldown_until,
            ) - now
            if min_http_cd > 0:
                await _maybe_wait_rate_limit(min(min_http_cd, 65.0), f"HTTP 429 cooldown {int(min_http_cd)}s")

            attempt += 1
            try:
                target_email = await temp_client.create_temp_email(exclude_domains=list(rejected_domains))
            except Exception as e:
                last_err = f"TempMail unavailable: {e}"
                break

            domain = target_email.split("@")[-1].lower() if "@" in target_email else ""
            per_attempt_timeout = min(otp_timeout, max(15, int(deadline - time.time())))

            try:
                if not use_recovery_mode:
                    ok, _, label = await _bind_via_login_change(client, temp_client, target_email, per_attempt_timeout)
                    if ok:
                        bound_ok = True
                        return True, target_email, label
                else:
                    ok, reason_code, label = await _bind_via_2fa_recovery(
                        client, temp_client, target_email, per_attempt_timeout, known_2fa_password=known_2fa_password
                    )
                    if ok:
                        bound_ok = True
                        return True, target_email, label
                    if reason_code == "fatal":
                        last_err = label
                        break
            except FloodWaitError as fw:
                secs = max(1, int(getattr(fw, "seconds", 0) or max(1, int(_TG_EMAIL_COOLDOWN_UNTIL - time.time()))))
                await temp_client.cleanup()
                if await _maybe_wait_rate_limit(secs, f"FloodWait {secs}s"):
                    attempt = max(0, attempt - 1)
                    continue
                break
            except TimeoutError:
                last_err = f"OTP timeout (@{domain})"
                logger.warning(f"[TEMPMAIL] Attempt {attempt}/{max_attempts}: {last_err}")
                rejected_domains.add(domain)
                await temp_client.cleanup()
                if attempt < max_attempts:
                    await asyncio.sleep(1.0)
                    continue
            except Exception as err:
                err_s = str(err)
                err_upper = err_s.upper()
                logger.warning(f"[TEMPMAIL] Attempt {attempt}/{max_attempts} (@{domain}) error: {err_s[:140]}")

                # Automatically switch from LoginChange -> 2FA Recovery Email on EMAIL_NOT_SETUP
                if "EMAIL_NOT_SETUP" in err_upper and not use_recovery_mode:
                    use_recovery_mode = True
                    try:
                        ok, reason_code, label = await _bind_via_2fa_recovery(
                            client, temp_client, target_email, per_attempt_timeout, known_2fa_password=known_2fa_password
                        )
                        if ok:
                            bound_ok = True
                            return True, target_email, label
                        if reason_code == "fatal":
                            last_err = label
                            break
                    except TimeoutError:
                        last_err = f"OTP timeout (@{domain})"
                        rejected_domains.add(domain)
                        await temp_client.cleanup()
                        if attempt < max_attempts:
                            await asyncio.sleep(1.0)
                            continue
                    except FloodWaitError as fw:
                        secs = max(1, int(getattr(fw, "seconds", 0) or max(1, int(_TG_EMAIL_COOLDOWN_UNTIL - time.time()))))
                        await temp_client.cleanup()
                        if await _maybe_wait_rate_limit(secs, f"FloodWait {secs}s"):
                            attempt = max(0, attempt - 1)
                            continue
                        break
                    except Exception as rec_err:
                        err_s = str(rec_err)
                        err_upper = err_s.upper()

                if any(k in err_upper for k in ("PASSWORD_HASH_INVALID",)):
                    last_err = "2FA password mismatch"
                    break

                if any(k in err_upper for k in ("FLOOD_WAIT", "TOO_MANY", "429")):
                    await temp_client.cleanup()
                    if await _maybe_wait_rate_limit(20.0, f"Rate limit: {err_s[:40]}"):
                        attempt = max(0, attempt - 1)
                        continue
                    break

                if any(k in err_upper for k in ("EMAIL_NOT_ALLOWED", "EMAIL_INVALID", "EMAIL_NOT_SETUP", "BAD_REQUEST", "RPCERROR", "400")):
                    last_err = "Domain rejected" if ("EMAIL_NOT_ALLOWED" in err_upper or "EMAIL_INVALID" in err_upper) else err_s[:60]
                    rejected_domains.add(domain)
                    await temp_client.cleanup()
                    if attempt < max_attempts:
                        await asyncio.sleep(1.0)
                        continue
                else:
                    last_err = err_s[:60]
                break

            await temp_client.cleanup()
    finally:
        if not bound_ok:
            await temp_client.cleanup()

    if prev_pattern:
        return False, prev_pattern, f"Existing ({prev_pattern}) [Change failed: {last_err}]"
    return False, None, f"Not Added ({last_err})"
