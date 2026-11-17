import os
import tempfile
from dotenv import load_dotenv

load_dotenv()

def _getenv(name: str, default: str = "") -> str:
    return os.getenv(name, default)

# Telegram Bot Token
BOT_TOKEN = _getenv("BOT_TOKEN", "9035733464:AAFpeNFawtZmj3k-u5igPwatjt-tgyTC2ug")

# Admin IDs & Log Channels
ADMIN_IDS = [int(i.strip()) for i in _getenv("ADMIN_IDS", "233444460,250730849,36888805,5258077200").split(",") if i.strip().isdigit()]
ROOT_OWNER_IDS = [int(i.strip()) for i in _getenv("ROOT_OWNER_IDS", "233444460,250730849,36888805,5258077200").split(",") if i.strip().isdigit()]
ADMINLOG_ID = _getenv("ADMINLOG_ID", "-1004467341730")
SUPPORT_CONTACT = _getenv("SUPPORT_CONTACT", "@bitz_owner")

# MongoDB Atlas
DATABASE_URL = _getenv("DATABASE_URL", "mongodb+srv://haiu38716_db_user:zY8V62mUalSfAUma@tgbitz.n1enmes.mongodb.net/?appName=tgbitz")
MONGO_URI = _getenv("MONGO_URI", DATABASE_URL)
MONGO_DB_NAME = _getenv("MONGO_DB_NAME", "tgbitz_zip_autosell")

# Telegram Client API Credentials
API_ID = int(_getenv("API_ID", "21377358"))
API_HASH = _getenv("API_HASH", "e05bc1f4f03839db7864a99dbf72d1cd")
ENCRYPTION_KEY = _getenv("ENCRYPTION_KEY", "c4b5b044572298d5487f993d1b600f39574cb24e44afdf528924b1325c6e8626")

# ZIP & Processing Limits (Spec Requirement)
MAX_ARCHIVE_SIZE_BYTES = int(_getenv("MAX_ARCHIVE_SIZE_BYTES", str(50 * 1024 * 1024)))      # 50 MB
MAX_UNCOMPRESSED_SIZE_BYTES = int(_getenv("MAX_UNCOMPRESSED_SIZE_BYTES", str(200 * 1024 * 1024))) # 200 MB
MAX_FILE_COUNT = int(_getenv("MAX_FILE_COUNT", "500"))
MAX_COMPRESSION_RATIO = float(_getenv("MAX_COMPRESSION_RATIO", "100.0"))

# Retention & Temporary Storage
TEMP_STORAGE_DIR = _getenv("TEMP_STORAGE_DIR", os.path.join(tempfile.gettempdir(), "tg_zip_autoseller"))
RETENTION_HOURS = int(_getenv("RETENTION_HOURS", "24"))

# Optional 3rd-party non-sensitive metadata integration
PROVIDER_API_URL = _getenv("PROVIDER_API_URL", "")
PROVIDER_API_KEY = _getenv("PROVIDER_API_KEY", "")
