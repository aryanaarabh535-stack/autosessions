import os
import re
import json
import sqlite3
import uuid
from typing import List, Dict, Optional, Tuple
from database.models import SessionItem
from encryption import encrypt_value

DIAL_CODE_TO_ISO = {
    "1": "US", "7": "RU", "20": "EG", "27": "ZA", "30": "GR", "31": "NL", "32": "BE", "33": "FR",
    "34": "ES", "39": "IT", "40": "RO", "41": "CH", "43": "AT", "44": "GB", "45": "DK", "46": "SE",
    "47": "NO", "48": "PL", "49": "DE", "51": "PE", "52": "MX", "54": "AR", "55": "BR", "56": "CL",
    "57": "CO", "58": "VE", "60": "MY", "61": "AU", "62": "ID", "63": "PH", "64": "NZ", "65": "SG",
    "66": "TH", "81": "JP", "82": "KR", "84": "VN", "86": "CN", "90": "TR", "91": "IN", "92": "PK",
    "93": "AF", "94": "LK", "95": "MM", "98": "IR", "212": "MA", "213": "DZ", "216": "TN", "218": "LY",
    "225": "CI", "234": "NG", "254": "KE", "255": "TZ", "256": "UG", "351": "PT", "374": "AM",
    "380": "UA", "880": "BD", "961": "LB", "962": "JO", "963": "SY", "964": "IQ", "965": "KW",
    "966": "SA", "967": "YE", "971": "AE", "994": "AZ", "998": "UZ"
}


def mask_phone(phone: Optional[str]) -> str:
    if not phone:
        return "Unknown"
    digits = re.sub(r"[^\d+]", "", phone)
    if len(digits) <= 5:
        return digits
    prefix = digits[:5]
    suffix = digits[-3:] if len(digits) > 8 else digits[-2:]
    return f"{prefix}****{suffix}"


def detect_country_iso(phone: Optional[str]) -> str:
    if not phone:
        return "XX"
    digits = phone.lstrip("+").strip()
    # Check 3-digit, 2-digit, 1-digit dial codes
    for l in (3, 2, 1):
        pref = digits[:l]
        if pref in DIAL_CODE_TO_ISO:
            return DIAL_CODE_TO_ISO[pref]
    return "XX"


class SessionLoader:
    @staticmethod
    def load_sessions_from_dir(extracted_dir: str, job_id: str, upload_id: str, user_id: int) -> Tuple[List[SessionItem], Dict[str, int]]:
        """
        Scans extracted directory for session files and companion metadata JSONs.
        Generates encrypted SessionItem entries ready for distribution.
        """
        sessions_map: Dict[str, SessionItem] = {}
        metadata_map: Dict[str, dict] = {}

        # 1. Collect JSON metadata first
        for root, _, files in os.walk(extracted_dir):
            for file in files:
                if file.lower().endswith(".json"):
                    full_p = os.path.join(root, file)
                    base_name = os.path.splitext(file)[0].lower()
                    try:
                        with open(full_p, "r", encoding="utf-8", errors="ignore") as jf:
                            data = json.load(jf)
                            if isinstance(data, dict):
                                metadata_map[base_name] = data
                    except Exception:
                        pass

        # 2. Collect .session files
        for root, _, files in os.walk(extracted_dir):
            for file in files:
                if file.lower().endswith(".session"):
                    full_p = os.path.join(root, file)
                    base_name = os.path.splitext(file)[0].lower()
                    rel_p = os.path.relpath(full_p, extracted_dir)

                    meta = metadata_map.get(base_name, {})
                    phone = meta.get("phone") or meta.get("phone_number")
                    
                    # If not in JSON, extract phone from filename if numeric
                    if not phone:
                        num_m = re.search(r"(\+?\d{8,15})", base_name)
                        if num_m:
                            phone = num_m.group(1)

                    if phone and not phone.startswith("+"):
                        phone = f"+{phone}"

                    # Read binary session and encrypt
                    try:
                        with open(full_p, "rb") as sf:
                            session_bytes = sf.read()
                        
                        # Inspect Telethon/Pyrogram SQLite session for phone or auth key if needed
                        if not phone and len(session_bytes) > 1024:
                            try:
                                conn = sqlite3.connect(full_p)
                                cur = conn.cursor()
                                # Telethon sessions table
                                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
                                if cur.fetchone():
                                    cur.execute("SELECT dc_id, server_address, port, auth_key FROM sessions")
                                    row = cur.fetchone()
                                    if row:
                                        # Got valid telethon session
                                        pass
                                conn.close()
                            except Exception:
                                pass

                        session_hex = session_bytes.hex()
                        encrypted_str = encrypt_value(session_hex)
                    except Exception as e:
                        continue

                    iso = detect_country_iso(phone)
                    phone_m = mask_phone(phone)
                    sid = f"SES-{uuid.uuid4().hex[:10].upper()}"

                    item = SessionItem(
                        session_id=sid,
                        job_id=job_id,
                        upload_id=upload_id,
                        user_id=user_id,
                        phone=phone,
                        phone_masked=phone_m,
                        country_iso=iso,
                        session_format="telethon_sqlite",
                        session_encrypted=encrypted_str,
                        temp_file_rel=rel_p,
                        status="pending"
                    )
                    # Deduplicate by phone if known, else by relative path
                    key = phone if phone else rel_p
                    sessions_map[key] = item

        items = list(sessions_map.values())
        country_breakdown: Dict[str, int] = {}
        for it in items:
            country_breakdown[it.country_iso] = country_breakdown.get(it.country_iso, 0) + 1

        return items, country_breakdown
