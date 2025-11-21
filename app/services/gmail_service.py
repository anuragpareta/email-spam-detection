# app/services/gmail_service.py

import base64
from datetime import datetime
from typing import List, Dict

from googleapiclient.discovery import build
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials


class GmailService:
    """
    A clean Gmail client that:
    - Uses credentials passed from main.py
    - Does NOT handle OAuth or token refresh
    - Only performs Gmail API operations
    """

    def __init__(self, credentials: Credentials):
        self.credentials = credentials
        self.service = build("gmail", "v1", credentials=credentials)

    def _extract_text_from_html(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text(separator=" ", strip=True)

    def _decode_email_body(self, payload: dict) -> str:
        """Extract plain text or HTML body from payload."""
        body_text = ""

        parts = payload.get("parts", []) or []

        for part in parts:
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data")

            if not data:
                continue

            try:
                decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            except:
                continue

            if mime == "text/plain":
                return decoded

            if mime == "text/html":
                return self._extract_text_from_html(decoded)

        # Fallback if body is directly in payload
        body_raw = payload.get("body", {}).get("data")
        if body_raw:
            try:
                decoded = base64.urlsafe_b64decode(body_raw).decode("utf-8", errors="ignore")
                return self._extract_text_from_html(decoded)
            except:
                pass

        return body_text

    # --------------------------------------------
    # MAIN FUNCTION - fetch emails by date range
    # --------------------------------------------
    def fetch_emails_by_date_range(
        self,
        start_date: str,   # YYYY-MM-DD
        end_date: str      # YYYY-MM-DD
    ) -> List[Dict]:

        start_str = datetime.strptime(start_date, "%d-%m-%Y").strftime("%Y/%m/%d")
        end_str   = datetime.strptime(end_date, "%d-%m-%Y").strftime("%Y/%m/%d")
        query = f"after:{start_str} before:{end_str}"

        response = self.service.users().messages().list(
            userId="me", q=query, maxResults=500
        ).execute()

        message_list = response.get("messages", [])
        emails = []

        for m in message_list:
            msg = self.service.users().messages().get(
                userId="me", id=m["id"], format="full"
            ).execute()

            payload = msg.get("payload", {})
            headers = payload.get("headers", [])

            subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")
            sender = next((h["value"] for h in headers if h["name"] == "From"), "")

            body = self._decode_email_body(payload)

            emails.append({"id": m["id"], "sender": sender, "subject": subject, "body": body})

        return emails

    # --------------------------------------------
    # Move emails to trash
    # --------------------------------------------
    def move_emails_to_trash(self, message_ids: List[str]) -> int:
        count = 0
        for msg_id in message_ids:
            self.service.users().messages().trash(
                userId="me", id=msg_id
            ).execute()
            count += 1
        return count
