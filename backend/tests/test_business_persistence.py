"""CRM 和受控文件的真实 PostgreSQL 恢复与隔离测试。"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import psycopg

from email_agent.persistence import open_postgres_persistence


def test_postgres_restores_crm_and_files_with_user_isolation(
    postgres_test_url: str,
) -> None:
    run_id = uuid.uuid4().hex
    user_id = f"business-owner-{run_id}"
    other_user = f"business-other-{run_id}"
    email = f"contact-{run_id}@example.com"
    file_id = f"file_{run_id}"
    now = datetime.now(UTC)
    contact = {
        "email": email,
        "display_name": "Contact",
        "frequency": 2,
        "last_contact": now.isoformat(),
        "contact_type": "person",
        "company": "example.com",
        "relationship": None,
        "priority": "medium",
        "deal": None,
        "next_contact_date": None,
        "tags": [],
        "notes": None,
        "needs_reply": False,
        "updated_at": now.isoformat(),
    }
    file_record = {
        "file_id": file_id,
        "filename": "notes.txt",
        "content_type": "text/plain",
        "size_bytes": 4,
        "sha256": "0" * 64,
        "extracted_text": "text",
        "truncated": False,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
    }

    async def run() -> None:
        async with open_postgres_persistence(postgres_test_url) as persistence:
            persistence.state.put_crm_contact(user_id, email, contact)
            persistence.state.put_uploaded_file(user_id, file_id, file_record, b"text")

        async with open_postgres_persistence(postgres_test_url) as restarted:
            assert restarted.state.get_crm_contact(user_id, email)["email"] == email
            assert restarted.state.get_crm_contact(other_user, email) is None
            assert restarted.state.get_uploaded_file(user_id, file_id)["filename"] == "notes.txt"
            assert restarted.state.get_uploaded_file(other_user, file_id) is None
            assert restarted.state.delete_uploaded_file(user_id, file_id) is True

    try:
        asyncio.run(run())
    finally:
        with psycopg.connect(postgres_test_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM email_agent_crm_contacts
                    WHERE user_id IN (%s, %s)
                    """,
                    (user_id, other_user),
                )
                cur.execute(
                    "DELETE FROM email_agent_uploaded_files WHERE file_id = %s",
                    (file_id,),
                )
