import json

import pytest

from tgdcfs.backends.base import StoreCapabilities
from tgdcfs.backends.telegram.store import TelegramStore
from tgdcfs.core.model import TGFSDirectory, TGFSMetadata
from tgdcfs.core.repository.impl.metadata.pinned_message import (
    PinnedMessageMetadataRepository,
)
from tgdcfs.crypto.buffer import MetadataCrypto
from tgdcfs.errors import MetadataNotInitialized, NoPinnedMessage
from tgdcfs.reqres import (
    Document,
    FileMessageFromBuffer,
    MessageRespWithDocument,
    SentFileMessage,
)


class _Chunks:
    def __init__(self, data: bytes):
        self._data = data

    @property
    def chunks(self):
        async def iterator():
            yield self._data

        return iterator()


class TestPinnedMessageMetadataRepository:
    @pytest.fixture
    def store(self, mocker):
        store = mocker.AsyncMock(spec=TelegramStore)
        store.key = "tg:111"
        store.caps = StoreCapabilities(
            max_part_bytes=1 << 30, max_text_chars=4096, supports_server_copy=True
        )
        return store

    @pytest.fixture
    def repository(self, store) -> PinnedMessageMetadataRepository:
        return PinnedMessageMetadataRepository(store)

    @pytest.fixture
    def sample_metadata(self) -> TGFSMetadata:
        return TGFSMetadata(TGFSDirectory.root_dir())

    @pytest.fixture
    def sample_pinned_message(self) -> MessageRespWithDocument:
        document = Document(
            size=1024,
            id=456,
            access_hash=789,
            file_reference=b"test_reference",
            mime_type="application/json",
        )
        return MessageRespWithDocument(message_id=123, text="", document=document)

    def test_init(self, store):
        repo = PinnedMessageMetadataRepository(store)

        assert repo._store is store
        assert repo._message_id is None
        assert repo.metadata is None

    def test_metadata_file_name_constant(self):
        assert PinnedMessageMetadataRepository.METADATA_FILE_NAME == "metadata.json"

    @pytest.mark.asyncio
    async def test_push_without_metadata_raises_error(self, repository):
        with pytest.raises(MetadataNotInitialized):
            await repository.push()

    @pytest.mark.asyncio
    async def test_push_new_metadata_creates_pinned_message(
        self, repository, store, sample_metadata
    ):
        repository.metadata = sample_metadata

        store.upload.return_value = [SentFileMessage(message_id=456, size=1024)]

        await repository.push()

        store.upload.assert_called_once()
        call_args = store.upload.call_args[0][0]
        assert isinstance(call_args, FileMessageFromBuffer)
        assert call_args.name == "metadata.json"
        assert json.loads(call_args.buffer.decode()) == sample_metadata.to_dict()

        store.pin_message.assert_called_once_with(message_id=456)
        assert repository._message_id == 456

    @pytest.mark.asyncio
    async def test_push_existing_metadata_replaces_document(
        self, repository, store, sample_metadata
    ):
        repository.metadata = sample_metadata
        repository._message_id = 789

        await repository.push()

        store.replace_document.assert_called_once_with(
            789, json.dumps(sample_metadata.to_dict()).encode(), "metadata.json"
        )
        store.pin_message.assert_not_called()
        store.upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_push_encrypts_the_blob_when_crypto_is_set(
        self, store, sample_metadata
    ):
        crypto = MetadataCrypto(b"k" * 32, 64)
        repository = PinnedMessageMetadataRepository(store, crypto=crypto)
        repository.metadata = sample_metadata

        store.upload.return_value = [SentFileMessage(message_id=1, size=1)]

        await repository.push()

        blob = store.upload.call_args[0][0].buffer
        assert blob != json.dumps(sample_metadata.to_dict()).encode()
        assert json.loads(crypto.decrypt(blob)) == sample_metadata.to_dict()

    @pytest.mark.asyncio
    async def test_push_refuses_a_blob_that_does_not_fit_one_message(
        self, repository, store, sample_metadata
    ):
        store.caps = StoreCapabilities(
            max_part_bytes=16, max_text_chars=4096, supports_server_copy=True
        )
        repository.metadata = sample_metadata

        with pytest.raises(Exception, match="does not fit one message"):
            await repository.push()
        store.upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_with_existing_pinned_message(
        self, repository, store, sample_pinned_message, sample_metadata
    ):
        store.get_pinned_message.return_value = sample_pinned_message
        store.download_file.return_value = _Chunks(
            json.dumps(sample_metadata.to_dict()).encode()
        )

        result = await repository.get()

        store.get_pinned_message.assert_called_once()
        store.download_file.assert_called_once_with(123, 0, 1023)

        assert isinstance(result, TGFSMetadata)
        assert repository._message_id == 123

    @pytest.mark.asyncio
    async def test_get_decrypts_the_blob_when_crypto_is_set(
        self, store, sample_pinned_message, sample_metadata
    ):
        crypto = MetadataCrypto(b"k" * 32, 64)
        repository = PinnedMessageMetadataRepository(store, crypto=crypto)
        store.get_pinned_message.return_value = sample_pinned_message
        store.download_file.return_value = _Chunks(
            crypto.encrypt(json.dumps(sample_metadata.to_dict()).encode())
        )

        result = await repository.get()

        assert isinstance(result, TGFSMetadata)

    @pytest.mark.asyncio
    async def test_get_without_pinned_message_creates_new(
        self, repository, store, sample_pinned_message
    ):
        # First call raises NoPinnedMessage, second call returns the message
        store.get_pinned_message.side_effect = [
            NoPinnedMessage(),
            sample_pinned_message,
        ]
        store.upload.return_value = [SentFileMessage(message_id=123, size=1024)]

        async def mock_content_iterator():
            root_dir = TGFSDirectory.root_dir()
            metadata = TGFSMetadata(root_dir)
            yield json.dumps(metadata.to_dict()).encode()

        store.download_file.return_value = _Chunks(
            json.dumps(TGFSMetadata(TGFSDirectory.root_dir()).to_dict()).encode()
        )

        result = await repository.get()

        assert store.get_pinned_message.call_count == 2
        store.upload.assert_called_once()
        store.pin_message.assert_called_once_with(message_id=123)
        store.download_file.assert_called_once()

        assert isinstance(result, TGFSMetadata)
        assert repository._message_id == 123

    @pytest.mark.asyncio
    async def test_get_handles_complex_metadata_structure(
        self, repository, store, sample_pinned_message
    ):
        # Create a more complex metadata structure
        root_dir = TGFSDirectory.root_dir()
        root_dir.create_dir("subdir1")
        root_dir.create_dir("subdir2")
        complex_metadata = TGFSMetadata(root_dir)

        store.get_pinned_message.return_value = sample_pinned_message
        store.download_file.return_value = _Chunks(
            json.dumps(complex_metadata.to_dict()).encode()
        )

        result = await repository.get()

        assert isinstance(result, TGFSMetadata)
        assert len(result.dir.children) == 2
        assert "subdir1" in [child.name for child in result.dir.children]
        assert "subdir2" in [child.name for child in result.dir.children]

    @pytest.mark.asyncio
    async def test_push_and_get_integration(
        self, repository, store, sample_metadata, sample_pinned_message
    ):
        # Test the full cycle: push metadata and then retrieve it
        repository.metadata = sample_metadata

        store.upload.return_value = [SentFileMessage(message_id=999, size=2048)]
        store.get_pinned_message.return_value = MessageRespWithDocument(
            message_id=999,
            text="",
            document=Document(
                size=2048,
                id=888,
                access_hash=777,
                file_reference=b"test_ref",
                mime_type="application/json",
            ),
        )

        # Push the metadata
        await repository.push()

        # Mock the content retrieval for get operation
        store.download_file.return_value = _Chunks(
            json.dumps(sample_metadata.to_dict()).encode()
        )

        # Clear the in-memory metadata to test retrieval
        repository.metadata = None
        repository._message_id = None

        # Get the metadata back
        retrieved_metadata = await repository.get()

        # Verify the metadata was correctly retrieved
        assert isinstance(retrieved_metadata, TGFSMetadata)
        assert repository._message_id == 999

    @pytest.mark.asyncio
    async def test_multiple_pushes_update_existing_message(
        self, repository, store, sample_metadata
    ):
        # Set up initial state
        repository.metadata = sample_metadata
        repository._message_id = 555

        # First push (update)
        await repository.push()
        store.replace_document.assert_called_once_with(
            555, json.dumps(sample_metadata.to_dict()).encode(), "metadata.json"
        )

        # Reset mock
        store.reset_mock()

        # Modify metadata and push again
        sample_metadata.dir.create_dir("new_dir")
        await repository.push()

        # Should call replace_document again, not upload
        store.replace_document.assert_called_once_with(
            555, json.dumps(sample_metadata.to_dict()).encode(), "metadata.json"
        )
        store.upload.assert_not_called()
        store.pin_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_primary_store_without_a_mirror_group_does_not_mirror(
        self, repository, store, sample_metadata
    ):
        repository.metadata = sample_metadata
        repository._message_id = 1

        await repository.push()

        store.upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_mirror_store_entry_leaves_the_promotion_copy_alone(
        self, store, mocker, sample_metadata
    ):
        """An entry pinned to a mirror store is itself the redundant
        copy: the primary's promotion copy must skip that store."""
        from tgdcfs.core.mirror import MirrorGroup, MirrorStore

        primary = mocker.AsyncMock(spec=TelegramStore)
        primary.key = "tg:111"
        primary.caps = StoreCapabilities(
            max_part_bytes=1 << 30, max_text_chars=4096, supports_server_copy=True
        )
        store.key = "dc:222"
        mirror_group = MirrorGroup(
            primary=primary,
            stores=[MirrorStore(key="dc:222", store=store)],
            mode="auto",
            strict=False,
        )

        repository = PinnedMessageMetadataRepository(
            store, mirror_group=mirror_group
        )
        promotion = mocker.patch.object(
            mirror_group, "mirror_pinned", new_callable=mocker.AsyncMock
        )
        repository.metadata = sample_metadata
        repository._message_id = 1

        await repository.push()

        # The blob went to this entry's store, and the primary's
        # promotion copy never ran.
        store.replace_document.assert_called_once()
        promotion.assert_not_called()
