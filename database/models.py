import time
from typing import Optional, Dict, Any, List
from enum import Enum
from pydantic import BaseModel, Field


class UserStatus(str, Enum):
    ACTIVE = "active"
    BANNED = "banned"


class JobStage(str, Enum):
    QUEUED = "queued"
    VALIDATING = "validating"
    DISTRIBUTING = "distributing"
    SELLING = "selling"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class UserRecord(BaseModel):
    telegram_user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    registration_date: float = Field(default_factory=time.time)
    status: UserStatus = UserStatus.ACTIVE
    is_admin: bool = False


class UploadRecord(BaseModel):
    upload_id: str
    user_id: int
    filename: str
    upload_time: float = Field(default_factory=time.time)
    archive_fingerprint: str
    total_files: int = 0
    supported_files: int = 0
    invalid_files: int = 0
    processing_status: str = "completed"  # or rejected, failed


class JobRecord(BaseModel):
    job_id: str
    upload_id: str
    user_id: int
    current_stage: JobStage = JobStage.QUEUED
    created_at: float = Field(default_factory=time.time)
    completed_at: Optional[float] = None
    safe_error_code: Optional[str] = None
    total_accounts: int = 0
    sold_accounts: int = 0
    failed_accounts: int = 0
    total_payout_usd: float = 0.0
    operator_distribution: Dict[str, int] = Field(default_factory=dict)
    receiver_distribution: Dict[str, int] = Field(default_factory=dict)
    selected_receiver: Optional[str] = None  # None = auto highest paying


class AuditEvent(BaseModel):
    event_id: str
    actor_id: int
    action: str
    timestamp: float = Field(default_factory=time.time)
    non_sensitive_metadata: Dict[str, Any] = Field(default_factory=dict)


class SessionItem(BaseModel):
    session_id: str
    job_id: str
    upload_id: str
    user_id: int
    phone: Optional[str] = None
    phone_masked: str = "Hidden"
    country_iso: str = "XX"
    session_format: str = "telethon_sqlite"  # or string, json_pair, tdata
    session_encrypted: Optional[str] = None
    temp_file_rel: Optional[str] = None
    assigned_operator_id: Optional[str] = None
    target_receiver_id: Optional[str] = None
    target_receiver_username: Optional[str] = None
    status: str = "pending"  # pending, cleaning, selling, sold, failed
    payout_usd: float = 0.0
    error_msg: Optional[str] = None


class OperatorRecord(BaseModel):
    operator_id: str
    phone: str
    session_encrypted: Optional[str] = None
    is_active: bool = True
    daily_sales: int = 0
    max_daily_sales: int = 50
    last_used: float = 0.0
