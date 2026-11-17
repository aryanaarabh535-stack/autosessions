"""
TG BITZ — Cryptographic Envelope Encryption at Rest (Bot/encryption.py)
Provides authenticated AES-256-GCM envelope encryption for sensitive credentials
(2FA passwords, Telethon session blobs, and string sessions) at rest.
Strictly requires a dedicated ENCRYPTION_KEY in environment with fail-closed semantics.
"""

import os
import base64
import logging
import hashlib
from datetime import datetime, timezone
from typing import Optional, Union, Dict, Any, List
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

logger = logging.getLogger("tgbitz.encryption")

_PREFIX = "enc:v1:"
_PREFIX_BYTES = b"enc:v1:"

PLACEHOLDER_SUBSTRINGS = (
    "your_", "YOUR_", "changeme", "placeholder", "00000000",
    "12345678", "encryption_key_here", "your_64_hex"
)

def is_placeholder_key(key_str: Optional[str]) -> bool:
    """Detects placeholder, dummy, or insufficient entropy encryption keys."""
    if not key_str:
        return True
    s = key_str.strip()
    s_lower = s.lower()
    if any(p.lower() in s_lower for p in PLACEHOLDER_SUBSTRINGS):
        return True
    # If the key is hex-encoded (e.g. 64-char token_hex(32)), alphabet size is only 16.
    # Requiring 16 distinct chars rejects ~23.6% of valid random keys.
    is_hex = len(s) >= 32 and all(c in "0123456789abcdef" for c in s_lower)
    if is_hex:
        if len(set(s_lower)) < 8:
            return True
    else:
        if len(set(s)) < 12:
            return True
    return False

class EncryptionError(Exception):
    """Raised when encryption fails (fail-closed protection against plaintext leaks)."""
    pass

class DecryptionError(EncryptionError):
    """Raised when decryption fails due to corrupted, truncated, or tampered ciphertext."""
    pass

def _derive_master_key() -> bytes:
    """
    Resolves the 256-bit AES-GCM key from ENCRYPTION_KEY env var or authoritative config.
    Strictly fails if ENCRYPTION_KEY is missing, a known placeholder, or weak (< 32 characters).
    Never falls back to BOT_TOKEN or hard-coded default strings.
    """
    raw_env = os.environ.get("ENCRYPTION_KEY")
    if raw_env is None:
        try:
            import config
            raw_env = getattr(config, "ENCRYPTION_KEY", "")
        except Exception:
            raw_env = ""
    raw_env = (raw_env or "").strip()
    if not raw_env:
        raise RuntimeError(
            "CRITICAL: ENCRYPTION_KEY environment variable is missing or empty. "
            "A dedicated 256-bit encryption key is strictly required to secure credentials at rest."
        )
    if is_placeholder_key(raw_env):
        raise ValueError(
            "CRITICAL: ENCRYPTION_KEY is a placeholder or has insufficient entropy. "
            "Generate a secure key with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if len(raw_env) < 32:
        raise ValueError(
            "CRITICAL: ENCRYPTION_KEY is too weak. "
            "Must be at least 32 characters or a 64-character hex string (256-bit key)."
        )

    # 1. 64-character hex string (32 bytes)
    if len(raw_env) == 64:
        try:
            return bytes.fromhex(raw_env)
        except ValueError:
            pass

    # 2. Base64 encoded 32-byte key
    try:
        decoded = base64.b64decode(raw_env)
        if len(decoded) == 32:
            return decoded
    except Exception:
        pass

    # 3. Derive 256-bit key via HKDF-SHA256
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"tgbitz-envelope-salt-v1",
        info=b"tgbitz-master-key"
    )
    return hkdf.derive(raw_env.encode("utf-8"))

_KEY: Optional[bytes] = None
_AESGCM: Optional[AESGCM] = None
_LEGACY_KEYS: Optional[List[bytes]] = None

def _get_aesgcm() -> AESGCM:
    """Lazy-initializes AESGCM cipher with validated 256-bit master key."""
    global _KEY, _AESGCM
    if _AESGCM is None:
        _KEY = _derive_master_key()
        _AESGCM = AESGCM(_KEY)
    return _AESGCM

def _get_legacy_keys() -> List[bytes]:
    """Returns list of derived 32-byte legacy decryption keys if configured."""
    global _LEGACY_KEYS
    if _LEGACY_KEYS is None:
        _LEGACY_KEYS = []
        raw = os.getenv("LEGACY_ENCRYPTION_KEYS", "").strip()
        if raw:
            for k in raw.split(","):
                k = k.strip()
                if not k or is_placeholder_key(k):
                    continue
                try:
                    if len(k) == 64:
                        _LEGACY_KEYS.append(bytes.fromhex(k))
                    else:
                        hkdf = HKDF(
                            algorithm=hashes.SHA256(),
                            length=32,
                            salt=b"tgbitz-envelope-salt-v1",
                            info=b"tgbitz-master-key"
                        )
                        _LEGACY_KEYS.append(hkdf.derive(k.encode("utf-8")))
                except Exception:
                    pass
    return _LEGACY_KEYS

def get_encryption_fingerprint() -> str:
    """Returns the first 8 hex characters of the SHA-256 hash of the master key."""
    try:
        _get_aesgcm()
        global _KEY
        if _KEY:
            return hashlib.sha256(_KEY).hexdigest()[:8]
    except Exception:
        pass
    return "NOT_CONFIGURED"

def reset_encryption_key_cache():
    """Resets the cached cipher instance (useful for key rotation or testing)."""
    global _KEY, _AESGCM, _LEGACY_KEYS
    _KEY = None
    _AESGCM = None
    _LEGACY_KEYS = None

def verify_or_init_encryption_sentinel(db) -> Dict[str, Any]:
    """
    Verifies that the active ENCRYPTION_KEY matches the key used to encrypt existing data.
    Maintains a sentinel document in the 'system' collection with an encrypted constant
    and the key's SHA-256 fingerprint.
    Refuses to start if a key mismatch or decryption failure occurs.
    """
    if db is None:
        return {"status": "skipped", "reason": "no_db"}

    fp = get_encryption_fingerprint()
    if fp == "NOT_CONFIGURED":
        raise RuntimeError("CRITICAL: Cannot verify encryption sentinel without configured ENCRYPTION_KEY.")

    sentinel_col = db["system"]
    doc = sentinel_col.find_one({"_id": "enc_check"})

    if not doc:
        # Initialize sentinel on first run
        new_doc = {
            "_id": "enc_check",
            "fingerprint": fp,
            "sentinel": encrypt_string("tgbitz_sentinel_valid_v1"),
            "created_at": datetime.now(timezone.utc)
        }
        sentinel_col.insert_one(new_doc)
        logger.info(f"✓ Initialized encryption sentinel in system collection (fingerprint: {fp}).")
        return {"status": "initialized", "fingerprint": fp}

    stored_sentinel = doc.get("sentinel")
    try:
        decrypted = decrypt_string(stored_sentinel)
        if decrypted != "tgbitz_sentinel_valid_v1":
            raise DecryptionError("Decrypted sentinel content mismatch")
    except Exception as e:
        logger.critical(
            f"FATAL: ENCRYPTION_KEY does not match the key used for existing data! "
            f"Stored fingerprint: {doc.get('fingerprint')}, current fingerprint: {fp}. Error: {e}"
        )
        raise RuntimeError("ENCRYPTION_KEY does not match the key used for existing data") from e

    logger.info(f"✓ Verified encryption key against database sentinel (fingerprint: {fp}).")
    return {"status": "verified", "fingerprint": fp}


def encrypt_string(plaintext: Optional[str]) -> Optional[str]:
    """
    Encrypts a string (such as a 2FA password or session string) using AES-256-GCM.
    Returns format: 'enc:v1:<base64(nonce + ciphertext_with_tag)>'.
    Fail-closed: raises EncryptionError on failure; NEVER returns or saves plaintext.
    """
    if plaintext is None or plaintext == "None":
        return None
    if not plaintext:
        return ""
    if isinstance(plaintext, str) and plaintext.startswith(_PREFIX):
        return plaintext  # Already encrypted

    try:
        cipher = _get_aesgcm()
        nonce = os.urandom(12)
        ct = cipher.encrypt(nonce, plaintext.encode("utf-8"), None)
        payload = nonce + ct
        return _PREFIX + base64.b64encode(payload).decode("ascii")
    except Exception as e:
        logger.error(f"Failed to encrypt credential: {e}")
        raise EncryptionError(f"Credential encryption failed: {e}") from e


def decrypt_string(ciphertext: Optional[str]) -> Optional[str]:
    """
    Decrypts an AES-256-GCM encrypted string.
    Returns plaintext as-is if unencrypted for legacy backward compatibility.
    Supports LEGACY_ENCRYPTION_KEYS as fallback decryption keys.
    Raises DecryptionError if encrypted payload is corrupted or authentication fails.
    """
    if ciphertext is None or ciphertext == "None":
        return None
    if not ciphertext:
        return ""
    if not isinstance(ciphertext, str) or not ciphertext.startswith(_PREFIX):
        return ciphertext  # Legacy plaintext backwards compatibility

    try:
        raw_b64 = ciphertext[len(_PREFIX):]
        payload = base64.b64decode(raw_b64)
        if len(payload) < 28: # 12 bytes nonce + 16 bytes auth tag
            raise DecryptionError("Ciphertext payload too short (truncated or corrupted)")
        nonce = payload[:12]
        ct = payload[12:]
        try:
            cipher = _get_aesgcm()
            pt = cipher.decrypt(nonce, ct, None)
            return pt.decode("utf-8")
        except Exception as primary_err:
            for lk in _get_legacy_keys():
                try:
                    pt = AESGCM(lk).decrypt(nonce, ct, None)
                    return pt.decode("utf-8")
                except Exception:
                    pass
            raise primary_err
    except Exception as e:
        logger.error(f"Failed to decrypt credential: {e}")
        raise DecryptionError(f"Credential decryption failed: {e}") from e


def encrypt_bytes(data: Optional[bytes]) -> Optional[bytes]:
    """
    Encrypts raw bytes (such as a SQLite .session binary file) using AES-256-GCM.
    Returns: b'enc:v1:' + nonce (12 bytes) + ciphertext_with_tag.
    Fail-closed: raises EncryptionError on failure; NEVER saves plaintext.
    """
    if data is None:
        return None
    if not data:
        return b""
    if isinstance(data, (bytes, bytearray, memoryview)) and bytes(data).startswith(_PREFIX_BYTES):
        return bytes(data) # Already encrypted

    try:
        raw = bytes(data)
        cipher = _get_aesgcm()
        nonce = os.urandom(12)
        ct = cipher.encrypt(nonce, raw, None)
        return _PREFIX_BYTES + nonce + ct
    except Exception as e:
        logger.error(f"Failed to encrypt session bytes: {e}")
        raise EncryptionError(f"Session bytes encryption failed: {e}") from e


def decrypt_bytes(data: Optional[Union[bytes, str]]) -> Optional[bytes]:
    """
    Decrypts AES-256-GCM encrypted session bytes.
    Returns raw bytes as-is if not encrypted for backwards compatibility.
    Supports LEGACY_ENCRYPTION_KEYS as fallback decryption keys.
    Raises DecryptionError if ciphertext is truncated, corrupted or authentication fails.
    """
    if data is None:
        return None
    if not data:
        return b""
    raw = bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) else data.encode("utf-8")
    if not raw.startswith(_PREFIX_BYTES):
        return raw

    try:
        payload = raw[len(_PREFIX_BYTES):]
        if len(payload) < 28:
            raise DecryptionError("Ciphertext payload too short (truncated or corrupted)")
        nonce = payload[:12]
        ct = payload[12:]
        try:
            cipher = _get_aesgcm()
            return cipher.decrypt(nonce, ct, None)
        except Exception as primary_err:
            for lk in _get_legacy_keys():
                try:
                    return AESGCM(lk).decrypt(nonce, ct, None)
                except Exception:
                    pass
            raise primary_err
    except Exception as e:
        logger.error(f"Failed to decrypt session bytes: {e}")
        raise DecryptionError(f"Session bytes decryption failed: {e}") from e


CREDENTIAL_STRING_FIELDS = ("password", "string_session", "two_fa", "two_factor_auth", "twofa", "hex_key", "session_data")


def encrypt_account_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Encrypts all sensitive credentials in an account document before saving to DB."""
    if not doc or not isinstance(doc, dict):
        return doc
    clean = doc.copy()
    for field in CREDENTIAL_STRING_FIELDS:
        if field in clean and clean[field]:
            clean[field] = encrypt_string(str(clean[field]))
    if "session_file" in clean and clean["session_file"]:
        clean["session_file"] = encrypt_bytes(clean["session_file"])
    return clean


def read_account(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Returns a decrypted copy of an account document and is the authoritative way handlers read credentials.
    If decryption fails: logs error, and masks corrupted credentials so ciphertext (e.g. 'enc:v1:...')
    is NEVER exposed to users.
    """
    if not doc or not isinstance(doc, dict):
        return doc
    clean = doc.copy()
    for field in CREDENTIAL_STRING_FIELDS:
        val = clean.get(field)
        if val is not None and isinstance(val, str) and val.startswith(("enc:v1:", "enc:v2:")):
            try:
                clean[field] = decrypt_string(val)
            except Exception as e:
                logger.critical(f"Failed to decrypt {field} for account {clean.get('_id')}: {e}")
                clean[field] = ""
    if "session_file" in clean and clean["session_file"]:
        val_sf = clean["session_file"]
        raw = bytes(val_sf) if isinstance(val_sf, (bytes, bytearray, memoryview)) else bytes(val_sf)
        if raw.startswith((b"enc:v1:", b"enc:v2:")):
            try:
                clean["session_file"] = decrypt_bytes(raw)
            except Exception as e:
                logger.critical(f"Failed to decrypt session_file for account {clean.get('_id')}: {e}")
                clean["session_file"] = b""
    return clean


decrypt_account_doc = read_account


def save_account(col, account_doc: Dict[str, Any], query: Optional[Dict[str, Any]] = None):
    """
    Unified, secure account & session writer: encrypts account credentials
    (passwords, session files, string sessions, 2FA, hex keys) at rest before saving.
    If query is provided, performs an update; otherwise performs insert_one.
    """
    enc_doc = encrypt_account_doc(account_doc)
    if query:
        return col.update_one(query, {"$set": enc_doc}, upsert=True)
    return col.insert_one(enc_doc)


save_session = save_account


def migrate_plaintext_accounts(db, dry_run: bool = False) -> Dict[str, Any]:
    """
    Scans accounts and inventory collections for unencrypted credentials and encrypts them at rest.
    Supports dry-run verification mode. Reports detailed counts.
    """
    stats = {
        "scanned": 0,
        "migrated": 0,
        "errors": 0,
        "plaintext_rows": 0,
        "enc_v1_readable": 0,
        "enc_v1_unreadable": 0,
        "numbers_migrated": 0,
        "receiver_migrated": 0,
        "orders_migrated": 0,
        "total_migrated": 0,
        "dry_run": dry_run
    }
    cols_to_check = ["numbers", "receiver_accounts", "lzt_orders", "accounts", "s1_accounts", "s4_accounts"]
    for col_name in cols_to_check:
        try:
            col = db[col_name]
            cursor = col.find({})
            for doc in cursor:
                stats["scanned"] += 1
                needs_update = {}
                is_plaintext = False
                for field in CREDENTIAL_STRING_FIELDS:
                    val = doc.get(field)
                    if val and isinstance(val, str):
                        if val.startswith(_PREFIX):
                            try:
                                decrypt_string(val)
                                stats["enc_v1_readable"] += 1
                            except Exception:
                                stats["enc_v1_unreadable"] += 1
                        else:
                            is_plaintext = True
                            needs_update[field] = encrypt_string(val)
                val_sf = doc.get("session_file")
                if val_sf:
                    raw_b = bytes(val_sf)
                    if raw_b.startswith(_PREFIX_BYTES):
                        try:
                            decrypt_bytes(raw_b)
                            stats["enc_v1_readable"] += 1
                        except Exception:
                            stats["enc_v1_unreadable"] += 1
                    else:
                        is_plaintext = True
                        needs_update["session_file"] = encrypt_bytes(raw_b)

                if is_plaintext:
                    stats["plaintext_rows"] += 1
                if needs_update:
                    if not dry_run:
                        col.update_one({"_id": doc["_id"]}, {"$set": needs_update})
                    stats["migrated"] += 1
                    if col_name == "numbers":
                        stats["numbers_migrated"] += 1
                    elif col_name == "receiver_accounts":
                        stats["receiver_migrated"] += 1
                    elif col_name == "lzt_orders":
                        stats["orders_migrated"] += 1
        except Exception as e:
            logger.warning(f"Plaintext migration scan on collection '{col_name}' warning: {e}")
            stats["errors"] += 1
    stats["total_migrated"] = stats["migrated"]
    return stats


# Convenience Aliases
encrypt_value = encrypt_string
decrypt_value = decrypt_string


