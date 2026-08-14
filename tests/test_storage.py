"""P4.7：文件存储端口与脚本存储服务测试。"""

from __future__ import annotations

from master.adapters.storage.local_storage import LocalStorage
from master.application.services.script_storage_service import ScriptStorageService


def test_local_storage_put_open_exists_delete(tmp_path) -> None:
    storage = LocalStorage(root=tmp_path)
    key = "scripts/S-1/1/archive.zip"

    assert not storage.exists(key)
    storage.put(key, b"hello script")
    assert storage.exists(key)

    with storage.open(key) as stream:
        assert stream.read() == b"hello script"

    storage.delete(key)
    assert not storage.exists(key)
    # 删除不存在的键静默忽略
    storage.delete(key)


def test_local_storage_supports_absolute_key(tmp_path) -> None:
    storage = LocalStorage(root=tmp_path)
    abs_path = tmp_path / "other" / "file.bin"
    abs_path.parent.mkdir(parents=True)
    abs_path.write_bytes(b"absolute")

    assert storage.exists(str(abs_path))
    with storage.open(str(abs_path)) as stream:
        assert stream.read() == b"absolute"


def test_script_storage_service_key_rule(tmp_path) -> None:
    svc = ScriptStorageService(LocalStorage(root=tmp_path))

    key = svc.store_script("S-1", 2, "archive.zip", b"payload")
    assert key == "scripts/S-1/2/archive.zip"

    assert svc.script_exists(key)
    with svc.open_script(key) as stream:
        assert stream.read() == b"payload"

    svc.delete_script(key)
    assert not svc.script_exists(key)
