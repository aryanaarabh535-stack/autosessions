import aiohttp
import logging
from typing import Optional, Dict, Any
import config

logger = logging.getLogger("tgbitz.autosell.provider")


class ProviderApiIntegration:
    """
    Coordinates non-sensitive job metadata with authorized 3rd-party integration.
    Strictly prohibits sending login codes, passwords, 2FA credentials, or session data.
    """
    def __init__(self, api_url: str = config.PROVIDER_API_URL, api_key: str = config.PROVIDER_API_KEY):
        self.api_url = api_url
        self.api_key = api_key

    async def report_job_metadata(self, job_id: str, upload_id: str, file_count: int, status: str) -> Optional[str]:
        if not self.api_url:
            return None

        payload = {
            "job_id": job_id,
            "upload_id": upload_id,
            "file_count": file_count,
            "status": status,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.api_url}/jobs/notify", json=payload, headers=headers, timeout=10) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        return data.get("reference_id")
                    else:
                        logger.warning(f"Provider API returned HTTP {resp.status}")
                        return None
        except Exception as e:
            logger.debug(f"Provider API note: {e}")
            return None
