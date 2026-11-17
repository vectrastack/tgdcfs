import json
from typing import Dict, FrozenSet, Optional

from tgdcfs.backends.base import IStore
from tgdcfs.core.mirror import MirrorGroup
from tgdcfs.core.model import TGFSMetadata
from tgdcfs.core.repository.interface import IMetaDataRepository
from tgdcfs.crypto.buffer import MetadataCrypto
from tgdcfs.errors import (
    MetadataNotInitialized,
    NoPinnedMessage,
    TechnicalError,
)
from tgdcfs.reqres import (
    FileMessageFromBuffer,
    MessageRespWithDocument,
)


class PinnedMessageMetadataRepository(IMetaDataRepository):
    """The directory tree as a pinned document in one store.

    The store is whichever channel the metadata entry names -- the file
    system's primary by default, any of its mirrors otherwise. That is
    what tells a Telegram pinned message and a Discord pinned message
    apart: two entries, one per store, are two redundant copies of the
    same tree.

    Reads and writes go to that store directly, so the repository works
    the same whether its store is the primary or a mirror, and the blob
    is at-rest encrypted when encryption is configured (``crypto``).
    """

    METADATA_FILE_NAME = "metadata.json"

    def __init__(
        self,
        store: IStore,
        mirror_group: Optional[MirrorGroup] = None,
        skip_mirror_keys: FrozenSet[str] = frozenset(),
        crypto: Optional[MetadataCrypto] = None,
    ):
        super().__init__()

        self._store = store
        self._mirror_group = mirror_group
        # Store keys whose pinned copy is maintained by their own
        # metadata entry; the automatic promotion copy skips them.
        self._skip_mirror_keys = skip_mirror_keys
        self._crypto = crypto

        self._message_id: Optional[int] = None
        # Mirror store key -> message id of the pinned metadata copy there.
        self._mirror_message_ids: Dict[str, int] = {}

    def _serialize(self) -> bytes:
        if not self.metadata:
            raise MetadataNotInitialized()
        buffer = json.dumps(self.metadata.to_dict()).encode()
        if self._crypto is not None:
            buffer = self._crypto.encrypt(buffer)
        return buffer

    async def push(self) -> None:
        buffer = self._serialize()
        # The reader below treats the pinned document as a single part, so
        # the blob has to fit one message of the store. That is 2 GiB on
        # Telegram and a few tens of megabytes on Discord.
        if len(buffer) > self._store.caps.max_part_bytes:
            raise TechnicalError(
                f"The metadata blob ({len(buffer)} bytes) does not fit one message "
                f"of store {self._store.key} "
                f"({self._store.caps.max_part_bytes} bytes); use the "
                f"github_repo or gitlab_repo metadata type for this file system"
            )
        if self._message_id is not None:
            await self._store.replace_document(
                self._message_id, buffer, self.METADATA_FILE_NAME
            )
            # The primary blob was edited in place, so the mirror copies
            # are stale now: forward the fresh blob, pin it, drop the old
            # copy. Keeps every mirror store self-sufficient for a
            # config-level promotion after the primary is lost.
            await self._mirror_pinned()
        else:
            resp = await self._store.upload(
                FileMessageFromBuffer.new(
                    name=self.METADATA_FILE_NAME,
                    buffer=buffer,
                )
            )
            message_id = resp[0].message_id
            await self._store.pin_message(message_id=message_id)
            self._message_id = message_id
            await self._mirror_pinned()

    async def _mirror_pinned(self) -> None:
        """Keep a pinned copy of the blob in every mirror store.

        Only the entry whose store is the file system's primary does
        this; an entry pinned to a mirror store is itself the explicit
        redundant copy. Stores with their own metadata entry are skipped
        -- a second pinned metadata document in one channel would be
        ambiguous to read back.
        """
        if not self._mirror_group or self._message_id is None:
            return
        if self._store.key != self._mirror_group.primary.key:
            return
        await self._mirror_group.mirror_pinned(
            self._message_id, self._mirror_message_ids, skip=set(self._skip_mirror_keys)
        )

    async def new_metadata(self) -> MessageRespWithDocument:
        from tgdcfs.core.model import TGFSDirectory

        self.metadata = TGFSMetadata(TGFSDirectory.root_dir())
        self._message_id = None
        await self.push()
        return await self._store.get_pinned_message()

    async def get(self) -> TGFSMetadata:
        try:
            pinned_message = await self._store.get_pinned_message()
        except NoPinnedMessage:
            pinned_message = await self.new_metadata()

        resp = await self._store.download_file(
            pinned_message.message_id, 0, pinned_message.document.size - 1
        )
        buffer = bytearray()
        async for chunk in resp.chunks:
            buffer.extend(chunk)
        data = bytes(buffer)
        if self._crypto is not None:
            data = self._crypto.decrypt(data)

        metadata = TGFSMetadata.from_dict(json.loads(data))

        self._message_id = pinned_message.message_id
        return metadata
