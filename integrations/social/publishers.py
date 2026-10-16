"""Social publishing interfaces.

LinkedIn (Posts API, `w_member_social`) and Instagram (Graph API content
publishing, Business/Creator account linked to a Facebook Page) both require an
approved app and OAuth tokens that you must create. Until configured these
publishers raise IntegrationRevoked, so nothing is ever reported as published
without a platform ID returned by the API.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import httpx
from urllib.parse import quote

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
        # LinkedIn retires API versions about a year after release; keep this current (YYYYMM).
        self.version = os.environ.get("LINKEDIN_API_VERSION") or "202609"

    def configured(self) -> bool:
        return bool(self.token and self.author)

    def _headers(self, json: bool = True) -> dict:
        h = {"Authorization": f"Bearer {self.token}", "X-Restli-Protocol-Version": "2.0.0", "LinkedIn-Version": self.version}
        if json:
            h["Content-Type"] = "application/json"
        return h

    def _check(self, r: httpx.Response, step: str, ok=(200, 201)) -> None:
        if r.status_code in (401, 403):
            raise _revoked("linkedin", f"LinkedIn rejected the token/permissions (HTTP {r.status_code})")
        if r.status_code not in ok:
            raise IntegrationError(f"LinkedIn {step} failed HTTP {r.status_code}: {r.text[:200]}")

    def publish_video_file(self, path: str | os.PathLike, text: str, title: str = "", wait_s: float = 600,
                           poll_s: float = 10, sleep=time.sleep) -> dict:
        """Videos API: initializeUpload -> PUT parts -> finalizeUpload -> wait AVAILABLE -> post."""
        if not self.configured():
            raise _revoked("linkedin", "LinkedIn is not configured (LINKEDIN_ACCESS_TOKEN / LINKEDIN_AUTHOR_URN)")
        data = Path(path).read_bytes()
        r = self.client.post("https://api.linkedin.com/rest/videos?action=initializeUpload", headers=self._headers(),
                             json={"initializeUploadRequest": {"owner": self.author, "fileSizeBytes": len(data),
                                                               "uploadCaptions": False, "uploadThumbnail": False}})
        self._check(r, "video init", ok=(200,))
        v = r.json()["value"]
        etags = []
        for part in v["uploadInstructions"]:
            chunk = data[int(part["firstByte"]):int(part["lastByte"]) + 1]
            pr = self.client.put(part["uploadUrl"], content=chunk, headers={"Content-Type": "application/octet-stream"}, timeout=300)
            self._check(pr, "video part upload", ok=(200, 201))
            etag = (pr.headers.get("etag") or "").strip('"')
            if not etag:
                raise IntegrationError("LinkedIn video part upload returned no ETag")
            etags.append(etag)
        r = self.client.post("https://api.linkedin.com/rest/videos?action=finalizeUpload", headers=self._headers(),
                             json={"finalizeUploadRequest": {"video": v["video"], "uploadToken": v.get("uploadToken", ""),
                                                             "uploadedPartIds": etags}})
        self._check(r, "video finalize", ok=(200,))
        deadline = time.monotonic() + wait_s
        while True:
            r = self.client.get(f"https://api.linkedin.com/rest/videos/{quote(v['video'], safe='')}", headers=self._headers(False))
            self._check(r, "video status", ok=(200,))
            st = r.json().get("status")
            if st == "AVAILABLE":
                break
            if st == "PROCESSING_FAILED":
                raise IntegrationError(f"LinkedIn could not process the video: {r.json().get('processingFailureReason')}")
            if time.monotonic() > deadline:
                raise IntegrationError("LinkedIn is still processing the video; publishing will be retried")
            sleep(poll_s)
        return self._post({"content": {"media": {"id": v["video"], "title": title[:200]}}}, text)

    def _post(self, extra: dict, text: str) -> dict:
        r = self.client.post("https://api.linkedin.com/rest/posts", headers=self._headers(),
                             json={"author": self.author, "commentary": text, "visibility": "PUBLIC", "lifecycleState": "PUBLISHED",
                                   "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                                                    "thirdPartyDistributionChannels": []}, **extra})
        self._check(r, "publish", ok=(201,))
        pid = r.headers.get("x-restli-id")
        if not pid:
            raise IntegrationError("LinkedIn returned no post id")
        return {"platform_id": pid, "url": f"https://www.linkedin.com/feed/update/{pid}", "evidence": f"linkedin post {pid}"}

    def publish_text(self, text: str) -> dict:
        if not self.configured():
            raise _revoked("linkedin", "LinkedIn is not configured (LINKEDIN_ACCESS_TOKEN / LINKEDIN_AUTHOR_URN)")
        return self._post({}, text)


class InstagramPublisher:
    name = "instagram"

    def __init__(self, token: str | None = None, ig_user_id: str | None = None, transport=None):
        self.token = token or os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
        self.user = ig_user_id or os.environ.get("INSTAGRAM_USER_ID", "")
        self.client = httpx.Client(transport=transport, timeout=60)

    def configured(self) -> bool:
        return bool(self.token and self.user)

    API = os.environ.get("INSTAGRAM_API_VERSION", "v21.0")

    def publish_reel_file(self, path: str | os.PathLike, caption: str, wait_s: float = 600, poll_s: float = 10,
                          sleep=time.sleep) -> dict:
        """Upload a local video straight to Meta (resumable upload, no public URL needed) and publish it as a reel."""
        if not self.configured():
            raise _revoked("instagram", "Instagram is not configured (INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_USER_ID)")
        data = Path(path).read_bytes()
        graph = f"https://graph.facebook.com/{self.API}"
        r = self.client.post(f"{graph}/{self.user}/media", params={"upload_type": "resumable"},
                             headers={"Authorization": f"Bearer {self.token}"},
                             data={"media_type": "REELS", "caption": caption})
        self._check(r, "container")
        container = r.json()["id"]
        uri = r.json().get("uri") or f"https://rupload.facebook.com/ig-api-upload/{self.API}/{container}"
        r = self.client.post(uri, content=data, headers={"Authorization": f"OAuth {self.token}", "offset": "0",
                                                         "file_size": str(len(data))}, timeout=300)
        self._check(r, "upload")
        deadline = time.monotonic() + wait_s
        while True:
            r = self.client.get(f"{graph}/{container}", params={"fields": "status_code,status"},
                                headers={"Authorization": f"OAuth {self.token}"})
            self._check(r, "status")
            code = r.json().get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise IntegrationError(f"Instagram could not process the video: {r.json().get('status', code)}")
            if time.monotonic() > deadline:
                raise IntegrationError("Instagram is still processing the video; publishing will be retried")
            sleep(poll_s)
        r = self.client.post(f"{graph}/{self.user}/media_publish", data={"creation_id": container},
                             headers={"Authorization": f"OAuth {self.token}"})
        self._check(r, "publish")
        mid = r.json()["id"]
        return {"platform_id": mid, "evidence": f"instagram media {mid}"}

    def _check(self, r: httpx.Response, step: str) -> None:
        if r.status_code in (400, 401, 403) and "OAuth" in r.text:
            raise _revoked("instagram", "Instagram rejected the token/permissions")
        if r.status_code != 200:
            raise IntegrationError(f"Instagram {step} failed HTTP {r.status_code}: {r.text[:200]}")

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


class PostizPublisher:
    """Publishes through Postiz (cloud or self-hosted), which holds the LinkedIn and
    Instagram logins for you, so no LinkedIn/Meta developer app is needed. One
    instance per connected channel (Postiz "integration"). Postiz accepts the post
    and publishes it itself; the evidence is Postiz's post id, and a failure on
    the platform side shows in Postiz, not here.
    """

    def __init__(self, integration_id: str, platform: str, api_key: str | None = None, base_url: str | None = None,
                 transport=None):
        self.integration_id = integration_id
        self.platform = platform
        self.name = f"postiz:{platform}"
        self.key = api_key if api_key is not None else os.environ.get("POSTIZ_API_KEY", "")
        self.base = (base_url or os.environ.get("POSTIZ_API_URL") or "https://api.postiz.com/public/v1").rstrip("/")
        self.client = httpx.Client(transport=transport, timeout=120)
        self.account_id = f"postiz:{integration_id}"
        self._identifier: str | None = None

    def configured(self) -> bool:
        return bool(self.key and self.integration_id)

    def _check(self, r: httpx.Response, step: str) -> None:
        if r.status_code in (401, 403):
            raise _revoked("postiz", f"Postiz rejected the API key (HTTP {r.status_code})")
        if r.status_code >= 300:
            raise IntegrationError(f"Postiz {step} failed HTTP {r.status_code}: {r.text[:200]}")

    def _channel(self) -> str:
        """The channel's provider identifier (e.g. linkedin, linkedin-page, instagram), used as settings.__type."""
        if self._identifier:
            return self._identifier
        r = self.client.get(f"{self.base}/integrations", headers={"Authorization": self.key})
        self._check(r, "integrations")
        ch = next((c for c in r.json() if c.get("id") == self.integration_id), None)
        if ch is None:
            raise _revoked("postiz", f"Postiz channel {self.integration_id} not found; check config/brands.yaml")
        if ch.get("disabled"):
            raise _revoked("postiz", f"Postiz channel {ch.get('name') or self.integration_id} is disabled or needs reconnecting")
        self._identifier = ch.get("identifier") or ch.get("providerIdentifier") or self.platform
        return self._identifier

    def _upload(self, path: str | os.PathLike) -> dict:
        p = Path(path)
        with p.open("rb") as f:
            r = self.client.post(f"{self.base}/upload", headers={"Authorization": self.key}, files={"file": (p.name, f)})
        self._check(r, "upload")
        d = r.json()
        if not d.get("path"):
            raise IntegrationError(f"Postiz upload returned no media path: {str(d)[:200]}")
        return {"id": d.get("id"), "path": d["path"]}

    def _post(self, text: str, media: list[dict]) -> dict:
        ident = self._channel()
        settings: dict = {"__type": ident}
        if ident.startswith("instagram"):
            settings.update({"post_type": "post", "collaborators": []})
        body = {"type": "now", "date": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), "shortLink": False, "tags": [],
                "posts": [{"integration": {"id": self.integration_id}, "value": [{"content": text, "image": media}],
                           "settings": settings}]}
        r = self.client.post(f"{self.base}/posts", headers={"Authorization": self.key, "Content-Type": "application/json"},
                             json=body)
        self._check(r, "create post")
        d = r.json()
        first = d[0] if isinstance(d, list) and d else d if isinstance(d, dict) else {}
        pid = first.get("postId") or first.get("id")
        if not pid:
            raise IntegrationError(f"Postiz accepted the request but returned no post id: {str(d)[:200]}")
        return {"platform_id": f"postiz:{pid}", "url": None,
                "evidence": f"accepted by Postiz as post {pid} for {ident} channel {self.integration_id}"}

    def publish_text(self, text: str) -> dict:
        if not self.configured():
            raise _revoked("postiz", "Postiz is not configured (POSTIZ_API_KEY and a channel id in config/brands.yaml)")
        if self.platform == "instagram":
            raise IntegrationError("Instagram needs an image or video; this item has none")
        return self._post(text, [])

    def publish_reel_file(self, path: str | os.PathLike, caption: str, **_) -> dict:
        if not self.configured():
            raise _revoked("postiz", "Postiz is not configured (POSTIZ_API_KEY and a channel id in config/brands.yaml)")
        self._channel()  # fail on a missing/disabled channel before uploading anything
        return self._post(caption, [self._upload(path)])

    def publish_video_file(self, path: str | os.PathLike, text: str, title: str = "", **_) -> dict:
        return self.publish_reel_file(path, text)


class ZernioPublisher:
    """Publishes through Zernio (zernio.com, formerly Late): free for the first two
    connected accounts. You connect LinkedIn/Instagram on zernio.com; NOESIS sends
    the finished post. Media: presign -> PUT to storage -> post with mediaItems.
    A single video on Instagram publishes as a Reel. Evidence is Zernio's post id
    and per-platform status; the platform URL is kept when Zernio returns it.
    """

    def __init__(self, account_id: str, platform: str, api_key: str | None = None, base_url: str | None = None,
                 transport=None):
        self.zernio_account = account_id
        self.platform = platform
        self.name = f"zernio:{platform}"
        self.key = api_key if api_key is not None else os.environ.get("ZERNIO_API_KEY", "")
        self.base = (base_url or os.environ.get("ZERNIO_API_URL") or "https://zernio.com/api/v1").rstrip("/")
        self.client = httpx.Client(transport=transport, timeout=300)
        self.account_id = f"zernio:{account_id}"

    def configured(self) -> bool:
        return bool(self.key and self.zernio_account)

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    def _check(self, r: httpx.Response, step: str) -> None:
        if r.status_code in (401, 403):
            raise _revoked("zernio", f"Zernio rejected the API key or account (HTTP {r.status_code})")
        if r.status_code == 409:
            raise IntegrationError("Zernio: the same content was already posted to this account in the last 24 hours")
        if r.status_code >= 300:
            raise IntegrationError(f"Zernio {step} failed HTTP {r.status_code}: {r.text[:200]}")

    def _upload(self, path: str | os.PathLike) -> str:
        p = Path(path)
        ctype = "video/mp4" if p.suffix.lower() == ".mp4" else "video/quicktime" if p.suffix.lower() == ".mov" else "image/png"
        r = self.client.post(f"{self.base}/media/presign", headers=self._h(),
                             json={"filename": p.name, "contentType": ctype, "size": p.stat().st_size})
        self._check(r, "presign")
        d = r.json()
        with p.open("rb") as f:
            up = self.client.put(d["uploadUrl"], content=f.read(), headers={"Content-Type": ctype})
        if up.status_code >= 300:
            raise IntegrationError(f"Zernio media upload failed HTTP {up.status_code}")
        return d["publicUrl"]

    def _post(self, text: str, media_url: str | None) -> dict:
        body: dict = {"content": text, "publishNow": True,
                      "platforms": [{"platform": self.platform, "accountId": self.zernio_account}]}
        if media_url:
            body["mediaItems"] = [{"url": media_url, "type": "video"}]
        r = self.client.post(f"{self.base}/posts", headers={**self._h(), "x-request-id": "noesis-" + hashlib.sha256(f"{self.zernio_account}|{text}|{media_url}".encode()).hexdigest()[:32]},
                             json=body)
        self._check(r, "create post")
        post = (r.json() or {}).get("post") or {}
        pid = post.get("_id")
        if not pid:
            raise IntegrationError(f"Zernio accepted the request but returned no post id: {r.text[:200]}")
        plat = next((x for x in post.get("platforms") or [] if x.get("platform") == self.platform), {})
        if plat.get("status") in ("failed", "error"):
            raise IntegrationError(f"Zernio reports the {self.platform} post failed: {str(plat)[:200]}")
        return {"platform_id": f"zernio:{pid}", "url": plat.get("platformPostUrl"),
                "evidence": f"Zernio post {pid}: {self.platform} {plat.get('status') or post.get('status') or 'accepted'}"}

    def publish_text(self, text: str) -> dict:
        if not self.configured():
            raise _revoked("zernio", "Zernio is not configured (ZERNIO_API_KEY and an account id in config/brands.yaml)")
        if self.platform == "instagram":
            raise IntegrationError("Instagram needs an image or video; this item has none")
        return self._post(text, None)

    def publish_reel_file(self, path: str | os.PathLike, caption: str, **_) -> dict:
        if not self.configured():
            raise _revoked("zernio", "Zernio is not configured (ZERNIO_API_KEY and an account id in config/brands.yaml)")
        return self._post(caption, self._upload(path))

    def publish_video_file(self, path: str | os.PathLike, text: str, title: str = "", **_) -> dict:
        return self.publish_reel_file(path, text)
