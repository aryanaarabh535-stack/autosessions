import os
import uuid
import tempfile
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database.repository import DatabaseRepository
from database.models import UploadRecord, JobRecord, JobStage
from services.zip_validator import ZipValidator
from services.session_loader import SessionLoader
from services.audit_service import AuditService
from services.job_manager import JobManager
import config

router = Router()
repo = DatabaseRepository()
audit = AuditService(repo)
validator = ZipValidator()
job_manager = JobManager(repo, audit)


@router.message(Command("upload"))
async def cmd_upload_prompt(message: types.Message):
    await message.reply(
        "📤 <b>Please upload your <code>.zip</code> archive.</b>\n\n"
        "Ensure the archive contains <code>.session</code> or paired <code>.json</code> files.\n"
        "Maximum compressed size: <b>50 MB</b>.",
        parse_mode="HTML"
    )


@router.message(F.document)
async def handle_document_upload(message: types.Message):
    doc = message.document
    if not doc:
        return

    filename = doc.file_name or "archive.zip"
    if not filename.lower().endswith(".zip"):
        await message.reply(
            "❌ <b>Invalid File Format</b>\nOnly <code>.zip</code> archives are accepted.",
            parse_mode="HTML"
        )
        return

    # Check compressed size before download
    if doc.file_size and doc.file_size > config.MAX_ARCHIVE_SIZE_BYTES:
        await message.reply(
            f"❌ <b>Archive Too Large</b>\nMaximum allowed archive size is {config.MAX_ARCHIVE_SIZE_BYTES // (1024*1024)} MB.",
            parse_mode="HTML"
        )
        return

    user = message.from_user
    user_id = user.id if user else 0
    u_rec = repo.get_or_create_user(user_id, user.username if user else None, user.first_name if user else None)
    if u_rec.status.value == "banned":
        await message.reply("⛔ Your account has been suspended from using this service.")
        return

    status_msg = await message.reply("⏳ <i>Downloading and validating archive security...</i>", parse_mode="HTML")

    upload_id = f"A{uuid.uuid4().hex[:6].upper()}"
    temp_zip_path = os.path.join(tempfile.gettempdir(), f"upload_{upload_id}.zip")

    try:
        # Download document
        bot = message.bot
        file_info = await bot.get_file(doc.file_id)
        await bot.download_file(file_info.file_path, destination=temp_zip_path)

        # 1. Validate & Extract safely
        val_res = validator.validate_and_extract(temp_zip_path, upload_id)
        if not val_res.is_valid:
            audit.log_security_rejection(user_id, val_res.safe_error_code or "REJECTED", val_res.safe_error_message or "")
            await status_msg.edit_text(
                f"❌ <b>Archive Validation Failed</b>\n\n"
                f"• <b>Code:</b> <code>{val_res.safe_error_code}</code>\n"
                f"• <b>Reason:</b> {val_res.safe_error_message}",
                parse_mode="HTML"
            )
            return

        # 2. Duplicate Archive Detection
        existing_up = repo.get_upload_by_fingerprint(val_res.fingerprint)
        if existing_up:
            await status_msg.edit_text(
                f"⚠️ <b>Duplicate Archive Detected</b>\n\n"
                f"This identical archive was previously processed under Upload ID <code>#{existing_up.upload_id}</code>.\n"
                f"Please upload a new batch or contact support.",
                parse_mode="HTML"
            )
            validator.cleanup_extracted(val_res.extracted_dir)
            return

        # 3. Load & Inventory Sessions
        job_id = f"JOB-{uuid.uuid4().hex[:8].upper()}"
        session_items, country_breakdown = SessionLoader.load_sessions_from_dir(
            val_res.extracted_dir, job_id=job_id, upload_id=upload_id, user_id=user_id
        )

        if not session_items:
            await status_msg.edit_text(
                "❌ <b>No Valid Sessions Found</b>\n"
                "The archive was extracted safely, but no readable Telegram sessions could be loaded.",
                parse_mode="HTML"
            )
            validator.cleanup_extracted(val_res.extracted_dir)
            return

        # 4. Save Upload & Job Records
        up_rec = UploadRecord(
            upload_id=upload_id,
            user_id=user_id,
            filename=filename,
            archive_fingerprint=val_res.fingerprint,
            total_files=val_res.total_files,
            supported_files=val_res.supported_files,
            invalid_files=val_res.invalid_files,
            processing_status="completed"
        )
        repo.create_upload(up_rec)

        job_rec = JobRecord(
            job_id=job_id,
            upload_id=upload_id,
            user_id=user_id,
            current_stage=JobStage.QUEUED,
            total_accounts=len(session_items)
        )
        repo.create_job(job_rec)
        repo.save_session_items(session_items)

        audit.log_upload(user_id, upload_id, filename, val_res.total_files, val_res.supported_files, val_res.fingerprint)

        # 5. Format User Completion Message (PDF Section 6 Format)
        country_summary = ", ".join(f"<b>{k}</b>: {v}" for k, v in sorted(country_breakdown.items()))
        report_text = (
            f"<b>ZIP PROCESSING COMPLETED</b>\n"
            f"Upload ID: <code>#{upload_id}</code>\n"
            f"Files detected: {val_res.total_files}\n"
            f"Supported files: {val_res.supported_files}\n"
            f"Invalid or unsupported files: {val_res.invalid_files}\n"
            f"Status: Completed\n\n"
            f"🌍 <b>Accounts by Country:</b>\n{country_summary}\n\n"
            f"<i>Your archive has been validated and an inventory has been created. No authentication codes, passwords, 2FA credentials, or session files were shared.</i>"
        )

        # 6. Build Interactive Distribution Menu for Admin/Owner
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⚙️ Configure Distribution ({len(session_items)} Accounts)",
                    callback_data=f"dist:setup:{job_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚡ Fast Auto-Sell (All Operators & Highest Rates)",
                    callback_data=f"dist:fast_launch:{job_id}"
                )
            ]
        ])

        await status_msg.edit_text(report_text, reply_markup=kb, parse_mode="HTML")

    except Exception as e:
        await status_msg.edit_text(f"❌ Processing error: {str(e)[:100]}")
    finally:
        if os.path.exists(temp_zip_path):
            try:
                os.remove(temp_zip_path)
            except Exception:
                pass
