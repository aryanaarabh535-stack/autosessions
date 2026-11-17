import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.session_loader import mask_phone, detect_country_iso
from encryption import encrypt_string, decrypt_string
from database.models import UserRecord, UploadRecord, JobRecord, SessionItem, JobStage, UserStatus


class TestModelsAndEncryption(unittest.TestCase):
    def test_phone_masking(self):
        self.assertEqual(mask_phone("+12025550199"), "+1202****199")
        self.assertEqual(mask_phone("+96551234936"), "+9655****936")
        self.assertEqual(mask_phone("123"), "123")
        self.assertEqual(mask_phone(None), "Unknown")

    def test_country_detection(self):
        self.assertEqual(detect_country_iso("+12025550199"), "US")
        self.assertEqual(detect_country_iso("+96551234936"), "KW")
        self.assertEqual(detect_country_iso("+541158507940"), "AR")
        self.assertEqual(detect_country_iso("+601123184223"), "MY")
        self.assertEqual(detect_country_iso("+999999999999"), "XX")

    def test_aes256_gcm_roundtrip(self):
        secret = "SuperSecret_2FA_Password_9981!"
        encrypted = encrypt_string(secret)
        self.assertIsNotNone(encrypted)
        self.assertTrue(encrypted.startswith("enc:v1:"))
        self.assertNotEqual(secret, encrypted)

        decrypted = decrypt_string(encrypted)
        self.assertEqual(secret, decrypted)

    def test_pydantic_models(self):
        user = UserRecord(
            telegram_user_id=12345,
            username="testuser",
            first_name="Test",
            status=UserStatus.ACTIVE,
            registration_date=1700000000.0
        )
        self.assertEqual(user.telegram_user_id, 12345)
        self.assertEqual(user.status, UserStatus.ACTIVE)

        job = JobRecord(
            job_id="JOB-TEST01",
            upload_id="UPL-TEST01",
            user_id=12345,
            current_stage=JobStage.QUEUED,
            created_at=1700000000.0,
            total_accounts=5
        )
        self.assertEqual(job.job_id, "JOB-TEST01")
        self.assertEqual(job.current_stage, JobStage.QUEUED)
        self.assertEqual(job.total_accounts, 5)

        session = SessionItem(
            session_id="SESS-001",
            job_id="JOB-TEST01",
            upload_id="UPL-TEST01",
            user_id=12345,
            phone="+12025550199",
            country_iso="US",
            phone_masked="+1202****199"
        )
        self.assertEqual(session.country_iso, "US")
        self.assertEqual(session.status, "pending")


if __name__ == "__main__":
    unittest.main()
