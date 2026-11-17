import logging
import certifi
from pymongo import MongoClient, ASCENDING
import config

logger = logging.getLogger("tgbitz.autosell.database")

_sync_client = None
_sync_db = None

def get_sync_database():
    """Returns synchronous PyMongo Database instance."""
    global _sync_client, _sync_db
    if _sync_db is not None:
        return _sync_db
    
    db_name = getattr(config, "MONGO_DB_NAME", "tgbitz_zip_autosell")
    try:
        _sync_client = MongoClient(
            config.MONGO_URI,
            tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=5000,
            maxPoolSize=20
        )
        _sync_db = _sync_client[db_name]
        logger.info(f"✓ Connected to MongoDB Atlas sync client (db='{db_name}')")
    except Exception as e:
        logger.warning(f"MongoDB TLS connect warning, falling back to standard: {e}")
        _sync_client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=5000)
        _sync_db = _sync_client[db_name]
        logger.info(f"✓ Connected to MongoDB Atlas (standard fallback)")
    return _sync_db

def get_database():
    """Alias for repository compatibility."""
    return get_sync_database()

def init_database_indexes():
    """Ensures indexes according to PDF Specification Section 5."""
    try:
        db = get_sync_database()
        # users
        db["users"].create_index([("telegram_user_id", ASCENDING)], unique=True)
        # uploads
        db["uploads"].create_index([("upload_id", ASCENDING)], unique=True)
        db["uploads"].create_index([("archive_fingerprint", ASCENDING)])
        db["uploads"].create_index([("user_id", ASCENDING)])
        # jobs
        db["jobs"].create_index([("job_id", ASCENDING)], unique=True)
        db["jobs"].create_index([("upload_id", ASCENDING)])
        db["jobs"].create_index([("current_stage", ASCENDING)])
        # audit_logs
        db["audit_logs"].create_index([("event_id", ASCENDING)], unique=True)
        db["audit_logs"].create_index([("actor_id", ASCENDING)])
        db["audit_logs"].create_index([("timestamp", ASCENDING)])
        # operators
        db["operators"].create_index([("operator_id", ASCENDING)], unique=True)
        logger.info("✓ MongoDB collections and indexes initialized successfully.")
    except Exception as e:
        logger.warning(f"Index initialization note: {e}")
