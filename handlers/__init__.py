from aiogram import Router
from .start import router as start_router
from .upload import router as upload_router
from .distribution import router as distribution_router
from .history import router as history_router
from .admin import router as admin_router


def setup_routers() -> Router:
    main_router = Router()
    main_router.include_router(start_router)
    main_router.include_router(upload_router)
    main_router.include_router(distribution_router)
    main_router.include_router(history_router)
    main_router.include_router(admin_router)
    return main_router
