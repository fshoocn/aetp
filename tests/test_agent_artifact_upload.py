from __future__ import annotations

import asyncio
import json

from agent.application.services import artifact_upload_service as module
from agent.application.services.artifact_upload_service import ArtifactUploadService


def test_artifact_upload_builds_master_multipart_request(tmp_path, monkeypatch) -> None:
    artifact = tmp_path / "report.xml"
    artifact.write_text("<testsuite />", encoding="utf-8")
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"artifact_id": "A-1", "kind": "report"}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["content_type"] = request.headers["Content-type"]
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    result = asyncio.run(
        ArtifactUploadService().upload(
            "http://master.local/internal/runs/R-1/artifacts?project_id=p1",
            artifact,
            kind="report",
            filename="pytest-junit.xml",
        )
    )

    assert result["artifact_id"] == "A-1"
    assert "project_id=p1" in str(captured["url"])
    assert "kind=report" in str(captured["url"])
    assert b'filename="pytest-junit.xml"' in captured["body"]
    assert b"<testsuite />" in captured["body"]
    assert "multipart/form-data" in str(captured["content_type"])
    assert captured["timeout"] == 60
