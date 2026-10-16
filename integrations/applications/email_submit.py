"""Email applications: send an application to the address an opportunity names.

Only verified profile answers go into the email, and only verified documents
are attached (paths relative to the data directory, e.g. documents/resume.pdf).
Whether it may be sent without asking you is decided by policies/applications.yaml,
never here. Returns the Gmail message id as evidence.
"""
from __future__ import annotations

import json
from pathlib import Path

from workforce.policies.engine import IntegrationError

MAX_ATTACH_BYTES = 10 * 1024 * 1024


def compose(app: dict, opp: dict, profile: dict) -> tuple[str, str]:
    name = profile.get("name") or ""
    answers = json.loads(app.get("answers_json") or "{}")
    verified = {k: a["text"] for k, a in answers.items() if a.get("status") == "verified" and a.get("text")}
    parts = [f"Dear {opp.get('organizer') or 'Hiring Team'},", "",
             f"I would like to apply for {opp['title']}."]
    if "short_bio" in verified:
        parts += ["", verified.pop("short_bio")]
    for k, text in verified.items():
        parts += ["", f"{k.replace('_', ' ').capitalize()}:", text]
    parts += ["", "My documents are attached. Thank you for considering my application.", "", "Best regards,", name]
    if profile.get("email"):
        parts.append(profile["email"])
    return f"Application: {opp['title'][:120]}" + (f" – {name}" if name else ""), "\n".join(parts)


def make_email_submitter(mailbox, profile_loader, data_dir: str):
    def submit(app: dict, opp: dict) -> dict:
        from workforce.agents.applications import submission_method
        method, dest = submission_method(opp)
        if method != "email" or not dest:
            raise IntegrationError("this opportunity has no application email address")
        profile = profile_loader()
        subject, body = compose(dict(app), dict(opp), profile)
        files = []
        for d in json.loads(app.get("documents_json") or "[]"):
            p = (Path(data_dir) / d["path"]).resolve()
            if Path(data_dir).resolve() not in p.parents or not p.is_file():
                raise IntegrationError(f"document {d['path']} not found under the data folder")
            if p.stat().st_size > MAX_ATTACH_BYTES:
                raise IntegrationError(f"document {d['path']} is larger than 10 MB")
            files.append(p)
        res = mailbox.send(dest, subject, body, attachments=files)
        return {**res, "confirmation_id": res.get("message_id"), "evidence": f"emailed {dest}: {res.get('evidence')}"}
    return submit
