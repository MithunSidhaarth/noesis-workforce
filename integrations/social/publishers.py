"""Social publishing interfaces.

LinkedIn (Posts API, `w_member_social`) and Instagram (Graph API content
publishing, Business/Creator account linked to a Facebook Page) both require an
approved app and OAuth tokens that you must create. Until configured these
publishers raise IntegrationRevoked, so nothing is ever reported as published
without a platform ID returned by the API.
"""
from __future__ import annotations

import os

import httpx

from workforce.policies.engine import IntegrationError, IntegrationRevoked


def _revoked(name: str, msg: str) -> IntegrationRevoked:
    e = IntegrationRevoked(msg)
    e.integration = name  # type: ignore[attr-defined]
    return e


class LinkedInPublisher:
    name = "linkedin"

    def __init__(self, token: str | None = None, author_urn: str | None = None, transport=None):
        self.token = token or os.environ.get("LINKEDIN_ACCESS_TOKEN", "")
        self.author = author_urn or os.environ.get("LINKEDIN_AUTHOR_URN", "")
        self.client = httpx.Client(transport=transport, timeout=30)

    def configured(self) -> bool:
        return bool(self.token and self.author)

    def publish_text(self, text: str) -> dict:
        if not self.configured():
            raise _revoked("linkedin", "LinkedIn is not configured (LINKEDIN_ACCESS_TOKEN / LINKEDIN_AUTHOR_URN)")
        r = self.client.post("https://api.linkedin.com/rest/posts", headers={
            "Authorization": f"Bearer {self.token}", "X-Restli-Protocol-Version": "2.0.0",
            "LinkedIn-Version": os.environ.get("LINKEDIN_API_VERSION", "202501"), "Content-Type": "application/json"},
            json={"author": self.author, "commentary": text, "visibility": "PUBLIC", "lifecycleState": "PUBLISHED",
                  "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []}})
        if r.status_code in (401, 403):
            raise _revoked("linkedin", f"LinkedIn rejected the token/permissions (HTTP {r.status_code})")
        if r.status_code != 201:
            raise IntegrationError(f"LinkedIn publish failed HTTP {r.status_code}: {r.text[:200]}")
        pid = r.headers.get("x-restli-id")
        if not pid:
            raise IntegrationError("LinkedIn returned no post id")
        return {"platform_id": pid, "url": f"https://www.linkedin.com/feed/update/{pid}", "evidence": f"linkedin post {pid}"}


class InstagramPublisher:
    name = "instagram"

    def __init__(self, token: str | None = None, ig_user_id: str | None = None, transport=None):
        self.token = token or os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
        self.user = ig_user_id or os.environ.get("INSTAGRAM_USER_ID", "")
        self.client = httpx.Client(transport=transport, timeout=60)

    def configured(self) -> bool:
        return bool(self.token and self.user)

    def publish_reel(self, video_url: str, caption: str) -> dict:
        """video_url must be a publicly reachable HTTPS URL (Graph API pulls it)."""
        if not self.configured():
            raise _revoked("instagram", "Instagram is not configured (INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_USER_ID)")
        base = f"https://graph.facebook.com/v21.0/{self.user}"
        r = self.client.post(f"{base}/media", data={"media_type": "REELS", "video_url": video_url, "caption": caption,
                                                    "access_token": self.token})
        if r.status_code in (400, 401, 403) and "OAuth" in r.text:
            raise _revoked("instagram", "Instagram rejected the token/permissions")
        if r.status_code != 200:
            raise IntegrationError(f"Instagram container failed HTTP {r.status_code}: {r.text[:200]}")
        container = r.json()["id"]
        r = self.client.post(f"{base}/media_publish", data={"creation_id": container, "access_token": self.token})
        if r.status_code != 200:
            raise IntegrationError(f"Instagram publish failed HTTP {r.status_code}: {r.text[:200]} (container may still be processing)")
        mid = r.json()["id"]
        return {"platform_id": mid, "evidence": f"instagram media {mid}"}
