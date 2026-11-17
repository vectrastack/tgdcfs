"""The client wiring of the metadata redundancy set: how config entries
become repositories, which store each pinned entry lands in, and what
the redundant wrapper does when one of them fails."""

import pytest

from tests.fakes.store import FakeStore
from tgdcfs.config import Config, GithubRepoConfig, MetadataConfig, MetadataType
from tgdcfs.core.client import Client
from tgdcfs.core.repository.impl.metadata.github_repo import (
    GithubRepoMetadataRepository,
)
from tgdcfs.core.repository.impl.metadata.gitlab_repo import (
    GitlabRepoMetadataRepository,
)
from tgdcfs.core.repository.impl.metadata.pinned_message import (
    PinnedMessageMetadataRepository,
)
from tgdcfs.core.repository.impl.metadata.redundant import (
    RedundantMetadataRepository,
)
from tgdcfs.core.repository.interface import IMetaDataRepository

TELEGRAM = {"api_id": 1, "api_hash": "h", "bot": {"token": "t", "session_file": "b"}}
APP = {
    "users": {},
    "jwt": {"secret": "s", "algorithm": "HS256", "life": 3600},
    "server": {"host": "0.0.0.0", "port": 8080},
}


def _config():
    return Config.from_dict(
        {
            "backends": {"telegram": dict(TELEGRAM)},
            "stores": {
                "tg-primary": {"backend": "telegram", "channel": "-1001"},
                "tg-mirror": {"backend": "telegram", "channel": "-1002"},
            },
            "filesystems": {
                "media": {
                    "primary": "tg-primary",
                    "mirrors": ["tg-mirror"],
                    "metadata": [
                        {"type": "pinned_message", "store": "tg-primary"},
                        {"type": "pinned_message", "store": "tg-mirror"},
                    ],
                }
            },
            "tgdcfs": dict(APP),
        }
    )


class FakeGithubRepo(GithubRepoMetadataRepository):
    """The GitHub repository without the network, recognisable as one."""

    def __init__(self, config, name_key=None, tolerate_write_failures=False):
        IMetaDataRepository.__init__(self)
        self.got_tolerate_write_failures = tolerate_write_failures
        self.got_name_key = name_key

    async def get(self):
        raise RuntimeError("github is down in this test")

    async def push(self) -> None:
        pass

    @property
    def is_stale(self) -> bool:
        return False

    def mark_stale(self) -> None:
        pass

    def owns_tree(self) -> bool:
        return False

    async def restore(self) -> None:
        pass

    def adopt(self) -> bool:
        return False


class FakeGitlabRepo(GitlabRepoMetadataRepository):
    """The GitLab repository without the network."""

    def __init__(self, config, crypto=None):
        IMetaDataRepository.__init__(self)

    async def get(self):
        raise RuntimeError("gitlab is down in this test")

    async def push(self) -> None:
        pass

    def close(self) -> None:
        pass


# One pair of stores for the whole test: the factory hands out the very
# instances the file system keeps, so their state can be inspected.
_STORES = {
    "tg-primary": FakeStore(key="tg:-1001"),
    "tg-mirror": FakeStore(key="tg:-1002"),
}


async def _fake_store_factory(cfg):
    return _STORES[cfg.name]


def _repo(client: Client) -> RedundantMetadataRepository:
    """The file system's redundant metadata repository."""
    assert client.metadata_api is not None
    repo = client.metadata_api.metadata_repo
    assert isinstance(repo, RedundantMetadataRepository)
    return repo


class TestClientMetadataWiring:
    @pytest.mark.asyncio
    async def test_two_pinned_entries_become_a_redundant_set(self, mocker):
        mocker.patch(
            "tgdcfs.core.repository.impl.metadata.github_repo."
            "GithubRepoMetadataRepository",
            FakeGithubRepo,
        )
        config = _config()

        client = await Client.create(
            config.filesystems["media"], config, _fake_store_factory
        )

        repo = _repo(client)
        children = list(repo._children)  # noqa: SLF001
        assert [label for label, _ in children] == [
            "pinned_message:tg-primary",
            "pinned_message:tg-mirror",
        ]
        assert client.has_redundant_metadata

        # Each pinned entry got its own store, and the primary's
        # promotion copy skips the mirror with its own entry.
        primary_repo = children[0][1]
        mirror_repo = children[1][1]
        assert isinstance(primary_repo, PinnedMessageMetadataRepository)
        assert isinstance(mirror_repo, PinnedMessageMetadataRepository)
        assert primary_repo._store.key == "tg:-1001"  # noqa: SLF001
        assert mirror_repo._store.key == "tg:-1002"  # noqa: SLF001
        # The primary's promotion copy skips the mirror with its own
        # entry; the mirror's own set is irrelevant (it is not the
        # primary, so it never promotes).
        assert primary_repo._skip_mirror_keys == frozenset({"tg:-1002"})  # noqa: SLF001

        # The tree was loaded from the first entry that answered.
        assert repo._active is primary_repo  # noqa: SLF001
        assert client.metadata_api is not None
        assert client.metadata_api.health() == [
            {"label": "pinned_message:tg-primary", "active": True, "stale": False},
            {"label": "pinned_message:tg-mirror", "active": False, "stale": False},
        ]

        # A push fans out: the mirror store gets its own pinned
        # document even though the tree came from the primary.
        assert client.metadata_api is not None
        await client.metadata_api.push()
        assert _STORES["tg-mirror"].pinned is not None

    @pytest.mark.asyncio
    async def test_github_takes_write_failures_when_redundant(self, mocker):
        """In a redundancy set a GitHub outage is absorbed, not fatal."""
        mocker.patch(
            "tgdcfs.core.repository.impl.metadata.github_repo."
            "GithubRepoMetadataRepository",
            FakeGithubRepo,
        )
        config = _config()
        fs = config.filesystems["media"]
        fs.metadata.insert(
            0,
            MetadataConfig(
                name="github",
                type=MetadataType.GITHUB_REPO,
                github_repo=GithubRepoConfig(
                    repo="o/r", commit="master", access_token="t"
                ),
            ),
        )

        client = await Client.create(fs, config, _fake_store_factory)

        repo = _repo(client)
        github_child = [
            c for _, c in repo._children if isinstance(c, FakeGithubRepo)  # noqa: SLF001
        ][0]
        assert github_child.got_tolerate_write_failures is True
        # A GitHub repo that is down at boot is stale, not fatal: the
        # pinned entries carry the file system.
        assert client.metadata_api is not None
        entries = client.metadata_api.health()
        # GitHub is first but down: stale, and the pinned entry behind
        # it is the active tree of record.
        assert entries[0] == {
            "label": "github_repo:o/r",
            "active": False,
            "stale": True,
        }
        assert entries[1]["active"] is True

    @pytest.mark.asyncio
    async def test_a_single_entry_stays_a_plain_repository(self, mocker):
        config = _config()
        fs = config.filesystems["media"]
        fs.metadata = fs.metadata[:1]

        client = await Client.create(
            fs, config, _fake_store_factory
        )

        assert client.metadata_api is not None
        repo = client.metadata_api.metadata_repo
        assert isinstance(repo, PinnedMessageMetadataRepository)
        assert not client.has_redundant_metadata
