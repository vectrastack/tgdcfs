"""The GitLab metadata repository, against a fake GitLab REST API."""

import base64
import json

import pytest

from tgdcfs.config import GitlabRepoConfig
from tgdcfs.core.model import TGFSDirectory, TGFSMetadata
from tgdcfs.core.repository.impl.metadata.gitlab_repo import (
    GitlabRepoMetadataRepository,
)
from tgdcfs.crypto.buffer import MetadataCrypto


class FakeResponse:
    def __init__(self, status_code=200, data=None, content=b""):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.content = content
        self.text = content.decode(errors="replace")

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeGitlab:
    """The slice of the GitLab REST v4 API the repository uses."""

    def __init__(self, content=b""):
        self.content = content
        self.put_calls = []
        self.post_calls = []
        self.put_status = 200
        self.fail_writes = False

    def get(self, url, **kwargs):
        if url.endswith("/raw"):
            if not self.content:
                return FakeResponse(status_code=404)
            return FakeResponse(content=self.content)
        return FakeResponse(data=[{"file_path": "metadata.json"}])

    def put(self, url, **kwargs):
        self.put_calls.append((url, kwargs))
        if self.fail_writes:
            return FakeResponse(status_code=500)
        return FakeResponse(status_code=self.put_status)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if self.fail_writes:
            return FakeResponse(status_code=500)
        return FakeResponse(status_code=201)


@pytest.fixture
def config():
    return GitlabRepoConfig(
        repo="group/project", branch="main", access_token="t"
    )


@pytest.fixture
def sample_metadata():
    root = TGFSDirectory.root_dir()
    root.create_dir("photos")
    return TGFSMetadata(root)


def _encoded(metadata: TGFSMetadata) -> bytes:
    return json.dumps(metadata.to_dict()).encode()


class TestGitlabRepoMetadataRepository:
    def test_the_file_lives_on_the_branch(self, config):
        repo = GitlabRepoMetadataRepository(config)

        assert repo._file_url() == (
            "https://gitlab.com/api/v4/projects/"
            "group%2Fproject/repository/files/metadata.json"
        )

    @pytest.mark.asyncio
    async def test_get_reads_the_file(self, config, sample_metadata, mocker):
        api = FakeGitlab(content=_encoded(sample_metadata))
        repo = GitlabRepoMetadataRepository(config)
        mocker.patch.object(repo, "_session", api)

        result = await repo.get()

        assert isinstance(result, TGFSMetadata)
        assert [d.name for d in result.dir.children] == ["photos"]

    @pytest.mark.asyncio
    async def test_get_decrypts_when_crypto_is_set(
        self, config, sample_metadata, mocker
    ):
        crypto = MetadataCrypto(b"k" * 32, 64)
        api = FakeGitlab(content=crypto.encrypt(_encoded(sample_metadata)))
        repo = GitlabRepoMetadataRepository(config, crypto=crypto)
        mocker.patch.object(repo, "_session", api)

        result = await repo.get()

        assert [d.name for d in result.dir.children] == ["photos"]

    @pytest.mark.asyncio
    async def test_a_project_without_a_file_starts_empty(
        self, config, mocker
    ):
        api = FakeGitlab(content=b"")
        repo = GitlabRepoMetadataRepository(config)
        mocker.patch.object(repo, "_session", api)

        result = await repo.get()

        assert result.dir.children == []
        assert repo.metadata is result

    @pytest.mark.asyncio
    async def test_push_updates_an_existing_file(
        self, config, sample_metadata, mocker
    ):
        api = FakeGitlab()
        repo = GitlabRepoMetadataRepository(config)
        mocker.patch.object(repo, "_session", api)
        repo.metadata = sample_metadata

        await repo.push()

        assert len(api.put_calls) == 1
        url, kwargs = api.put_calls[0]
        assert "repository/files/metadata.json" in url
        body = kwargs["json"]
        assert body["branch"] == "main"
        assert json.loads(base64.b64decode(body["content"])) == (
            sample_metadata.to_dict()
        )
        assert api.post_calls == []

    @pytest.mark.asyncio
    async def test_push_creates_the_file_when_the_update_is_refused(
        self, config, sample_metadata, mocker
    ):
        api = FakeGitlab()
        api.put_status = 404
        repo = GitlabRepoMetadataRepository(config)
        mocker.patch.object(repo, "_session", api)
        repo.metadata = sample_metadata

        await repo.push()

        assert api.post_calls, "a refused update must fall back to a create"

    @pytest.mark.asyncio
    async def test_a_failed_write_raises(self, config, sample_metadata, mocker):
        api = FakeGitlab()
        api.fail_writes = True
        repo = GitlabRepoMetadataRepository(config)
        mocker.patch.object(repo, "_session", api)
        repo.metadata = sample_metadata

        with pytest.raises(Exception, match="GitLab refused"):
            await repo.push()
