import time
from aiogram import Router, types
from aiogram.filters import Command
from database.repository import DatabaseRepository

router = Router()
repo = DatabaseRepository()


@router.message(Command("history"))
async def cmd_history(message: types.Message):
    user_id = message.from_user.id if message.from_user else 0
    uploads = repo.list_user_uploads(user_id, limit=10)
    if not uploads:
        await message.reply("📂 <b>No upload history found.</b>\nUse <code>/upload</code> to submit your first ZIP archive.", parse_mode="HTML")
        return

    lines = []
    for u in uploads:
        date_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(u.upload_time))
        lines.append(
            f"• <code>#{u.upload_id}</code> | {date_str}\n"
            f"   📁 {u.filename} ({u.supported_files} sessions, {u.invalid_files} invalid)\n"
            f"   ⚡ Status: <b>{u.processing_status.upper()}</b>"
        )

    text = (
        "📜 <b>YOUR RECENT UPLOAD HISTORY</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + "\n\n".join(lines)
    )
    await message.reply(text, parse_mode="HTML")


@router.message(Command("stats"))
async def cmd_stats(message: types.Message):
    user_id = message.from_user.id if message.from_user else 0
    stats = repo.get_user_stats(user_id)

    text = (
        "📊 <b>YOUR PROCESSING STATISTICS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• 📦 <b>Total Archives Uploaded:</b> {stats['total_uploads']}\n"
        f"• ⚙️ <b>Total Jobs Created:</b> {stats['total_jobs']}\n"
        f"• 📱 <b>Total Accounts Processed:</b> {stats['total_accounts']}\n"
        f"• ✅ <b>Successfully Sold:</b> <b>{stats['sold_accounts']}</b>\n"
        f"• ❌ <b>Failed / Held:</b> {stats['failed_accounts']}\n"
        f"• 💰 <b>Total Earnings:</b> <b>${stats['total_payout_usd']:.2f} USD</b>"
    )
    await message.reply(text, parse_mode="HTML")
