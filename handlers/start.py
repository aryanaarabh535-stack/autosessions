from aiogram import Router, types
from aiogram.filters import CommandStart, Command
from database.repository import DatabaseRepository
import config

router = Router()
repo = DatabaseRepository()


@router.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    if user:
        repo.get_or_create_user(user.id, user.username, user.first_name)

    text = (
        "👋 <b>Welcome to the Telegram ZIP Automation Bot!</b>\n\n"
        "This platform automates ZIP archive validation, session inventory, and "
        "high-yield distribution to authorized receivers with zero credential leakage.\n\n"
        "📜 <b>Terms & Privacy Notice:</b>\n"
        "• Uploads are validated in isolated temporary storage.\n"
        "• Passwords, 2FA credentials, or reusable session files are NEVER forwarded to third parties.\n"
        "• Temporary extracted files are securely deleted after processing.\n"
        "• You must only process accounts you own or are explicitly authorized to manage.\n\n"
        "📥 <b>Upload Instructions:</b>\n"
        "Send <code>/upload</code> or simply upload a <code>.zip</code> archive containing "
        "Telegram session files (<code>.session</code> / <code>.json</code>).\n\n"
        "💡 Use <code>/help</code> for format guidelines or <code>/stats</code> to view your statistics."
    )
    await message.reply(text, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: types.Message):
    text = (
        "ℹ️ <b>Supported Archive Formats & Guidelines</b>\n\n"
        "• <b>Archive Format:</b> <code>.zip</code> only (Max 50 MB compressed, 200 MB uncompressed)\n"
        "• <b>Session Types:</b> Telethon / Pyrogram SQLite (<code>.session</code>), metadata (<code>.json</code>)\n"
        "• <b>Security Protections:</b> Strict protection against path traversal, ZIP bombs, symlinks, and executables.\n"
        "• <b>Commands:</b>\n"
        "   /upload — Start ZIP upload\n"
        "   /history — View previous uploads & jobs\n"
        "   /stats — View personal processing statistics\n"
        "   /support — Contact system support"
    )
    await message.reply(text, parse_mode="HTML")


@router.message(Command("support"))
async def cmd_support(message: types.Message):
    text = (
        f"💬 <b>System Support & Assistance</b>\n\n"
        f"For questions regarding archive formats or trade execution, contact {config.SUPPORT_CONTACT}."
    )
    await message.reply(text, parse_mode="HTML")
