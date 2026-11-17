import time
import uuid
from typing import Optional, List, Dict, Any
from .connection import get_sync_database
from .models import UserRecord, UserStatus, UploadRecord, JobRecord, JobStage, AuditEvent, SessionItem, OperatorRecord
import config


class DatabaseRepository:
    def __init__(self, db=None):
        self.db = db if db is not None else get_sync_database()

    # --- Users ---
    def get_or_create_user(self, user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> UserRecord:
        doc = self.db["users"].find_one({"telegram_user_id": user_id})
        is_admin = user_id in getattr(config, "ADMIN_IDS", [])
        if doc:
            # Update username/first_name if changed
            updates = {}
            if username and doc.get("username") != username:
                updates["username"] = username
            if first_name and doc.get("first_name") != first_name:
                updates["first_name"] = first_name
            if doc.get("is_admin") != is_admin:
                updates["is_admin"] = is_admin
            if updates:
                self.db["users"].update_one({"telegram_user_id": user_id}, {"$set": updates})
                doc.update(updates)
            return UserRecord.model_validate(doc)
        
        new_u = UserRecord(
            telegram_user_id=user_id,
            username=username,
            first_name=first_name,
            registration_date=time.time(),
            status=UserStatus.ACTIVE,
            is_admin=is_admin
        )
        self.db["users"].insert_one(new_u.model_dump())
        return new_u

    def set_user_status(self, user_id: int, status: UserStatus) -> bool:
        res = self.db["users"].update_one({"telegram_user_id": user_id}, {"$set": {"status": status.value}})
        return res.matched_count > 0

    def list_users(self, limit: int = 50) -> List[UserRecord]:
        return [UserRecord.model_validate(d) for d in self.db["users"].find().limit(limit)]

    # --- Uploads ---
    def create_upload(self, upload: UploadRecord) -> str:
        self.db["uploads"].insert_one(upload.model_dump())
        return upload.upload_id

    def get_upload_by_fingerprint(self, fingerprint: str) -> Optional[UploadRecord]:
        doc = self.db["uploads"].find_one({"archive_fingerprint": fingerprint})
        return UploadRecord.model_validate(doc) if doc else None

    def get_upload(self, upload_id: str) -> Optional[UploadRecord]:
        doc = self.db["uploads"].find_one({"upload_id": upload_id})
        return UploadRecord.model_validate(doc) if doc else None

    def list_user_uploads(self, user_id: int, limit: int = 20) -> List[UploadRecord]:
        return [UploadRecord.model_validate(d) for d in self.db["uploads"].find({"user_id": user_id}).sort("upload_time", -1).limit(limit)]

    # --- Jobs ---
    def create_job(self, job: JobRecord) -> str:
        self.db["jobs"].insert_one(job.model_dump())
        return job.job_id

    def update_job_stage(self, job_id: str, stage: JobStage, safe_error: Optional[str] = None, data_updates: Optional[dict] = None) -> bool:
        up = {"current_stage": stage.value}
        if safe_error:
            up["safe_error_code"] = safe_error
        if stage in (JobStage.COMPLETED, JobStage.FAILED, JobStage.REJECTED):
            up["completed_at"] = time.time()
        if data_updates:
            up.update(data_updates)
        res = self.db["jobs"].update_one({"job_id": job_id}, {"$set": up})
        return res.matched_count > 0

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        doc = self.db["jobs"].find_one({"job_id": job_id})
        return JobRecord.model_validate(doc) if doc else None

    def list_jobs(self, limit: int = 30) -> List[JobRecord]:
        return [JobRecord.model_validate(d) for d in self.db["jobs"].find().sort("created_at", -1).limit(limit)]

    def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        total_uploads = self.db["uploads"].count_documents({"user_id": user_id})
        jobs = list(self.db["jobs"].find({"user_id": user_id}))
        total_accounts = sum(j.get("total_accounts", 0) for j in jobs)
        sold_accounts = sum(j.get("sold_accounts", 0) for j in jobs)
        failed_accounts = sum(j.get("failed_accounts", 0) for j in jobs)
        total_payout = sum(j.get("total_payout_usd", 0.0) for j in jobs)
        return {
            "total_uploads": total_uploads,
            "total_jobs": len(jobs),
            "total_accounts": total_accounts,
            "sold_accounts": sold_accounts,
            "failed_accounts": failed_accounts,
            "total_payout_usd": round(total_payout, 2)
        }

    def get_system_metrics(self) -> Dict[str, Any]:
        return {
            "total_users": self.db["users"].count_documents({}),
            "total_uploads": self.db["uploads"].count_documents({}),
            "valid_archives": self.db["uploads"].count_documents({"processing_status": "completed"}),
            "rejected_archives": self.db["uploads"].count_documents({"processing_status": "rejected"}),
            "total_jobs": self.db["jobs"].count_documents({}),
            "completed_jobs": self.db["jobs"].count_documents({"current_stage": JobStage.COMPLETED.value}),
            "failed_jobs": self.db["jobs"].count_documents({"current_stage": JobStage.FAILED.value}),
        }

    # --- Audit Logs ---
    def record_audit(self, actor_id: int, action: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        eid = f"AUD-{uuid.uuid4().hex[:12].upper()}"
        event = AuditEvent(
            event_id=eid,
            actor_id=actor_id,
            action=action,
            timestamp=time.time(),
            non_sensitive_metadata=metadata or {}
        )
        self.db["audit_logs"].insert_one(event.model_dump())
        return eid

    def list_audit_logs(self, limit: int = 50) -> List[AuditEvent]:
        return [AuditEvent.model_validate(d) for d in self.db["audit_logs"].find().sort("timestamp", -1).limit(limit)]

    # --- Session Items ---
    def save_session_items(self, items: List[SessionItem]):
        if not items:
            return
        docs = [it.model_dump() for it in items]
        self.db["session_items"].insert_many(docs)

    def get_session_items_for_job(self, job_id: str) -> List[SessionItem]:
        return [SessionItem.model_validate(d) for d in self.db["session_items"].find({"job_id": job_id})]

    def update_session_item(self, session_id: str, updates: dict):
        self.db["session_items"].update_one({"session_id": session_id}, {"$set": updates})

    # --- Operators ---
    def list_operators(self, active_only: bool = True) -> List[OperatorRecord]:
        q = {"is_active": True} if active_only else {}
        return [OperatorRecord.model_validate(d) for d in self.db["operators"].find(q)]

    def upsert_operator(self, operator: OperatorRecord):
        self.db["operators"].update_one(
            {"operator_id": operator.operator_id},
            {"$set": operator.model_dump()},
            upsert=True
        )

    def remove_operator(self, operator_id: str) -> bool:
        res = self.db["operators"].delete_one({"operator_id": operator_id})
        return res.deleted_count > 0
