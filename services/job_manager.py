import time
import uuid
from typing import Optional, Dict, List, Any
from database.models import JobRecord, JobStage, SessionItem
from database.repository import DatabaseRepository
from .audit_service import AuditService


class JobManager:
    def __init__(self, repo: Optional[DatabaseRepository] = None, audit: Optional[AuditService] = None):
        self.repo = repo if repo is not None else DatabaseRepository()
        self.audit = audit if audit is not None else AuditService(self.repo)

    def create_job_from_upload(
        self,
        user_id: int,
        upload_id: str,
        items: List[SessionItem],
        country_breakdown: Dict[str, int]
    ) -> JobRecord:
        job_id = f"JOB-{uuid.uuid4().hex[:8].upper()}"
        job = JobRecord(
            job_id=job_id,
            upload_id=upload_id,
            user_id=user_id,
            current_stage=JobStage.VALIDATING,
            created_at=time.time(),
            total_accounts=len(items)
        )
        self.repo.create_job(job)
        self.repo.save_session_items(items)
        self.audit.log_job_stage(user_id, job_id, "VALIDATING", {
            "total_accounts": len(items),
            "countries": country_breakdown
        })
        return job

    def configure_distribution(
        self,
        job_id: str,
        actor_id: int,
        operator_distribution: Dict[str, int],
        selected_receiver: Optional[str] = None,
        receiver_limits: Optional[Dict[str, int]] = None
    ) -> bool:
        """
        Assigns operator limits and receiver limits to the job, transitioning it to DISTRIBUTING.
        """
        job = self.repo.get_job(job_id)
        if not job:
            return False

        updates = {
            "operator_distribution": operator_distribution,
            "receiver_distribution": receiver_limits or {},
            "selected_receiver": selected_receiver,
            "current_stage": JobStage.DISTRIBUTING.value
        }
        self.repo.update_job_stage(job_id, JobStage.DISTRIBUTING, data_updates=updates)

        # Distribute items to operators
        items = self.repo.get_session_items_for_job(job_id)
        pending_items = [it for it in items if it.status == "pending"]

        # 1. Distribute across operators according to quotas
        curr_idx = 0
        for op_id, count in operator_distribution.items():
            assigned_slice = pending_items[curr_idx: curr_idx + count]
            for it in assigned_slice:
                self.repo.update_session_item(it.session_id, {
                    "assigned_operator_id": op_id,
                    "target_receiver_id": selected_receiver if selected_receiver != "auto" else None
                })
            curr_idx += count

        # If any remaining items without explicit limit, assign round-robin across available operators
        op_keys = list(operator_distribution.keys())
        if op_keys and curr_idx < len(pending_items):
            for i, it in enumerate(pending_items[curr_idx:]):
                chosen_op = op_keys[i % len(op_keys)]
                self.repo.update_session_item(it.session_id, {
                    "assigned_operator_id": chosen_op,
                    "target_receiver_id": selected_receiver if selected_receiver != "auto" else None
                })

        self.audit.log_event(actor_id, "DISTRIBUTION_CONFIGURED", {
            "job_id": job_id,
            "operators": operator_distribution,
            "selected_receiver": selected_receiver or "auto",
            "receiver_limits": receiver_limits or {}
        })
        return True

    def mark_selling_started(self, job_id: str, actor_id: int):
        self.repo.update_job_stage(job_id, JobStage.SELLING)
        self.audit.log_job_stage(actor_id, job_id, "SELLING")

    def complete_job(self, job_id: str, actor_id: int, sold_count: int, failed_count: int, total_payout: float):
        stage = JobStage.COMPLETED if sold_count > 0 or failed_count == 0 else JobStage.FAILED
        self.repo.update_job_stage(
            job_id, stage,
            data_updates={
                "sold_accounts": sold_count,
                "failed_accounts": failed_count,
                "total_payout_usd": round(total_payout, 2),
                "completed_at": time.time()
            }
        )
        self.audit.log_job_stage(actor_id, job_id, stage.value, {
            "sold": sold_count,
            "failed": failed_count,
            "payout_usd": round(total_payout, 2)
        })
