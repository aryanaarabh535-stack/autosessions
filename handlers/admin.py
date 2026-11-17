import time
from aiogram import Router, types
from aiogram.filters import Command
from database.repository import DatabaseRepository
from database.models import UserStatus, OperatorRecord
from trading.operator_pool import OperatorPool
import config

router = Router()
repo = DatabaseRepository()
op_pool = OperatorPool()


def is_admin(user_id: int) -> bool:
    return user_id in getattr(config, "ADMIN_IDS", [])


@router.message(Command("admin"))
async def cmd_admin_dashboard(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    metrics = repo.get_system_metrics()
    ops = op_pool.list_registered_operators()

    text = (
        "👑 <b>ADMINISTRATOR CONTROL DASHBOARD</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• 👥 <b>Registered Users:</b> {metrics['total_users']}\n"
        f"• 📦 <b>Total Archives Uploaded:</b> {metrics['total_uploads']}\n"
        f"• ✅ <b>Valid Archives:</b> {metrics['valid_archives']}\n"
        f"• 🚫 <b>Rejected Archives:</b> {metrics['rejected_archives']}\n"
        f"• ⚙️ <b>Total Jobs:</b> {metrics['total_jobs']}\n"
        f"• 🎉 <b>Completed Jobs:</b> {metrics['completed_jobs']}\n"
        f"• ❌ <b>Failed Jobs:</b> {metrics['failed_jobs']}\n"
        f"• 👤 <b>Active Operators:</b> {len(ops)}\n\n"
        "<b>Available Admin Commands:</b>\n"
        "• <code>/users</code> — List registered users\n"
        "• <code>/jobs</code> — Inspect processing queue\n"
        "• <code>/logs</code> — View non-sensitive audit logs\n"
        "• <code>/operators</code> — Manage operator pool\n"
        "• <code>/addoperator &lt;id&gt; &lt;phone&gt;</code> — Add operator\n"
        "• <code>/ban &lt;user_id&gt;</code> — Ban user\n"
        "• <code>/unban &lt;user_id&gt;</code> — Unban user\n"
        "• <code>/broadcast &lt;message&gt;</code> — Send announcement"
    )
    await message.reply(text, parse_mode="HTML")


@router.message(Command("users"))
async def cmd_list_users(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    users = repo.list_users(limit=25)
    lines = []
    for u in users:
        uname = f"@{u.username}" if u.username else "NoUsername"
        lines.append(f"• <code>{u.telegram_user_id}</code> | {uname} | <b>{u.status.value.upper()}</b>")

    text = "👥 <b>REGISTERED USERS</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines)
    await message.reply(text, parse_mode="HTML")


@router.message(Command("jobs"))
async def cmd_list_jobs(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    jobs = repo.list_jobs(limit=15)
    if not jobs:
        await message.reply("No jobs currently recorded.")
        return

    lines = []
    for j in jobs:
        lines.append(
            f"• <code>{j.job_id}</code> (Upload: #{j.upload_id})\n"
            f"   Stage: <b>{j.current_stage.value.upper()}</b> | Accounts: {j.total_accounts} (Sold: {j.sold_accounts}, Failed: {j.failed_accounts})\n"
            f"   Payout: ${j.total_payout_usd:.2f} USD"
        )
    text = "⚙️ <b>RECENT PROCESSING JOBS</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n\n".join(lines)
    await message.reply(text, parse_mode="HTML")


@router.message(Command("logs"))
async def cmd_list_logs(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    logs = repo.list_audit_logs(limit=15)
    lines = []
    for l in logs:
        t_str = time.strftime("%H:%M:%S", time.localtime(l.timestamp))
        lines.append(f"• <code>{t_str}</code> | <b>{l.action}</b> (User: <code>{l.actor_id}</code>)")

    text = "📋 <b>AUDIT LOGS (Non-Sensitive)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines)
    await message.reply(text, parse_mode="HTML")


@router.message(Command("operators"))
async def cmd_list_operators(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    ops = op_pool.list_registered_operators()
    lines = [f"• <b>{o['operator_id']}</b>: <code>{o['phone']}</code> (Source: {o['source']})" for o in ops]
    text = "👥 <b>ACTIVE OPERATORS</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + ("\n".join(lines) if lines else "No operators found.")
    await message.reply(text, parse_mode="HTML")


@router.message(Command("addoperator"))
async def cmd_add_operator(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").strip().split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Usage: <code>/addoperator &lt;operator_id&gt; &lt;phone&gt;</code>", parse_mode="HTML")
        return

    op_id = parts[1].strip()
    phone = parts[2].strip()

    op_rec = OperatorRecord(
        operator_id=op_id,
        phone=phone,
        is_active=True
    )
    repo.upsert_operator(op_rec)
    await message.reply(f"✅ Operator <code>{op_id}</code> ({phone}) registered successfully.", parse_mode="HTML")


@router.message(Command("ban"))
async def cmd_ban_user(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Usage: <code>/ban &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    uid = int(parts[1])
    repo.set_user_status(uid, UserStatus.BANNED)
    await message.reply(f"⛔ User <code>{uid}</code> has been banned.", parse_mode="HTML")


@router.message(Command("unban"))
async def cmd_unban_user(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Usage: <code>/unban &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    uid = int(parts[1])
    repo.set_user_status(uid, UserStatus.ACTIVE)
    await message.reply(f"✅ User <code>{uid}</code> has been unbanned.", parse_mode="HTML")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    text_to_send = (message.text or "").replace("/broadcast", "", 1).strip()
    if not text_to_send:
        await message.reply("Usage: <code>/broadcast &lt;announcement text&gt;</code>", parse_mode="HTML")
        return

    users = repo.list_users(limit=500)
    success = 0
    bot = message.bot
    for u in users:
        try:
            await bot.send_message(u.telegram_user_id, f"📢 <b>Announcement:</b>\n\n{text_to_send}", parse_mode="HTML")
            success += 1
        except Exception:
            pass
    await message.reply(f"📢 Broadcast delivered to {success} users.", parse_mode="HTML")
