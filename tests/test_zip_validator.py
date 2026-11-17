import os
import sys
import unittest
import tempfile
import zipfile
import shutil

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.zip_validator import ZipValidator


class TestZipValidator(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_zip_validator_")
        self.validator = ZipValidator(
            max_archive_size=10 * 1024 * 1024,
            max_uncompressed_size=20 * 1024 * 1024,
            max_files=10,
            temp_dir=os.path.join(self.test_dir, "temp_extracted")
        )

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_valid_archive(self):
        zip_path = os.path.join(self.test_dir, "valid.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("session1.session", b"test_session_binary_data")
            zf.writestr("session1.json", b'{"phone": "+12025550199"}')

        result = self.validator.validate_and_extract(zip_path, "UPLOAD_001")
        self.assertTrue(result.is_valid)
        self.assertEqual(result.total_files, 2)
        self.assertEqual(result.supported_files, 2)
        self.assertEqual(result.invalid_files, 0)
        self.assertTrue(len(result.fingerprint) == 64)
        self.assertTrue(os.path.exists(result.extracted_dir))

    def test_path_traversal_rejection(self):
        zip_path = os.path.join(self.test_dir, "traversal.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../evil.txt", b"malicious content")

        result = self.validator.validate_and_extract(zip_path, "UPLOAD_002")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.safe_error_code, "PATH_TRAVERSAL_DETECTED")

    def test_file_count_limit(self):
        zip_path = os.path.join(self.test_dir, "too_many_files.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            for i in range(15):  # limit is 10
                zf.writestr(f"file_{i}.txt", b"hello")

        result = self.validator.validate_and_extract(zip_path, "UPLOAD_003")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.safe_error_code, "EXCESSIVE_FILE_COUNT")

    def test_dangerous_extension_rejection(self):
        zip_path = os.path.join(self.test_dir, "dangerous.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("malware.exe", b"MZ\x90\x00...")

        result = self.validator.validate_and_extract(zip_path, "UPLOAD_004")
        self.assertFalse(result.is_valid)
        self.assertEqual(result.safe_error_code, "EXECUTABLE_PROHIBITED")


if __name__ == "__main__":
    unittest.main()
