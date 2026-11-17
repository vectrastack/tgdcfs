import hashlib
import json
import logging
from typing import Optional, Sequence

from tgdcfs.core.model import TGFSMetadata
from tgdcfs.core.repository.interface import IMetaDataRepository
from tgdcfs.errors import MetadataNotInitialized, TechnicalError

from .github_repo import GithubRepoMetadataRepository

logger = logging.getLogger(__name__)


class RedundantMetadataRepository(IMetaDataRepository):
    """The same metadata in several stores at once.

    File data already had this: the file goes to the primary store and
    its mirrors, so one store failing never loses it. This is the same
    idea for the metadata: every entry in the file system's ``metadata``
    list is a full copy of the directory tree -- a pinned message in
    Telegram, one in Discord, a GitHub repo, a GitLab repo -- and this
    repository keeps them all in step.

    Reads fail over: the first entry that answers is the tree of record.
    Writes fan out: every entry gets every change. An entry that fails
    is marked stale and skipped until it answers again, while the others
    keep working; a stale entry that comes back is brought up to date
    from the tree of record, so a temporary outage never costs data.

    The GitHub repo plays a special role: it does not take pushes (its
    writes are the per-operation commits of its own directory objects)
    but is (re)built wholesale from the tree of record whenever it
    fell behind -- an outage it absorbed, or a tree another store
    loaded -- after which the tree is re-wrapped in its directory
    objects and commits to GitHub directly again.
    """

    def __init__(self, children: Sequence[tuple[str, IMetaDataRepository]]):
        super().__init__()
        if not children:
            raise TechnicalError(
                "A redundant metadata repository needs at least one entry"
            )
        self._children: list[tuple[str, IMetaDataRepository]] = list(children)
        self._active: Optional[IMetaDataRepository] = None
        # Repositories that failed and still have to be caught up.
        self._stale: set[int] = set()

    # -- reading ------------------------------------------------------------

    async def get(self) -> TGFSMetadata:
        """The first entry that answers is the tree of record.

        An entry that fails is marked stale and tried again on the next
        push or resync; the file system keeps running on the others.
        """
        last_error: Optional[Exception] = None
        for label, child in self._children:
            try:
                metadata = await child.get()
            except Exception as ex:
                self._stale.add(id(child))
                logger.warning(
                    f"Reading the metadata from {label} failed ({ex}); "
                    "trying the next store"
                )
                last_error = ex
                continue
            self._stale.discard(id(child))
            self._active = child
            self.metadata = metadata
            # The stats behaviour follows the entry of record.
            self.push_writes_stats = child.push_writes_stats
            # Every entry pushes the whole tree, so every one of them
            # gets the metadata of record (the active one already has it).
            for _, sibling in self._children:
                sibling.metadata = metadata
            return metadata
        raise TechnicalError(f"No metadata store could be read: {last_error}")

    # -- writing -------------------------------------------------------------

    async def push(self) -> None:
        """Fan every change out to all entries.

        Raises only when every push-based entry failed: as long as one
        store took the change, the metadata is safe and the failed
        stores are caught up when they answer again.
        """
        failures: list[tuple[str, Exception]] = []
        pushed = 0
        for label, child in self._children:
            # Every entry works on the whole tree of record -- also the
            # GitHub one, whose ``owns_tree`` answer has to be about
            # this tree and not an older one.
            child.metadata = self.metadata
            if isinstance(child, GithubRepoMetadataRepository):
                # Its writes are the per-operation commits of its own
                # directory objects; the catch-up is the wholesale
                # rebuild below.
                continue
            try:
                await child.push()
                self._stale.discard(id(child))
                pushed += 1
            except Exception as ex:
                self._stale.add(id(child))
                logger.warning(f"Writing the metadata to {label} failed: {ex}")
                failures.append((label, ex))

        await self._sync_github()

        # The GitHub repo holds the change as long as its own directory
        # objects carry the tree and no write was absorbed by an outage;
        # everything else had to be pushed.
        github = self._github_child()
        github_holds = (
            github is not None
            and not github.is_stale
            and github.owns_tree()
        )
        if not github_holds and pushed == 0 and failures:
            label, err = failures[0]
            raise TechnicalError(
                f"No metadata store took the change ({label}: {err})"
            )

    async def resync(self) -> None:
        """Catch up the entries that fell behind.

        Called on a timer: rewrites the stale push-based entries and
        rebuilds the GitHub repo when it needs it, without touching the
        healthy ones.
        """
        if not self._stale and not self._github_needs_sync():
            return
        for label, child in self._children:
            if isinstance(child, GithubRepoMetadataRepository):
                continue
            child.metadata = self.metadata
            if id(child) not in self._stale:
                continue
            try:
                await child.push()
                self._stale.discard(id(child))
            except Exception as ex:
                logger.warning(
                    f"Resyncing the metadata to {label} failed: {ex}"
                )
        await self._sync_github()

    # -- GitHub catch-up ------------------------------------------------------

    def _github_child(self) -> Optional[GithubRepoMetadataRepository]:
        for _, child in self._children:
            if isinstance(child, GithubRepoMetadataRepository):
                return child
        return None

    def _github_needs_sync(self) -> bool:
        github = self._github_child()
        if github is None:
            return False
        # Stale: a write was absorbed by an outage. Not its own form: the
        # tree came from another store. Either way it has to be rebuilt.
        return github.is_stale or not github.owns_tree()

    async def _sync_github(self) -> None:
        """Bring the GitHub repo in step with the tree of record.

        One commit replaces its whole content with the in-memory tree,
        after which the tree is re-wrapped in the repo's own directory
        objects so later changes commit to GitHub directly again. An
        unchanged tree (nothing missed) is left alone.
        """
        github = self._github_child()
        if github is None:
            return

        if not github.is_stale and github.owns_tree():
            # The tree is the repo's own (GithubDirectory) form and no
            # write was absorbed by an outage: every change already
            # committed to GitHub directly. Nothing to catch up.
            return

        github.metadata = self.metadata
        try:
            await github.restore()
        except Exception as ex:
            logger.warning(f"Rebuilding the metadata in GitHub failed: {ex}")
            return
        try:
            github.adopt()
        except Exception as ex:
            # The repo holds the tree; only the direct commits are off.
            logger.warning(f"Re-wrapping the tree for GitHub failed: {ex}")

    # -- stats (delegated to the entry of record) ---------------------------

    def _active_child(self) -> IMetaDataRepository:
        if self._active is None:
            raise MetadataNotInitialized()
        return self._active

    async def load_stats(self, directory) -> bool:
        return await self._active_child().load_stats(directory)

    def stats_stored(self, directory) -> bool:
        return self._active_child().stats_stored(directory)

    async def save_stats(self, directories, keep: bool) -> None:
        await self._active_child().save_stats(directories, keep)

    # -- introspection --------------------------------------------------------

    @property
    def entries(self) -> list[dict]:
        """The state of every entry, for the management API."""
        result = []
        for label, child in self._children:
            result.append(
                {
                    "label": label,
                    "active": child is self._active,
                    "stale": id(child) in self._stale,
                }
            )
        return result

    def _tree_hash(self) -> str:
        """A hash of the tree of record; unchanged means an unchanged
        tree, so a GitHub repo that already holds it is not rewritten."""
        if self.metadata is None:
            raise MetadataNotInitialized()
        data = json.dumps(
            self.metadata.to_dict(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(data.encode()).hexdigest()
