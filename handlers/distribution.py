from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database.repository import DatabaseRepository
from database.models import JobStage
from services.job_manager import JobManager
from services.audit_service import AuditService
from trading.operator_pool import OperatorPool
from trading.receiver_adapter import ReceiverAdapter
from trading.cleaner import AccountCleaner
from trading.otp_service import OtpService
from trading.verifier import AccountVerifier
from workers.selling_worker import SellingWorker
import config

router = Router()
repo = DatabaseRepository()
audit = AuditService(repo)
job_manager = JobManager(repo, audit)
op_pool = OperatorPool()
otp_svc = OtpService(config.API_ID, config.API_HASH)
verifier = AccountVerifier(config.API_ID, config.API_HASH)
rec_adapter = ReceiverAdapter(otp_svc, verifier)

# Global selling worker instance
selling_worker = SellingWorker(
    repo=repo,
    operator_pool=op_pool,
    receiver_adapter=rec_adapter
)


@router.callback_query(F.data.startswith("dist:setup:"))
async def cb_dist_setup(query: types.CallbackQuery):
    job_id = query.data.split(":")[2]
    job = repo.get_job(job_id)
    if not job:
        await query.answer("Job not found.", show_alert=True)
        return

    ops = op_pool.list_registered_operators()
    if not ops:
        await query.message.edit_text(
            f"⚠️ <b>No Active Operators Found</b>\n\n"
            f"Please register an operator first using <code>/addoperator &lt;id&gt; &lt;phone&gt;</code>.",
            parse_mode="HTML"
        )
        return

    op_lines = "\n".join(f"• <b>{o['operator_id']}</b> ({o['phone']})" for o in ops)
    kb_rows = []

    # Preset 1: Auto Even Split
    kb_rows.append([
        InlineKeyboardButton(
            text=f"⚖️ Auto Even Split ({len(ops)} Operators)",
            callback_data=f"dist:apply_ops:even:{job_id}"
        )
    ])

    # Preset 2: Single Operator Buttons
    for o in ops[:4]:
        kb_rows.append([
            InlineKeyboardButton(
                text=f"👤 All ({job.total_accounts}) to {o['operator_id']}",
                callback_data=f"dist:apply_ops:single_{o['operator_id']}:{job_id}"
            )
        ])

    # Custom Quota instructions
    kb_rows.append([
        InlineKeyboardButton(
            text="✏️ Set Custom Quotas (/quota)",
            callback_data=f"dist:quota_help:{job_id}"
        )
    ])

    text = (
        f"👥 <b>OPERATOR DISTRIBUTION — {job_id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• 📦 <b>Total Accounts to Sell:</b> <b>{job.total_accounts}</b>\n\n"
        f"<b>Available Operators:</b>\n{op_lines}\n\n"
        f"<i>Select how many accounts each operator should sell:</i>"
    )
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")


@router.callback_query(F.data.startswith("dist:quota_help:"))
async def cb_quota_help(query: types.CallbackQuery):
    job_id = query.data.split(":")[2]
    text = (
        f"✏️ <b>Custom Operator Quota Instructions</b>\n\n"
        f"To split accounts by specific numbers across your operators, send:\n"
        f"<code>/quota {job_id} OP-9879:8,OP-2:14</code>\n\n"
        f"Example: 1 operator sells 8 accounts, 2nd operator sells 14 accounts, etc."
    )
    await query.message.edit_text(text, parse_mode="HTML")


@router.message(Command("quota"))
async def cmd_set_custom_quota(message: types.Message):
    parts = (message.text or "").strip().split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(
            "Usage: <code>/quota &lt;job_id&gt; OP-1:8,OP-2:14</code>\n"
            "Example: <code>/quota JOB-12345678 OP-9879:8,OP-2:14</code>",
            parse_mode="HTML"
        )
        return

    job_id = parts[1].strip()
    quota_str = parts[2].strip()
    job = repo.get_job(job_id)
    if not job:
        await message.reply(f"❌ Job <code>{job_id}</code> not found.", parse_mode="HTML")
        return

    distribution = {}
    try:
        for pair in quota_str.split(","):
            if ":" in pair:
                op, count = pair.split(":")
                distribution[op.strip()] = int(count.strip())
    except Exception:
        await message.reply("❌ Invalid format. Use: <code>OP-1:8,OP-2:14</code>", parse_mode="HTML")
        return

    total_allocated = sum(distribution.values())
    await show_receiver_selection(message, job_id, distribution, message.from_user.id)


@router.callback_query(F.data.startswith("dist:apply_ops:"))
async def cb_apply_ops(query: types.CallbackQuery):
    _, _, op_mode, job_id = query.data.split(":")
    job = repo.get_job(job_id)
    if not job:
        await query.answer("Job not found.", show_alert=True)
        return

    ops = op_pool.list_registered_operators()
    distribution = {}

    if op_mode == "even":
        per_op = max(1, job.total_accounts // len(ops))
        rem = job.total_accounts % len(ops)
        for i, o in enumerate(ops):
            distribution[o["operator_id"]] = per_op + (1 if i < rem else 0)
    elif op_mode.startswith("single_"):
        chosen_op = op_mode.replace("single_", "")
        distribution[chosen_op] = job.total_accounts

    await show_receiver_selection_query(query, job_id, distribution)


async def show_receiver_selection_query(query: types.CallbackQuery, job_id: str, distribution: dict):
    job = repo.get_job(job_id)
    op_summary = ", ".join(f"<b>{k}</b>: {v}" for k, v in distribution.items())

    # Build receiver buttons
    kb_rows = [
        [
            InlineKeyboardButton(
                text="⚡ Auto Highest-Yield Receiver (Recommended)",
                callback_data=f"dist:set_rec:auto:{job_id}"
            )
        ]
    ]

    # Add prominent receiver buttons
    receivers = rec_adapter.receivers
    top_receivers = ["crazy_time", "star_bot", "tg_get2", "cm_receiver6", "br_receiver6", "newfast_receiver", "jia_receiver8", "tb_unlimited2"]
    for rid in top_receivers:
        r_prof = receivers.get(rid)
        if r_prof:
            kb_rows.append([
                InlineKeyboardButton(
                    text=f"🛒 {r_prof.name} (@{r_prof.bot_username})",
                    callback_data=f"dist:set_rec:{rid}:{job_id}"
                )
            ])

    text = (
        f"🎯 <b>SELECT TARGET RECEIVER — {job_id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• 👥 <b>Operator Allocation:</b> {op_summary}\n\n"
        f"Choose whether to automatically route each country to its highest paying receiver, "
        f"or lock sales to a specific receiver bot:"
    )
    # Save partial distribution state in job
    repo.update_job_stage(job_id, JobStage.DISTRIBUTING, data_updates={"operator_distribution": distribution})
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")


async def show_receiver_selection(message: types.Message, job_id: str, distribution: dict, actor_id: int):
    op_summary = ", ".join(f"<b>{k}</b>: {v}" for k, v in distribution.items())
    kb_rows = [
        [
            InlineKeyboardButton(
                text="⚡ Auto Highest-Yield Receiver (Recommended)",
                callback_data=f"dist:set_rec:auto:{job_id}"
            )
        ]
    ]
    for rid in ["crazy_time", "star_bot", "tg_get2", "cm_receiver6", "br_receiver6", "newfast_receiver"]:
        r_prof = rec_adapter.receivers.get(rid)
        if r_prof:
            kb_rows.append([
                InlineKeyboardButton(
                    text=f"🛒 {r_prof.name} (@{r_prof.bot_username})",
                    callback_data=f"dist:set_rec:{rid}:{job_id}"
                )
            ])

    text = (
        f"🎯 <b>SELECT TARGET RECEIVER — {job_id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• 👥 <b>Operator Allocation:</b> {op_summary}\n\n"
        f"Choose whether to automatically route each country to its highest paying receiver, "
        f"or lock sales to a specific receiver bot:"
    )
    repo.update_job_stage(job_id, JobStage.DISTRIBUTING, data_updates={"operator_distribution": distribution})
    await message.reply(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")


@router.callback_query(F.data.startswith("dist:set_rec:"))
async def cb_set_receiver(query: types.CallbackQuery):
    _, _, chosen_rec, job_id = query.data.split(":")
    job = repo.get_job(job_id)
    if not job:
        await query.answer("Job not found.", show_alert=True)
        return

    rec_label = "Auto Highest-Yield" if chosen_rec == "auto" else f"@{rec_adapter.receivers.get(chosen_rec, chosen_rec).bot_username}"
    op_summary = ", ".join(f"<b>{k}</b>: {v}" for k, v in job.operator_distribution.items())

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🚀 START SELLING BATCH NOW",
                callback_data=f"dist:launch:{chosen_rec}:{job_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="❌ Cancel",
                callback_data=f"dist:cancel:{job_id}"
            )
        ]
    ])

    text = (
        f"🚀 <b>READY TO LAUNCH AUTO-SELL BATCH</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• 🆔 <b>Job ID:</b> <code>{job_id}</code>\n"
        f"• 📦 <b>Accounts to Sell:</b> <b>{job.total_accounts}</b>\n"
        f"• 👥 <b>Operator Distribution:</b> {op_summary}\n"
        f"• 🎯 <b>Target Receiver:</b> <b>{rec_label}</b>\n\n"
        f"<i>All sessions will be cleaned, 2FA removed, verified with 2-devices, and local session terminated cleanly.</i>"
    )
    await query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("dist:fast_launch:"))
async def cb_fast_launch(query: types.CallbackQuery):
    job_id = query.data.split(":")[2]
    job = repo.get_job(job_id)
    if not job:
        await query.answer("Job not found.", show_alert=True)
        return

    ops = op_pool.list_registered_operators()
    distribution = {}
    per_op = max(1, job.total_accounts // len(ops))
    rem = job.total_accounts % len(ops)
    for i, o in enumerate(ops):
        distribution[o["operator_id"]] = per_op + (1 if i < rem else 0)

    job_manager.configure_distribution(
        job_id, query.from_user.id,
        operator_distribution=distribution,
        selected_receiver="auto"
    )
    selling_worker.bot = query.bot
    await selling_worker.launch_selling_batch(job_id, query.from_user.id)

    await query.message.edit_text(
        f"🚀 <b>AUTO-SELL BATCH LAUNCHED!</b>\n\n"
        f"• 🆔 <b>Job:</b> <code>{job_id}</code>\n"
        f"• 📦 <b>Accounts:</b> {job.total_accounts}\n"
        f"• 👥 <b>Operators:</b> {len(distribution)} active\n"
        f"• 🎯 <b>Receiver:</b> Auto Highest-Yield\n\n"
        f"<i>Live progress is streaming to Admin Log channel (-1004467341730).</i>",
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("dist:launch:"))
async def cb_launch_batch(query: types.CallbackQuery):
    _, _, chosen_rec, job_id = query.data.split(":")
    job = repo.get_job(job_id)
    if not job:
        await query.answer("Job not found.", show_alert=True)
        return

    job_manager.configure_distribution(
        job_id, query.from_user.id,
        operator_distribution=job.operator_distribution,
        selected_receiver=chosen_rec
    )
    selling_worker.bot = query.bot
    await selling_worker.launch_selling_batch(job_id, query.from_user.id)

    rec_label = "Auto Highest-Yield" if chosen_rec == "auto" else f"@{rec_adapter.receivers.get(chosen_rec, chosen_rec).bot_username}"
    await query.message.edit_text(
        f"🚀 <b>AUTO-SELL BATCH LAUNCHED!</b>\n\n"
        f"• 🆔 <b>Job:</b> <code>{job_id}</code>\n"
        f"• 📦 <b>Accounts:</b> {job.total_accounts}\n"
        f"• 🎯 <b>Receiver:</b> {rec_label}\n\n"
        f"<i>The bot is processing accounts sequentially. Live progress is being posted to the Admin Log channel.</i>",
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("dist:cancel:"))
async def cb_cancel_dist(query: types.CallbackQuery):
    job_id = query.data.split(":")[2]
    repo.update_job_stage(job_id, JobStage.REJECTED, safe_error="Cancelled by user")
    await query.message.edit_text(f"🛑 Job <code>{job_id}</code> distribution cancelled.", parse_mode="HTML")
