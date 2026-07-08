"""Google Drive Integration for Report Uploads.

This module provides utilities to upload files to Google Drive
and generate shareable links.

Setup:
1. Create a Google Cloud project
2. Enable Google Drive API
3. Create service account credentials
4. Download JSON key file to: data/google_service_account.json
5. Share target Drive folder with service account email

Usage:
    from common.google_drive_uploader import upload_to_drive

    file_url = upload_to_drive(
        file_path="reports/monthly_report.xlsx",
        folder_id="your_folder_id",  # Optional
    )
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Google Drive API dependencies
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    GDRIVE_AVAILABLE = True
except ImportError:
    GDRIVE_AVAILABLE = False
    logger.warning(
        "Google Drive libraries not installed. Run: pip install google-api-python-client google-auth"
    )


# Service account credentials path
ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS_PATH = ROOT / "data" / "google_service_account.json"

# Scopes required
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def get_drive_service():
    """Get authenticated Google Drive service."""
    if not GDRIVE_AVAILABLE:
        raise ImportError("Google Drive libraries not installed")

    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Service account credentials not found at: {CREDENTIALS_PATH}\n"
            f"Please download from Google Cloud Console and save to this location."
        )

    credentials = service_account.Credentials.from_service_account_file(
        str(CREDENTIALS_PATH), scopes=SCOPES
    )

    service = build("drive", "v3", credentials=credentials)
    return service


def upload_to_drive(
    file_path: str | Path,
    folder_id: str | None = None,
    make_public: bool = True,
) -> str:
    """Upload file to Google Drive and return shareable link.

    Args:
        file_path: Path to file to upload
        folder_id: Google Drive folder ID (optional)
        make_public: If True, make file publicly accessible

    Returns:
        Shareable URL to the uploaded file

    Raises:
        ImportError: If Google Drive libraries not installed
        FileNotFoundError: If credentials or file not found
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    try:
        service = get_drive_service()

        # File metadata
        file_metadata = {
            "name": file_path.name,
        }

        if folder_id:
            file_metadata["parents"] = [folder_id]

        # Determine MIME type
        if file_path.suffix == ".xlsx":
            mime_type = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        elif file_path.suffix == ".csv":
            mime_type = "text/csv"
        else:
            mime_type = "application/octet-stream"

        # Upload file
        media = MediaFileUpload(str(file_path), mimetype=mime_type, resumable=True)

        file = (
            service.files()
            .create(body=file_metadata, media_body=media, fields="id, webViewLink")
            .execute()
        )

        file_id = file.get("id")

        # Make publicly accessible
        if make_public:
            try:
                service.permissions().create(
                    fileId=file_id,
                    body={
                        "type": "anyone",
                        "role": "reader",
                    },
                ).execute()
                logger.info(f"File made public: {file_id}")
            except Exception as e:
                logger.warning(f"Failed to make file public: {e}")

        web_view_link = file.get("webViewLink", "")

        logger.info(f"Uploaded {file_path.name} to Google Drive: {web_view_link}")

        return web_view_link

    except Exception as e:
        logger.error(f"Failed to upload to Google Drive: {e}")
        raise


def upload_multiple_files(
    file_paths: list[str | Path],
    folder_id: str | None = None,
) -> dict[str, str]:
    """Upload multiple files to Google Drive.

    Args:
        file_paths: List of file paths to upload
        folder_id: Google Drive folder ID (optional)

    Returns:
        Dict mapping filename to URL
    """
    results = {}

    for file_path in file_paths:
        try:
            url = upload_to_drive(file_path, folder_id)
            results[Path(file_path).name] = url
        except Exception as e:
            logger.error(f"Failed to upload {file_path}: {e}")
            results[Path(file_path).name] = f"Error: {str(e)}"

    return results


if __name__ == "__main__":
    # Test upload
    logging.basicConfig(level=logging.INFO)

    test_file = ROOT / "reports" / "monthly_report_202512.xlsx"

    if test_file.exists():
        try:
            url = upload_to_drive(test_file)
            print("✅ Upload successful!")
            print(f"URL: {url}")
        except Exception as e:
            print(f"❌ Upload failed: {e}")
    else:
        print(f"Test file not found: {test_file}")
