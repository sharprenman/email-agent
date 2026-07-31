"""受控上传文件的校验、生命周期和用户隔离测试。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from email_agent.config import AuthContext
from email_agent.content_tools import AttachmentTextService
from email_agent.files import UploadedFileError, UploadedFileService
from email_agent.persistence import build_in_memory_persistence


def _service(persistence, user_id: str = "owner") -> UploadedFileService:
    return UploadedFileService(
        persistence.state,
        AuthContext(user_id=user_id),
        AttachmentTextService(max_attachment_bytes=1024),
        max_bytes=1024,
    )


def test_upload_returns_controlled_id_and_never_exposes_path() -> None:
    persistence = build_in_memory_persistence()
    service = _service(persistence)

    record = asyncio.run(
        service.upload(
            "../../private\\说明.txt",
            "text/plain",
            "安全正文".encode(),
        )
    )
    context = asyncio.run(service.build_context((record.file_id,)))

    assert record.file_id.startswith("file_")
    assert record.filename == "说明.txt"
    assert "private" not in context
    assert "不可信数据" in context
    assert "安全正文" in context


def test_uploaded_file_is_user_isolated_and_deletable() -> None:
    persistence = build_in_memory_persistence()
    owner = _service(persistence)
    other = _service(persistence, "other")
    record = asyncio.run(owner.upload("notes.md", "text/markdown", b"# Notes"))

    with pytest.raises(UploadedFileError, match="无权访问"):
        asyncio.run(other.get(record.file_id))

    assert asyncio.run(owner.delete(record.file_id)) is True
    assert asyncio.run(owner.delete(record.file_id)) is False
    with pytest.raises(UploadedFileError, match="不存在"):
        asyncio.run(owner.get(record.file_id))


def test_expired_uploaded_file_is_unavailable() -> None:
    persistence = build_in_memory_persistence()
    service = _service(persistence)
    record = asyncio.run(service.upload("notes.txt", "text/plain", b"notes"))
    expired = record.model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    persistence.state.put_uploaded_file(
        "owner",
        record.file_id,
        expired.model_dump(mode="json"),
        b"notes",
    )

    with pytest.raises(UploadedFileError, match="已过期"):
        asyncio.run(service.get(record.file_id))


@pytest.mark.parametrize(
    ("filename", "content_type", "content"),
    [
        ("empty.txt", "text/plain", b""),
        ("fake.pdf", "text/plain", b"%PDF-fake"),
        ("program.exe", "application/octet-stream", b"MZ"),
        ("broken.pdf", "application/pdf", b"not-pdf"),
    ],
)
def test_upload_rejects_empty_disguised_and_unsafe_files(
    filename: str,
    content_type: str,
    content: bytes,
) -> None:
    service = _service(build_in_memory_persistence())

    with pytest.raises(UploadedFileError):
        asyncio.run(service.upload(filename, content_type, content))
