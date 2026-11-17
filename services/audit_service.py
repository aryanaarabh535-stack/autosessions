import logging
from typing import Dict, Any, Optional
from database.repository import DatabaseRepository

logger = logging.getLogger("tgbitz.autosell.audit")


class AuditService:
    def __init__(self, repo: Optional[DatabaseRepository] = None):
        self.repo = repo if repo is not None else DatabaseRepository()

    def log_event(self, actor_id: int, action: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        # Strict sanitization: ensure no passwords, full sessions, or auth codes exist in metadata
        clean_meta = {}
        for k, v in (metadata or {}).items():
            k_low = k.lower()
            if any(secret in k_low for secret in ("pass", "token", "auth_key", "secret", "cookie", "otp", "code")):
                clean_meta[k] = "[REDACTED]"
            elif isinstance(v, str) and len(v) > 256:
                clean_meta[k] = f"{v[:64]}... [TRUNCATED]"
            else:
                clean_meta[k] = v

        eid = self.repo.record_audit(actor_id, action, clean_meta)
        logger.info(f"📋 [AUDIT {eid}] User {actor_id} -> {action} | {clean_meta}")
        return eid

    def log_upload(self, actor_id: int, upload_id: str, filename: str, total_files: int, supported_files: int, fingerprint: str):
        return self.log_event(actor_id, "ARCHIVE_UPLOADED", {
            "upload_id": upload_id,
            "filename": filename,
            "total_files": total_files,
            "supported_files": supported_files,
            "fingerprint": fingerprint[:16] + "..."
        })

    def log_job_stage(self, actor_id: int, job_id: str, stage: str, metadata: Optional[dict] = None):
        meta = {"job_id": job_id, "stage": stage}
        if metadata:
            meta.update(metadata)
        return self.log_event(actor_id, f"JOB_STAGE_{stage.upper()}", meta)

    def log_security_rejection(self, actor_id: int, reason_code: str, details: str):
        return self.log_event(actor_id, "SECURITY_REJECTION", {
            "reason_code": reason_code,
            "details": details
        })
