"""Client API PlusVibe — wrapper typé pour les endpoints Unibox et Leads."""

import os
import time
import httpx
from typing import Optional
from pydantic import BaseModel


BASE_URL = "https://api.plusvibe.ai/api/v1"


class Workspace(BaseModel):
    id: str
    name: str


class Email(BaseModel):
    id: str
    thread_id: Optional[str] = None
    message_id: Optional[str] = None
    subject: Optional[str] = None
    body_html: Optional[str] = None
    body_text: Optional[str] = None
    content_preview: Optional[str] = None
    from_email: Optional[str] = None
    to_email: Optional[str] = None
    direction: Optional[str] = None  # "IN" or "OUT"
    is_unread: Optional[int] = None  # 0 = lu, 1 = non lu
    label: Optional[str] = None
    lead_email: Optional[str] = None
    lead_id: Optional[str] = None
    campaign_id: Optional[str] = None
    eaccount: Optional[str] = None  # Compte email expéditeur côté RightLiens
    created_at: Optional[str] = None
    raw: Optional[dict] = None


class Lead(BaseModel):
    id: str
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company_name: Optional[str] = None
    job_title: Optional[str] = None
    phone: Optional[str] = None
    status: Optional[str] = None
    label: Optional[str] = None
    raw: Optional[dict] = None


class PlusVibeClient:
    """Client synchrone pour l'API PlusVibe avec rate limiting."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("PLUSVIBE_API_KEY")
        if not self.api_key:
            raise ValueError("PLUSVIBE_API_KEY manquant")
        self.client = httpx.Client(
            base_url=BASE_URL,
            headers={"x-api-key": self.api_key},
            timeout=30.0,
        )
        self._last_request_time = 0.0
        self._min_interval = 0.2  # 5 req/sec max

    def _rate_limit(self):
        """Respecter la limite de 5 req/sec."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

    def _request(self, method: str, path: str, params: Optional[dict] = None,
                 json_data: Optional[dict] = None) -> dict:
        """Requête HTTP avec rate limiting et retry."""
        self._rate_limit()
        for attempt in range(3):
            try:
                response = self.client.request(
                    method, path, params=params, json=json_data
                )
                if response.status_code == 429:
                    wait = 2 ** attempt
                    print(f"  Rate limited, retry dans {wait}s...")
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if attempt < 2 and e.response.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                raise
        return {}

    # --- Workspaces ---

    def get_workspaces(self) -> list[Workspace]:
        """Récupère la liste des workspaces."""
        data = self._request("GET", "/authenticate")
        workspaces = data.get("workspaces", [])
        return [
            Workspace(id=ws.get("_id", ws.get("id", "")), name=ws.get("name", ""))
            for ws in workspaces
        ]

    # --- Unibox / Emails ---

    def get_emails(self, workspace_id: str, page_trail: Optional[str] = None,
                   label: Optional[str] = None,
                   campaign_id: Optional[str] = None) -> tuple[list["Email"], Optional[str]]:
        """Récupère les emails de la Unibox.

        Returns: (liste d'emails, page_trail pour la page suivante)
        Params acceptés par l'API: workspace_id, page_trail, label, campaign_id
        """
        params: dict = {"workspace_id": workspace_id}
        if page_trail:
            params["page_trail"] = page_trail
        if label:
            params["label"] = label
        if campaign_id:
            params["campaign_id"] = campaign_id
        data = self._request("GET", "/unibox/emails", params=params)
        emails_raw = data.get("data", [])
        next_page = data.get("page_trail")
        emails = [self._parse_email(e) for e in emails_raw] if isinstance(emails_raw, list) else []
        return emails, next_page

    def get_all_emails(self, workspace_id: str, max_pages: int = 5,
                       **kwargs) -> list["Email"]:
        """Récupère plusieurs pages d'emails."""
        all_emails = []
        page_trail = None
        for _ in range(max_pages):
            emails, page_trail = self.get_emails(
                workspace_id, page_trail=page_trail, **kwargs
            )
            all_emails.extend(emails)
            if not page_trail or not emails:
                break
        return all_emails

    def get_thread(self, workspace_id: str, thread_id: str) -> list["Email"]:
        """Récupère tous les emails d'un thread en parcourant la Unibox.

        Note: L'API PlusVibe n'a pas d'endpoint dédié pour les threads.
        On récupère tous les emails et on filtre par thread_id.
        """
        all_emails = self.get_all_emails(workspace_id, max_pages=10)
        thread_emails = [e for e in all_emails if e.thread_id == thread_id]
        thread_emails.sort(key=lambda e: e.created_at or "")
        return thread_emails

    def reply_to_email(self, workspace_id: str, reply_to_id: str,
                       subject: str, from_email: str, to_email: str,
                       body: str, cc: Optional[str] = None) -> dict:
        """Répond à un email via Unibox."""
        payload = {
            "reply_to_id": reply_to_id,
            "subject": subject,
            "from": from_email,
            "to": to_email,
            "body": body,
        }
        if cc:
            payload["cc"] = cc
        return self._request(
            "POST", "/unibox/emails/reply",
            params={"workspace_id": workspace_id},
            json_data=payload,
        )

    # --- Leads ---

    def get_leads(self, workspace_id: str, status: Optional[str] = None,
                  label: Optional[str] = None, email: Optional[str] = None,
                  limit: int = 50, page: int = 1) -> list[Lead]:
        """Récupère les leads d'un workspace."""
        params: dict = {"workspace_id": workspace_id, "limit": limit, "page": page}
        if status:
            params["status"] = status
        if label:
            params["label"] = label
        if email:
            params["email"] = email
        data = self._request("GET", "/lead/workspace-leads", params=params)
        leads_raw = data.get("leads", data.get("data", []))
        if isinstance(leads_raw, list):
            return [self._parse_lead(l) for l in leads_raw]
        return []

    def update_lead_label(self, workspace_id: str, lead_id: str,
                          label: str) -> dict:
        """Met à jour le label d'un lead."""
        return self._request(
            "POST", "/lead/label-update",
            params={"workspace_id": workspace_id},
            json_data={"lead_id": lead_id, "label": label},
        )

    # --- Parsers ---

    def _parse_email(self, raw: dict) -> Email:
        body = raw.get("body", {})
        if isinstance(body, dict):
            body_html = body.get("html", "")
            body_text = body.get("text", "")
        else:
            body_html = str(body) if body else ""
            body_text = ""

        return Email(
            id=raw.get("id", raw.get("_id", "")),
            thread_id=raw.get("thread_id"),
            message_id=raw.get("message_id"),
            subject=raw.get("subject"),
            body_html=body_html,
            body_text=body_text,
            content_preview=raw.get("content_preview"),
            from_email=raw.get("from_address_email"),
            to_email=raw.get("to_address_email_list"),
            direction=raw.get("direction"),
            is_unread=raw.get("is_unread"),
            label=raw.get("label"),
            lead_email=raw.get("lead"),
            lead_id=raw.get("lead_id"),
            campaign_id=raw.get("campaign_id"),
            eaccount=raw.get("eaccount"),
            created_at=raw.get("timestamp_created"),
            raw=raw,
        )

    def _parse_lead(self, raw: dict) -> Lead:
        return Lead(
            id=raw.get("_id", raw.get("id", "")),
            email=raw.get("email"),
            first_name=raw.get("first_name", raw.get("firstName")),
            last_name=raw.get("last_name", raw.get("lastName")),
            company_name=raw.get("company_name", raw.get("companyName")),
            job_title=raw.get("job_title", raw.get("jobTitle")),
            phone=raw.get("phone"),
            status=raw.get("status"),
            label=raw.get("label"),
            raw=raw,
        )

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
