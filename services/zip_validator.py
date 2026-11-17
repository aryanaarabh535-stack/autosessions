import os
import zipfile
import hashlib
import shutil
from typing import Tuple, List, Dict, Optional, Any
from dataclasses import dataclass, field
import config

DANGEROUS_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".sh", ".elf", ".dll", ".so", ".vbs",
    ".js", ".scr", ".com", ".ps1", ".py", ".bin", ".jar", ".app", ".msi"
}

SUPPORTED_EXTENSIONS = {".session", ".json"}
SUPPORTED_TDATA_FILES = {"key_datas", "settingss", "usertag"}


@dataclass
class ZipValidationResult:
    is_valid: bool
    safe_error_code: Optional[str] = None
    safe_error_message: Optional[str] = None
    fingerprint: str = ""
    total_files: int = 0
    supported_files: int = 0
    invalid_files: int = 0
    extracted_dir: Optional[str] = None
    file_inventory: List[Dict[str, Any]] = field(default_factory=list)


class ZipValidator:
    def __init__(
        self,
        max_archive_size: int = config.MAX_ARCHIVE_SIZE_BYTES,
        max_uncompressed_size: int = config.MAX_UNCOMPRESSED_SIZE_BYTES,
        max_files: int = config.MAX_FILE_COUNT,
        max_ratio: float = config.MAX_COMPRESSION_RATIO,
        temp_dir: str = config.TEMP_STORAGE_DIR
    ):
        self.max_archive_size = max_archive_size
        self.max_uncompressed_size = max_uncompressed_size
        self.max_files = max_files
        self.max_ratio = max_ratio
        self.temp_dir = temp_dir
        os.makedirs(self.temp_dir, exist_ok=True)

    def compute_fingerprint(self, file_path: str) -> str:
        """Computes SHA-256 fingerprint of the archive file."""
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    def validate_and_extract(self, zip_path: str, upload_id: str) -> ZipValidationResult:
        """
        Executes strict end-to-end archive verification, path traversal checks,
        ZIP bomb prevention, and isolated extraction.
        """
        # 1. File existence & basic size check
        if not os.path.exists(zip_path):
            return ZipValidationResult(False, "FILE_NOT_FOUND", "Uploaded archive file not found.")

        file_size = os.path.getsize(zip_path)
        if file_size > self.max_archive_size:
            return ZipValidationResult(
                False, "ARCHIVE_TOO_LARGE",
                f"Archive exceeds maximum compressed limit of {self.max_archive_size // (1024*1024)} MB."
            )
        if file_size == 0:
            return ZipValidationResult(False, "ARCHIVE_EMPTY", "Uploaded archive file is empty.")

        # Compute archive fingerprint
        fingerprint = self.compute_fingerprint(zip_path)

        # 2. Integrity & ZIP format check
        if not zipfile.is_zipfile(zip_path):
            return ZipValidationResult(False, "INVALID_FORMAT", "File is not a valid ZIP archive.")

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                # Test zip integrity
                corrupt_file = zf.testzip()
                if corrupt_file:
                    return ZipValidationResult(
                        False, "CORRUPT_ARCHIVE",
                        f"Archive is corrupted or failed CRC integrity check."
                    )

                infolist = zf.infolist()
                total_files = len(infolist)

                if total_files > self.max_files:
                    return ZipValidationResult(
                        False, "EXCESSIVE_FILE_COUNT",
                        f"Archive contains {total_files} files, exceeding limit of {self.max_files}."
                    )

                total_uncompressed = 0
                supported_count = 0
                invalid_count = 0
                inventory = []

                for info in infolist:
                    filename = info.filename
                    
                    # Normalize path and check for traversal
                    norm_path = os.path.normpath(filename)
                    if (
                        ".." in norm_path.split(os.path.sep)
                        or os.path.isabs(norm_path)
                        or norm_path.startswith("/")
                        or norm_path.startswith("\\")
                        or ":" in norm_path
                    ):
                        return ZipValidationResult(
                            False, "PATH_TRAVERSAL_DETECTED",
                            "Security rejection: archive contains illegal relative or absolute path traversal."
                        )

                    # Symlink detection (UNIX symlink attribute)
                    is_symlink = (info.external_attr >> 16) & 0o170000 == 0o120000
                    if is_symlink:
                        return ZipValidationResult(
                            False, "SYMLINK_PROHIBITED",
                            "Security rejection: archive contains prohibited symbolic links."
                        )

                    # Directory entries
                    if info.is_dir():
                        continue

                    total_uncompressed += info.file_size
                    if total_uncompressed > self.max_uncompressed_size:
                        return ZipValidationResult(
                            False, "UNCOMPRESSED_LIMIT_EXCEEDED",
                            f"Extracted content exceeds maximum uncompressed limit of {self.max_uncompressed_size // (1024*1024)} MB."
                        )

                    # ZIP bomb check per entry
                    comp_size = max(info.compress_size, 1)
                    ratio = info.file_size / comp_size
                    if ratio > self.max_ratio and info.file_size > 1024 * 1024:
                        return ZipValidationResult(
                            False, "ZIP_BOMB_DETECTED",
                            "Security rejection: potential ZIP bomb detected (excessive compression ratio)."
                        )

                    # Executable / malicious file check
                    _, ext = os.path.splitext(filename.lower())
                    if ext in DANGEROUS_EXTENSIONS:
                        return ZipValidationResult(
                            False, "EXECUTABLE_PROHIBITED",
                            f"Security rejection: archive contains prohibited executable content ({ext})."
                        )

                    # Inventory classification
                    base_name = os.path.basename(filename).lower()
                    is_supported = ext in SUPPORTED_EXTENSIONS or base_name in SUPPORTED_TDATA_FILES or "tdata" in filename.lower()
                    if is_supported:
                        supported_count += 1
                        inventory.append({
                            "rel_path": norm_path,
                            "filename": os.path.basename(filename),
                            "ext": ext,
                            "size_bytes": info.file_size,
                            "is_supported": True
                        })
                    else:
                        invalid_count += 1
                        inventory.append({
                            "rel_path": norm_path,
                            "filename": os.path.basename(filename),
                            "ext": ext,
                            "size_bytes": info.file_size,
                            "is_supported": False
                        })

                if supported_count == 0:
                    return ZipValidationResult(
                        False, "NO_SUPPORTED_FILES",
                        "Archive does not contain any supported Telegram session files (.session or .json)."
                    )

                # 3. Safe Extraction into isolated temp directory
                extract_target = os.path.join(self.temp_dir, f"upload_{upload_id}")
                if os.path.exists(extract_target):
                    shutil.rmtree(extract_target, ignore_errors=True)
                os.makedirs(extract_target, exist_ok=True)

                for info in infolist:
                    if info.is_dir():
                        continue
                    # Safe extraction avoiding path traversal
                    target_file = os.path.join(extract_target, os.path.normpath(info.filename))
                    target_dir = os.path.dirname(target_file)
                    os.makedirs(target_dir, exist_ok=True)
                    with zf.open(info) as src, open(target_file, "wb") as dst:
                        shutil.copyfileobj(src, dst)

                return ZipValidationResult(
                    is_valid=True,
                    safe_error_code=None,
                    safe_error_message=None,
                    fingerprint=fingerprint,
                    total_files=total_files,
                    supported_files=supported_count,
                    invalid_files=invalid_count,
                    extracted_dir=extract_target,
                    file_inventory=inventory
                )

        except zipfile.BadZipFile:
            return ZipValidationResult(False, "MALFORMED_ZIP", "Archive is malformed or damaged.")
        except Exception as e:
            return ZipValidationResult(False, "VALIDATION_ERROR", f"Validation failed: {str(e)[:100]}")

    def cleanup_extracted(self, extracted_dir: Optional[str]):
        """Safely cleans up isolated extracted temporary storage according to retention policy."""
        if extracted_dir and os.path.exists(extracted_dir):
            try:
                shutil.rmtree(extracted_dir, ignore_errors=True)
            except Exception:
                pass
