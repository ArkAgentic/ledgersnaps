from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class StoredArtifact:
    artifact_ref: str
    backend: str
    size_bytes: int


class StorageBackend:
    """Temporary artifact storage backend.

    Backends:
    - localfs (default): write to .data/artifacts
    - azureblob: upload to Azure Blob Storage
    """

    def __init__(self, kind: Optional[str] = None) -> None:
        self.kind = (kind or os.getenv("STORAGE_BACKEND", "localfs")).strip().lower()

    def put_bytes(self, *, tenant_id: str, user_id: str, job_id: str, filename: str, content: bytes) -> StoredArtifact:
        if self.kind == "azureblob":
            return self._put_azureblob(tenant_id=tenant_id, user_id=user_id, job_id=job_id, filename=filename, content=content)
        return self._put_localfs(tenant_id=tenant_id, user_id=user_id, job_id=job_id, filename=filename, content=content)

    def get_bytes(self, artifact_ref: str) -> bytes:
        if self.kind == "azureblob":
            return self._get_azureblob(artifact_ref)
        return self._get_localfs(artifact_ref)

    def delete(self, artifact_ref: str) -> None:
        if self.kind == "azureblob":
            self._delete_azureblob(artifact_ref)
            return
        self._delete_localfs(artifact_ref)

    # -------- localfs --------
    def _artifact_root(self) -> Path:
        p = Path(os.getenv("LEDGERSNAPS_ARTIFACT_ROOT", os.path.join(os.getcwd(), ".data", "artifacts")))
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _put_localfs(self, *, tenant_id: str, user_id: str, job_id: str, filename: str, content: bytes) -> StoredArtifact:
        ext = Path(filename or "upload.bin").suffix
        path = self._artifact_root() / tenant_id / user_id / job_id
        path.mkdir(parents=True, exist_ok=True)
        f = path / f"{uuid.uuid4().hex}{ext}"
        f.write_bytes(content)
        return StoredArtifact(artifact_ref=f"localfs://{f}", backend="localfs", size_bytes=len(content))

    def _get_localfs(self, artifact_ref: str) -> bytes:
        if not artifact_ref.startswith("localfs://"):
            raise RuntimeError("artifact_ref_invalid")
        p = Path(artifact_ref.replace("localfs://", "", 1))
        return p.read_bytes()

    def _delete_localfs(self, artifact_ref: str) -> None:
        if not artifact_ref.startswith("localfs://"):
            return
        p = Path(artifact_ref.replace("localfs://", "", 1))
        if p.exists():
            p.unlink()
        # best-effort empty dir cleanup
        try:
            parent = p.parent
            while parent.name and parent != self._artifact_root():
                parent.rmdir()
                parent = parent.parent
        except Exception:
            pass

    # -------- azure blob --------
    def _azure_client(self):
        conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()
        container = os.getenv("AZURE_STORAGE_CONTAINER", "").strip()
        if not conn or not container:
            raise RuntimeError("azureblob_not_configured: set AZURE_STORAGE_CONNECTION_STRING and AZURE_STORAGE_CONTAINER")
        try:
            from azure.storage.blob import BlobServiceClient  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("azure_storage_sdk_missing: pip install azure-storage-blob") from e
        svc = BlobServiceClient.from_connection_string(conn)
        return svc, container

    def _put_azureblob(self, *, tenant_id: str, user_id: str, job_id: str, filename: str, content: bytes) -> StoredArtifact:
        svc, container = self._azure_client()
        blob_name = f"tenant/{tenant_id}/user/{user_id}/jobs/{job_id}/{uuid.uuid4().hex}-{Path(filename or 'upload.bin').name}"
        bc = svc.get_blob_client(container=container, blob=blob_name)
        bc.upload_blob(content, overwrite=True)
        return StoredArtifact(artifact_ref=f"azureblob://{container}/{blob_name}", backend="azureblob", size_bytes=len(content))

    def _get_azureblob(self, artifact_ref: str) -> bytes:
        if not artifact_ref.startswith("azureblob://"):
            raise RuntimeError("artifact_ref_invalid")
        svc, _container = self._azure_client()
        rest = artifact_ref.replace("azureblob://", "", 1)
        container, blob_name = rest.split("/", 1)
        bc = svc.get_blob_client(container=container, blob=blob_name)
        return bc.download_blob().readall()

    def _delete_azureblob(self, artifact_ref: str) -> None:
        if not artifact_ref.startswith("azureblob://"):
            return
        svc, _container = self._azure_client()
        rest = artifact_ref.replace("azureblob://", "", 1)
        container, blob_name = rest.split("/", 1)
        bc = svc.get_blob_client(container=container, blob=blob_name)
        try:
            bc.delete_blob(delete_snapshots="include")
        except Exception:
            # cleanup best-effort
            pass


def artifact_exists_local(artifact_ref: str) -> bool:
    if not artifact_ref.startswith("localfs://"):
        return False
    p = Path(artifact_ref.replace("localfs://", "", 1))
    return p.exists()
