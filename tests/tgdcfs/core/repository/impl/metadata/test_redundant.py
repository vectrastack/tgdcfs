"""The redundant metadata repository: failover, fan-out and self-healing."""

import pytest

from tgdcfs.core.model import TGFSDirectory, TGFSMetadata
from tgdcfs.core.repository.impl.metadata.github_repo import (
    GithubRepoMetadataRepository,
)
from tgdcfs.core.repository.impl.metadata.redundant import (
    RedundantMetadataRepository,
)
from tgdcfs.core.repository.interface import IMetaDataRepository
from tgdcfs.errors import MetadataNotInitialized, TechnicalError


class FakeChild(IMetaDataRepository):
    """A push-based metadata store that can fail on demand."""

    def __init__(self, name: str):
        super().__init__()
        self.name = name
        self.fail_get = False
        self.fail_push = False
        self.get_calls = 0
        self.push_calls = 0
        self.tree = TGFSDirectory.root_dir()

    async def get(self) -> TGFSMetadata:
        self.get_calls += 1
        if self.fail_get:
            raise TechnicalError(f"{self.name} is unreachable")
        return TGFSMetadata(self.tree)

    async def push(self) -> None:
        self.push_calls += 1
        if self.fail_push:
            raise TechnicalError(f"{self.name} refused the write")

    async def load_stats(self, directory) -> bool:
        return True

    def stats_stored(self, directory) -> bool:
        return False


class FakeGithub(GithubRepoMetadataRepository):
    """The parts of the GitHub repository the wrapper talks to, with the
    network left out: a subclass so the wrapper recognises it."""

    def __init__(self):
        # The real constructor logs in to GitHub; this fake is the
        # repository without the network.
        IMetaDataRepository.__init__(self)
        self.restore_calls = 0
        self.fail_restore = False
        self.adopt_calls = 0
        self.fail_adopt = False
        self.stale = False
        self.tree = TGFSDirectory.root_dir()
        self.wrapped = False

    @property
    def is_stale(self) -> bool:
        return self.stale

    def mark_stale(self) -> None:
        self.stale = True

    def owns_tree(self) -> bool:
        return self.wrapped

    async def restore(self) -> None:
        self.restore_calls += 1
        if self.fail_restore:
            raise TechnicalError("GitHub is unreachable")
        self.stale = False

    def adopt(self) -> bool:
        self.adopt_calls += 1
        if self.fail_adopt:
            raise TechnicalError("cannot wrap")
        self.wrapped = True
        return True

    async def get(self) -> TGFSMetadata:
        self.metadata = TGFSMetadata(self.tree)
        return self.metadata


def _children(*repos):
    return [(repo.name, repo) for repo in repos]


def _root_with(name: str) -> TGFSMetadata:
    root = TGFSDirectory.root_dir()
    root.create_dir(name)
    return TGFSMetadata(root)


class TestRedundantMetadataRepository:
    def test_it_needs_at_least_one_entry(self):
        with pytest.raises(TechnicalError, match="at least one"):
            RedundantMetadataRepository([])

    @pytest.mark.asyncio
    async def test_the_first_entry_that_answers_is_the_tree_of_record(self):
        first, second = FakeChild("pinned-tg"), FakeChild("pinned-dc")
        wrapper = RedundantMetadataRepository(_children(first, second))

        metadata = await wrapper.get()

        assert first.get_calls == 1
        assert second.get_calls == 0
        assert wrapper.metadata is metadata
        assert wrapper._active is first

    @pytest.mark.asyncio
    async def test_a_failing_entry_is_skipped_and_marked_stale(self):
        first, second = FakeChild("pinned-tg"), FakeChild("pinned-dc")
        first.fail_get = True
        wrapper = RedundantMetadataRepository(_children(first, second))

        metadata = await wrapper.get()

        assert second.get_calls == 1
        assert wrapper._active is second
        assert [e["stale"] for e in wrapper.entries] == [True, False]

    @pytest.mark.asyncio
    async def test_every_entry_failing_raises(self):
        first, second = FakeChild("pinned-tg"), FakeChild("pinned-dc")
        first.fail_get = True
        second.fail_get = True
        wrapper = RedundantMetadataRepository(_children(first, second))

        with pytest.raises(TechnicalError, match="No metadata store"):
            await wrapper.get()

    @pytest.mark.asyncio
    async def test_push_fans_out_to_all_entries(self):
        first, second = FakeChild("pinned-tg"), FakeChild("pinned-dc")
        wrapper = RedundantMetadataRepository(_children(first, second))
        await wrapper.get()
        wrapper.metadata = _root_with("photos")

        await wrapper.push()

        assert first.push_calls == 1
        assert second.push_calls == 1

    @pytest.mark.asyncio
    async def test_one_failing_push_is_not_an_error(self):
        first, second = FakeChild("pinned-tg"), FakeChild("pinned-dc")
        first.fail_push = True
        wrapper = RedundantMetadataRepository(_children(first, second))
        await wrapper.get()

        await wrapper.push()

        assert first.push_calls == 1
        assert second.push_calls == 1
        assert [e["stale"] for e in wrapper.entries] == [True, False]

    @pytest.mark.asyncio
    async def test_all_pushes_failing_raises(self):
        first, second = FakeChild("pinned-tg"), FakeChild("pinned-dc")
        first.fail_push = True
        second.fail_push = True
        wrapper = RedundantMetadataRepository(_children(first, second))
        await wrapper.get()

        with pytest.raises(TechnicalError, match="took the change"):
            await wrapper.push()

    @pytest.mark.asyncio
    async def test_resync_retries_only_the_stale_entries(self):
        first, second = FakeChild("pinned-tg"), FakeChild("pinned-dc")
        first.fail_push = True
        wrapper = RedundantMetadataRepository(_children(first, second))
        await wrapper.get()
        await wrapper.push()
        first.push_calls = 0
        second.push_calls = 0

        first.fail_push = False
        await wrapper.resync()

        assert first.push_calls == 1
        assert second.push_calls == 0
        assert [e["stale"] for e in wrapper.entries] == [False, False]

    @pytest.mark.asyncio
    async def test_a_stale_github_is_restored_and_adopted(self):
        github = FakeGithub()
        first = FakeChild("pinned-tg")
        wrapper = RedundantMetadataRepository(
            [("pinned-tg", first), ("github", github)]
        )
        await wrapper.get()
        wrapper.metadata = _root_with("photos")
        github.stale = True

        await wrapper.push()

        assert github.restore_calls == 1
        assert github.adopt_calls == 1

    @pytest.mark.asyncio
    async def test_a_github_loaded_tree_is_not_restored_on_push(self):
        github = FakeGithub()
        github.wrapped = True
        wrapper = RedundantMetadataRepository(
            [("github", github), ("pinned-dc", FakeChild("pinned-dc"))]
        )
        # The tree of record came from GitHub itself: its own form, and
        # every later change commits to it directly.
        await wrapper.get()
        github.restore_calls = 0
        wrapper.metadata = _root_with("photos")

        await wrapper.push()

        assert github.restore_calls == 0

    @pytest.mark.asyncio
    async def test_a_tree_from_another_store_is_copied_into_github(self):
        github = FakeGithub()
        first = FakeChild("pinned-tg")
        wrapper = RedundantMetadataRepository(
            [("pinned-tg", first), ("github", github)]
        )
        await wrapper.get()

        await wrapper.push()

        assert github.restore_calls == 1
        assert github.adopt_calls == 1

    @pytest.mark.asyncio
    async def test_a_restored_github_is_not_rewritten_on_later_pushes(self):
        github = FakeGithub()
        first = FakeChild("pinned-tg")
        wrapper = RedundantMetadataRepository(
            [("pinned-tg", first), ("github", github)]
        )
        await wrapper.get()
        await wrapper.push()
        assert github.restore_calls == 1

        # After the rebuild the tree is the repo's own form, and the
        # changes commit to it directly: no further wholesale rewrites.
        await wrapper.push()
        assert github.restore_calls == 1

        assert wrapper.metadata is not None
        wrapper.metadata.dir.create_dir("new")
        await wrapper.push()
        assert github.restore_calls == 1

    @pytest.mark.asyncio
    async def test_a_fresh_outage_brings_the_rebuild_back(self):
        github = FakeGithub()
        first = FakeChild("pinned-tg")
        wrapper = RedundantMetadataRepository(
            [("pinned-tg", first), ("github", github)]
        )
        await wrapper.get()
        await wrapper.push()
        assert github.restore_calls == 1

        # A new outage: writes are absorbed, the repo falls behind.
        github.stale = True
        assert wrapper.metadata is not None
        wrapper.metadata.dir.create_dir("during-the-outage")
        await wrapper.push()
        assert github.restore_calls == 2

    @pytest.mark.asyncio
    async def test_a_failing_restore_is_logged_not_raised(self):
        github = FakeGithub()
        github.fail_restore = True
        first = FakeChild("pinned-tg")
        wrapper = RedundantMetadataRepository(
            [("pinned-tg", first), ("github", github)]
        )
        await wrapper.get()

        # The push still succeeds: the pinned store took the change.
        await wrapper.push()

        assert github.restore_calls == 1

    @pytest.mark.asyncio
    async def test_stats_delegate_to_the_active_child(self):
        first, second = FakeChild("pinned-tg"), FakeChild("pinned-dc")
        first.fail_get = True
        wrapper = RedundantMetadataRepository(_children(first, second))
        await wrapper.get()

        directory = wrapper.root()
        assert await wrapper.load_stats(directory) is True
        assert wrapper.stats_stored(directory) is False

    def test_entries_report_the_health_of_every_store(self):
        first, second = FakeChild("pinned-tg"), FakeChild("pinned-dc")
        wrapper = RedundantMetadataRepository(_children(first, second))

        assert wrapper.entries == [
            {"label": "pinned-tg", "active": False, "stale": False},
            {"label": "pinned-dc", "active": False, "stale": False},
        ]

    def test_stats_before_init_raise(self):
        wrapper = RedundantMetadataRepository(_children(FakeChild("x")))

        with pytest.raises(MetadataNotInitialized):
            wrapper._tree_hash()
