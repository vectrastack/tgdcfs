"""The directory tree as one JSON file in a GitLab project.

The pinned message keeps the tree in a channel; this keeps it in a git
repository instead -- same blob, same push-based contract, so the two
combine into a redundant metadata set where either can fail without the
file system noticing.

Everything runs through GitLab's REST API (v4) with the ``requests``
library that already ships as a PyGithub dependency. The calls are
blocking, so they run on a worker thread like the GitHub repository's
do, keeping the event loop free.
"""

import asyncio
import base64
import json
import logging
import urllib.parse
from typing import Optional

import requests

from tgdcfs.config import GitlabRepoConfig
from tgdcfs.core.model import TGFSMetadata
from tgdcfs.core.repository.interface import IMetaDataRepository
from tgdcfs.crypto.buffer import MetadataCrypto
from tgdcfs.errors import TechnicalError

logger = logging.getLogger(__name__)

FILE_PATH = "metadata.json"
TIMEOUT_SECONDS = 30
# How often a push that GitLab refused is retried before it is given up
# for this round (the redundant wrapper retries on the next push).
PUSH_ATTEMPTS = 3
PUSH_RETRY_DELAY = 1.5


class GitlabRepoMetadataRepository(IMetaDataRepository):
    def __init__(
        self,
        config: GitlabRepoConfig,
        crypto: Optional[MetadataCrypto] = None,
    ):
        super().__init__()
        self._config = config
        self._crypto = crypto
        self._session = requests.Session()
        self._session.headers["PRIVATE-TOKEN"] = config.access_token
        # Serialises the pushes: metadata writes are rare and small, and
        # keeping them in order stops the last writer from being undone
        # by the one before it.
        self._lock = asyncio.Lock()

    def close(self) -> None:
        self._session.close()

    def _file_url(self) -> str:
        project = urllib.parse.quote(self._config.repo, safe="")
        path = urllib.parse.quote(FILE_PATH, safe="")
        return f"{self._config.url}/api/v4/projects/{project}/repository/files/{path}"

    def _read_file(self) -> Optional[bytes]:
        """The stored blob, or ``None`` when the project has none yet.

        Raises on anything but a missing file: an unreachable GitLab is
        the caller's signal to fall back to the other metadata stores.
        """
        response = self._session.get(
            self._file_url() + "/raw",
            params={"ref": self._config.branch},
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.content

    async def get(self) -> TGFSMetadata:
        data = await asyncio.to_thread(self._read_file)
        if data is None:
            # A brand-new project: start from an empty tree, like the
            # pinned message does when the channel has no pin yet.
            from tgdcfs.core.model import TGFSDirectory

            self.metadata = TGFSMetadata(TGFSDirectory.root_dir())
            return self.metadata
        if self._crypto is not None:
            data = self._crypto.decrypt(data)
        return TGFSMetadata.from_dict(json.loads(data))

    def _write_file(self, content: bytes) -> None:
        body = {
            "branch": self._config.branch,
            "content": base64.b64encode(content).decode(),
            "encoding": "base64",
            "commit_message": "Update TGDCFS metadata",
        }
        # The file may or may not exist yet; PUT updates, POST creates,
        # and either can lose the race on a brand-new project, so both
        # are tried before giving up.
        response = self._session.put(
            self._file_url(), json=body, timeout=TIMEOUT_SECONDS
        )
        if response.status_code in (200, 201):
            return
        created = self._session.post(
            self._file_url(), json=body, timeout=TIMEOUT_SECONDS
        )
        if created.status_code in (200, 201):
            return
        raise TechnicalError(
            f"GitLab refused the metadata write "
            f"({response.status_code} {response.text[:200]}; "
            f"{created.status_code} {created.text[:200]})"
        )

    async def push(self) -> None:
        from tgdcfs.errors import MetadataNotInitialized

        if not self.metadata:
            raise MetadataNotInitialized()
        buffer = json.dumps(self.metadata.to_dict()).encode()
        if self._crypto is not None:
            buffer = self._crypto.encrypt(buffer)
        async with self._lock:
            for attempt in range(PUSH_ATTEMPTS):
                try:
                    await asyncio.to_thread(self._write_file, buffer)
                    return
                except Exception as ex:
                    if attempt + 1 == PUSH_ATTEMPTS:
                        raise
                    logger.warning(
                        f"Writing the metadata to {self._config.repo} on "
                        f"{self._config.url} failed ({ex}); trying again"
                    )
                    await asyncio.sleep(PUSH_RETRY_DELAY * (attempt + 1))
