"""The ``metadata`` block as a redundancy list."""

import pytest

from tgdcfs.config import (
    Config,
    FilesystemConfig,
    GitlabRepoConfig,
    MetadataType,
)

TELEGRAM = {"api_id": 1, "api_hash": "h", "bot": {"token": "t", "session_file": "b"}}
APP = {
    "users": {},
    "jwt": {"secret": "s", "algorithm": "HS256", "life": 3600},
    "server": {"host": "0.0.0.0", "port": 8080},
}


def _config(metadata, stores=None):
    """A whole config with one file system whose ``metadata`` is the
    argument, so the validation between stores and entries runs too."""
    return {
        "backends": {"telegram": dict(TELEGRAM)},
        "stores": stores
        or {
            "tg-main": {"backend": "telegram", "channel": "-1001"},
            "tg-spare": {"backend": "telegram", "channel": "-1002"},
        },
        "filesystems": {
            "media": {
                "primary": "tg-main",
                "mirrors": ["tg-spare"],
                "metadata": metadata,
            }
        },
        "tgdcfs": dict(APP),
    }


GITHUB_ENTRY = {
    "type": "github_repo",
    "github_repo": {
        "repo": "o/r",
        "commit": "master",
        "access_token": "t",
    },
}

GITLAB_ENTRY = {
    "type": "gitlab_repo",
    "gitlab_repo": {
        "repo": "group/project",
        "branch": "main",
        "access_token": "t",
    },
}


class TestMetadataList:
    def test_no_metadata_defaults_to_a_pinned_message_on_the_primary(self):
        fs = FilesystemConfig.from_dict("media", {"primary": "tg-main"})

        assert len(fs.metadata) == 1
        assert fs.metadata[0].type == MetadataType.PINNED_MESSAGE
        assert fs.metadata[0].store is None

    def test_a_single_mapping_stays_a_one_entry_list(self):
        fs = FilesystemConfig.from_dict(
            "media", {"primary": "tg-main", "metadata": {"type": "github_repo", **GITHUB_ENTRY}}
        )

        assert len(fs.metadata) == 1
        assert fs.metadata[0].type == MetadataType.GITHUB_REPO

    def test_a_list_configures_the_redundancy_set(self):
        fs = FilesystemConfig.from_dict(
            "media",
            {
                "primary": "tg-main",
                "mirrors": ["dc-main"],
                "metadata": [
                    {"type": "pinned_message", "store": "tg-main"},
                    {"type": "pinned_message", "store": "tg-spare"},
                ],
            },
        )

        assert [e.type for e in fs.metadata] == [
            MetadataType.PINNED_MESSAGE,
            MetadataType.PINNED_MESSAGE,
        ]
        assert [e.store for e in fs.metadata] == ["tg-main", "tg-spare"]

    def test_gitlab_is_an_option(self):
        fs = FilesystemConfig.from_dict(
            "media", {"primary": "tg-main", "metadata": GITLAB_ENTRY}
        )

        assert fs.metadata[0].type == MetadataType.GITLAB_REPO
        assert fs.metadata[0].gitlab_repo is not None
        assert fs.metadata[0].gitlab_repo.repo == "group/project"
        assert fs.metadata[0].gitlab_repo.branch == "main"
        assert fs.metadata[0].gitlab_repo.url == "https://gitlab.com"

    def test_gitlab_config_accepts_a_private_installation(self):
        cfg = GitlabRepoConfig.from_dict(
            {
                "repo": "g/p",
                "access_token": "t",
                "url": "https://git.example.com/",
            }
        )

        assert cfg.url == "https://git.example.com"

    def test_every_kind_at_once(self):
        fs = FilesystemConfig.from_dict(
            "media",
            {
                "primary": "tg-main",
                "mirrors": ["dc-main"],
                "metadata": [
                    {"type": "pinned_message", "store": "tg-main"},
                    {"type": "pinned_message", "store": "tg-spare"},
                    GITHUB_ENTRY,
                    GITLAB_ENTRY,
                ],
            },
        )

        assert [e.type for e in fs.metadata] == [
            MetadataType.PINNED_MESSAGE,
            MetadataType.PINNED_MESSAGE,
            MetadataType.GITHUB_REPO,
            MetadataType.GITLAB_REPO,
        ]


class TestMetadataValidation:
    def test_the_same_pinned_store_cannot_be_listed_twice(self):
        with pytest.raises(ValueError, match="duplicates"):
            Config.from_dict(
                _config(
                    [
                        {"type": "pinned_message", "store": "tg-main"},
                        {"type": "pinned_message", "store": "tg-main"},
                    ]
                )
            )

    def test_two_pinned_entries_need_two_distinct_stores(self):
        config = Config.from_dict(
            _config(
                [
                    {"type": "pinned_message", "store": "tg-main"},
                    {"type": "pinned_message", "store": "tg-spare"},
                ]
            )
        )

        fs = config.filesystems["media"]
        assert len(fs.metadata) == 2

    def test_a_pinned_message_needs_a_configured_store(self):
        with pytest.raises(ValueError, match="which is not configured"):
            Config.from_dict(
                _config([{"type": "pinned_message", "store": "nope"}])
            )

    def test_a_pinned_message_needs_a_store_of_this_file_system(self):
        with pytest.raises(ValueError, match="neither this file system's"):
            Config.from_dict(
                _config(
                    [{"type": "pinned_message", "store": "elsewhere"}],
                    stores={
                        "tg-main": {"backend": "telegram", "channel": "-1001"},
                        "elsewhere": {"backend": "telegram", "channel": "-1009"},
                    },
                )
            )

    def test_github_may_appear_only_once(self):
        # The duplicate check fires before the structural one; either way,
        # the same kind twice is refused.
        with pytest.raises(ValueError, match="duplicates"):
            Config.from_dict(
                _config([GITHUB_ENTRY, GITHUB_ENTRY])
            )

    def test_gitlab_may_appear_only_once(self):
        with pytest.raises(ValueError, match="duplicates"):
            Config.from_dict(_config([GITLAB_ENTRY, GITLAB_ENTRY]))

    def test_github_anywhere_in_the_list_is_valid(self):
        config = Config.from_dict(
            _config(
                [
                    {"type": "pinned_message", "store": "tg-main"},
                    GITHUB_ENTRY,
                ]
            )
        )

        fs = config.filesystems["media"]
        assert [e.type for e in fs.metadata] == [
            MetadataType.PINNED_MESSAGE,
            MetadataType.GITHUB_REPO,
        ]
