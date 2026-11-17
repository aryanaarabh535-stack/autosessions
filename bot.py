import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import config
from database.connection import init_database_indexes, close_database_connection
from database.repository import DatabaseRepository
from handlers import setup_routers
from services.audit_service import AuditService

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("ZipAutoSellerBot")

audit = AuditService()
repo = DatabaseRepository()


async def set_bot_commands(bot: Bot):
    commands = [
        types.BotCommand(command="start", description="🏠 Start bot & view status"),
        types.BotCommand(command="upload", description="📦 Upload session ZIP archive"),
        types.BotCommand(command="history", description="📜 View upload & sell history"),
        types.BotCommand(command="admin", description="👑 Admin Control Dashboard"),
        types.BotCommand(command="help", description="❓ Bot guide & specifications"),
        types.BotCommand(command="support", description="💬 Contact support & operators"),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as e:
        logger.warning(f"Could not register bot commands: {e}")


async def on_startup(bot: Bot):
    logger.info("Initializing database indexes...")
    try:
        init_database_indexes()
        logger.info("MongoDB indexes verified successfully.")
    except Exception as e:
        logger.error(f"Error initializing database indexes: {e}")

    await set_bot_commands(bot)

    audit.log_event(
        action="SYSTEM_STARTUP",
        user_id=0,
        status="SUCCESS",
        metadata={
            "app_version": "1.0.0",
            "max_archive_mb": config.MAX_ARCHIVE_SIZE_BYTES // (1024 * 1024),
            "max_uncompressed_mb": config.MAX_UNCOMPRESSED_SIZE_BYTES // (1024 * 1024),
            "adminlog_id": config.ADMINLOG_ID
        }
    )

    startup_msg = (
        "🚀 <b>Telegram ZIP Auto-Selling Engine Started</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "• <b>Status:</b> 🟢 Operational (Online)\n"
        f"• <b>Max ZIP Size:</b> {config.MAX_ARCHIVE_SIZE_BYTES // (1024 * 1024)} MB\n"
        f"• <b>Max Uncompressed:</b> {config.MAX_UNCOMPRESSED_SIZE_BYTES // (1024 * 1024)} MB\n"
        f"• <b>Max Files:</b> {config.MAX_FILE_COUNT} files/archive\n"
        "• <b>Security Spec:</b> PDF Acceptance Compliant\n"
        "• <b>Engine:</b> Safe Extraction • Zero Secret Exposure • Multi-Operator Distro\n"
        "• <b>Receivers:</b> 11 Verified Profiles Loaded\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👤 <b>Admins:</b> @bitz_owner @jameendar"
    )

    try:
        await bot.send_message(
            chat_id=config.ADMINLOG_ID,
            text=startup_msg,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.warning(f"Failed to post startup notice to ADMINLOG_ID: {e}")


async def on_shutdown(bot: Bot):
    logger.info("Shutting down Telegram ZIP Auto-Selling Engine...")
    shutdown_msg = (
        "🛑 <b>Telegram ZIP Auto-Selling Engine Stopped</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "System shutdown gracefully.\n"
        "👤 @bitz_owner @jameendar"
    )
    try:
        await bot.send_message(
            chat_id=config.ADMINLOG_ID,
            text=shutdown_msg,
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

    close_database_connection()
    await bot.session.close()
    logger.info("Bot session and database connections closed.")


async def main():
    if not config.BOT_TOKEN:
        logger.critical("BOT_TOKEN is missing! Set BOT_TOKEN in .env or config.py.")
        sys.exit(1)

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    dp = Dispatcher()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Register all feature routers
    dp.include_router(setup_routers())

    logger.info("Starting long-polling for ZIP Auto-Seller...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Execution terminated by user/signal.")
