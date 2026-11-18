"""The GitLab tree-mirror store: the facade over GitLab REST v4 and the
``GithubRepoMetadataRepository`` engine running on top of it.

The fake below models the parts of the GitLab API the facade uses --
branches, commits-by-actions, tree listings with pagination, blobs, the
files API and compare -- closely enough that the store's real code paths
(incremental writes, the diff sync, the document migration) run against
it unchanged.
"""

import base64
import hashlib
import json
from typing import Dict, List, Optional
from unittest.mock import Mock, patch
from urllib.parse import unquote

import pytest
import requests
from github import GithubException, InputGitTreeElement

from tgdcfs.config import GitlabRepoConfig
from tgdcfs.core.model import TGFSDirectory, TGFSMetadata
from tgdcfs.core.repository.impl.metadata.github_repo.gh_directory import (
    GithubDirectory,
)
from tgdcfs.core.repository.impl.metadata.gitlab_tree import (
    GitlabRepository,
    GitlabTreeRepoMetadataRepository,
)
from tgdcfs.core.repository.interface import IMetaDataRepository
from tgdcfs.crypto.metadata_blob import (
    decrypt_metadata_blob,
    derive_metadata_blob_key,
    encrypt_metadata_blob,
)
from tgdcfs.errors import NoStoredMetadata, TechnicalError

PER_PAGE = 100


def blob_sha(content: str) -> str:
    data = content.encode()
    # Git's object id, so tree listings and blob reads agree with the
    # shas the facade compares.
    return hashlib.sha1(
        b"blob %d\0" % len(data) + data, usedforsecurity=False
    ).hexdigest()


class FakeResponse:
    def __init__(self, status: int, body, headers: Optional[dict] = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.content = self.text.encode()

    def json(self):
        return json.loads(self.text) if self.text else {}


class FakeGitlab:
    """An in-memory GitLab project: branches as file dicts, commits as
    (sha, message, date, changed paths, resulting files)."""

    def __init__(self, seed: Optional[Dict[str, str]] = None, empty_project=False):
        # A project only has a default branch once it holds a commit; an
        # empty project reports none.
        self.default_branch: Optional[str] = (
            "master" if seed and not empty_project else None
        )
        self.project = {
            "created_at": "2024-01-01T00:00:00Z",
            "last_activity_at": "2024-06-01T12:00:00Z",
            "default_branch": self.default_branch,
        }
        self.commits: Dict[str, dict] = {}
        self.blobs: Dict[str, str] = {}
        self.branch_files: Dict[str, Dict[str, str]] = {}
        self.branch_heads: Dict[str, str] = {}
        self.requests: List[tuple] = []
        if seed and self.default_branch is not None:
            self.commit(
                self.default_branch,
                "seed",
                [
                    {
                        "action": "create",
                        "file_path": p,
                        "content": c,
                        "encoding": "text",
                    }
                    for p, c in seed.items()
                ],
            )

    # -- git model ------------------------------------------------------------

    def commit(self, branch: str, message: str, actions: List[dict]) -> str:
        files = dict(self.branch_files.get(branch, {}))
        changed = []
        for action in actions:
            path = action["file_path"]
            kind = action["action"]
            if kind == "delete":
                if path not in files:
                    raise _Conflict(f"A file with this name does not exist")
                del files[path]
            else:
                if kind == "create" and path in files:
                    raise _Conflict(
                        f"A file with the same name already exists in {branch}"
                    )
                if kind == "update" and path not in files:
                    raise _Conflict(f"A file with this name does not exist")
                content = action["content"]
                if action.get("encoding") == "base64":
                    content = base64.b64decode(content).decode()
                sha = blob_sha(content)
                self.blobs[sha] = content
                files[path] = sha
            changed.append(path)
        sha = hashlib.sha1(
            (message + str(sorted(files.items())) + str(len(self.commits))).encode()
        ).hexdigest()
        self.commits[sha] = {
            "id": sha,
            "message": message,
            "committed_date": f"2024-06-{len(self.commits) % 28 + 1:02d}" "T00:00:00Z",
            "changed": changed,
            "files": files,
        }
        self.branch_files[branch] = files
        self.branch_heads[branch] = sha
        if self.default_branch is None:
            # The first commit determines the default branch, like GitLab.
            self.default_branch = branch
            self.project["default_branch"] = branch
        return sha

    def create_branch(self, branch: str, start: str) -> None:
        start_files = self._files_for(start)
        self.branch_files[branch] = dict(start_files)
        self.branch_heads[branch] = self._resolve_ref(start)

    def _resolve_ref(self, ref: str) -> str:
        if ref in self.branch_heads:
            return self.branch_heads[ref]
        if ref in self.commits:
            return ref
        raise _NotFound()

    def _files_for(self, ref: str) -> Dict[str, str]:
        if ref in self.branch_files:
            return self.branch_files[ref]
        if ref in self.commits:
            return self.commits[ref]["files"]
        raise _NotFound()

    def file_content(self, branch: str, path: str) -> Optional[str]:
        files = self.branch_files.get(branch, {})
        sha = files.get(path)
        return None if sha is None else self.blobs[sha]

    # -- HTTP routing -----------------------------------------------------------

    def handle(self, method: str, url: str, params: dict, body) -> FakeResponse:
        self.requests.append((method, url, dict(params or {}), body))
        try:
            status, resp = self._route(method, url, params or {}, body)
        except _NotFound:
            status, resp = 404, {"message": "404 Not Found"}
        except _Conflict as ex:
            status, resp = 400, {"message": str(ex)}
        headers = {}
        if isinstance(resp, list):
            page = int((params or {}).get("page", 1))
            more = page * PER_PAGE < len(resp)
            if more:
                headers["X-Next-Page"] = str(page + 1)
            resp = resp[(page - 1) * PER_PAGE : page * PER_PAGE]
        return FakeResponse(status, resp, headers)

    def _route(self, method, url, params, body):
        rest = url.split("/api/v4/projects/", 1)[1]
        project, _, rest = rest.partition("/")
        # The facade quotes paths (and project ids) with ``safe=""``; the
        # slashes of a file path come back encoded too.
        rest = unquote(rest)
        if rest == "":
            return 200, self.project
        if rest == "repository/branches":
            self._resolve_ref(body["ref"])
            if body["branch"] in self.branch_heads:
                raise _Conflict("Branch already exists")
            self.create_branch(body["branch"], body["ref"])
            return 200, {"name": body["branch"]}
        if rest.startswith("repository/branches/"):
            branch = rest[len("repository/branches/") :]
            return 200, {"commit": {"id": self._resolve_ref(branch)}}
        if rest == "repository/tree":
            return 200, self._tree(params)
        if rest == "repository/commits":
            if method == "POST":
                branch = body["branch"]
                if branch not in self.branch_heads:
                    raise _Conflict("Invalid branch name")
                sha = self.commit(branch, body["commit_message"], body["actions"])
                return 200, self.commits[sha]
            return 200, self._commit_history(params)
        if rest.startswith("repository/commits/"):
            sha = rest[len("repository/commits/") :]
            return 200, self.commits[sha]
        if rest.startswith("repository/blobs/"):
            sha = rest[len("repository/blobs/") :]
            return 200, {
                "content": base64.b64encode(self.blobs[sha].encode()).decode(),
                "encoding": "base64",
            }
        if rest == "repository/compare":
            return 200, self._compare(params)
        if rest.startswith("repository/files/"):
            path = rest[len("repository/files/") :]
            return self._files_api(method, path, params, body)
        raise _NotFound()

    def _tree(self, params) -> list:
        files = self._files_for(params["ref"])
        prefix = params.get("path", "").strip("/")
        recursive = params.get("recursive") == "true"
        entries = []
        seen_dirs = set()
        for path in sorted(files):
            if prefix and not (path == prefix or path.startswith(prefix + "/")):
                continue
            rel = path[len(prefix) + 1 :] if prefix else path
            if recursive or "/" not in rel:
                entries.append(
                    {
                        "id": files[path],
                        "name": rel.rsplit("/", 1)[-1],
                        "type": "blob",
                        "path": path,
                        "mode": "100644",
                    }
                )
            else:
                top = rel.split("/", 1)[0]
                if top not in seen_dirs:
                    seen_dirs.add(top)
                    entries.append(
                        {
                            "id": f"tree-{prefix}-{top}",
                            "name": top,
                            "type": "tree",
                            "path": f"{prefix}/{top}" if prefix else top,
                            "mode": "040000",
                        }
                    )
        return entries

    def _commit_history(self, params) -> list:
        head = self._resolve_ref(params["ref_name"])
        # Walk the branch back through the commit graph; the fake keeps one
        # linear history per branch through the parents it never records --
        # the ids are ordered by creation, which is enough for the facade.
        ordered = [
            self.commits[sha] for sha in self.commits if sha in self._reachable(head)
        ]
        ordered.reverse()
        path = params.get("path")
        if path:
            ordered = [
                c
                for c in ordered
                if any(ch == path or ch.startswith(path + "/") for ch in c["changed"])
            ]
        return ordered

    def _reachable(self, head: str) -> set:
        # The fake appends commits in creation order; a branch head at index
        # i means every commit up to i on the same file lineage. Approximate
        # reachability by commit insertion order up to the head.
        shas = list(self.commits)
        return set(shas[: shas.index(head) + 1])

    def _compare(self, params) -> dict:
        old, new = self._files_for(params["from"]), self._files_for(params["to"])
        diffs = []
        for path in sorted(set(old) | set(new)):
            if old.get(path) == new.get(path):
                continue
            diffs.append(
                {
                    "old_path": path,
                    "new_path": path,
                    "renamed_file": False,
                }
            )
        return {"diffs": diffs}

    def _files_api(self, method, path, params, body):
        branch = (body or {}).get("branch") or params.get("ref")
        if method == "GET":
            sha = self.branch_files.get(branch, {}).get(path)
            if sha is None:
                return 404, {"message": "404 File Not Found"}
            return 200, {
                "content": base64.b64encode(self.blobs[sha].encode()).decode(),
                "blob_id": sha,
            }
        if method == "POST":
            if branch not in self.branch_heads:
                if self.default_branch is None and not self.commits:
                    # An empty project: the first file initializes the
                    # repository and the branch.
                    self.default_branch = branch
                    self.project["default_branch"] = branch
                    sha = self.commit(
                        branch,
                        body["commit_message"],
                        [
                            {
                                "action": "create",
                                "file_path": path,
                                "content": body["content"],
                                "encoding": body.get("encoding", "text"),
                            }
                        ],
                    )
                    return 200, self.commits[sha]
                raise _Conflict("Invalid branch name")
            sha = self.commit(
                branch,
                body["commit_message"],
                [
                    {
                        "action": "create",
                        "file_path": path,
                        "content": body["content"],
                        "encoding": body.get("encoding", "text"),
                    }
                ],
            )
            return 200, self.commits[sha]
        if method == "DELETE":
            sha = self.branch_files.get(branch, {}).get(path)
            if sha is None:
                raise _Conflict("A file with this name does not exist")
            self.commit(
                branch,
                body["commit_message"],
                [{"action": "delete", "file_path": path}],
            )
            return 200, {}


class _NotFound(Exception):
    pass


class _Conflict(Exception):
    pass


@pytest.fixture(autouse=True)
def stub_timestamp_store():
    """Keep the date cache out of the store tests, like
    ``test_github_repo.py`` does: a real store would start background
    workers and write a cache file into the working directory."""
    with patch(
        "tgdcfs.core.repository.impl.metadata.github_repo.DirTimestampStore"
    ) as store_cls:
        store = Mock()
        store.get.return_value = None
        store_cls.return_value = store
        yield store


@pytest.fixture
def gitlab(monkeypatch):
    fake = FakeGitlab()

    def fake_request(self, method, url, params=None, json=None, timeout=None, **kw):
        return fake.handle(method, url, params, json)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    return fake


def gitlab_config(**overrides) -> GitlabRepoConfig:
    data = {
        "repo": "group/project",
        "branch": "main",
        "access_token": "glpat-token",
        "base_url": "https://gitlab.example.com",
    }
    data.update(overrides)
    return GitlabRepoConfig.from_dict(data)


def document(metadata: TGFSMetadata) -> str:
    return json.dumps(metadata.to_dict())


def sample_tree() -> TGFSMetadata:
    root = TGFSDirectory.root_dir()
    docs = root.create_dir("docs")
    docs.create_file_ref("readme", 42)
    return TGFSMetadata(root)


class TestFacade:
    async def test_the_recursive_tree_lists_every_blob(self, gitlab):
        gitlab.commit(
            "main",
            "seed",
            [
                {
                    "action": "create",
                    "file_path": "docs/readme.42",
                    "content": "",
                    "encoding": "text",
                },
                {
                    "action": "create",
                    "file_path": "docs/.gitkeep",
                    "content": "",
                    "encoding": "text",
                },
            ],
        )
        facade = GitlabRepository(gitlab_config())

        tree = facade.get_git_tree("main", recursive=True)

        assert tree.truncated is False
        assert {e.path for e in tree.tree} == {"docs/readme.42", "docs/.gitkeep"}

    async def test_pagination_is_followed(self, gitlab):
        gitlab.commit(
            "main",
            "seed",
            [
                {
                    "action": "create",
                    "file_path": f"f{i}.123",
                    "content": "",
                    "encoding": "text",
                }
                for i in range(250)
            ],
        )
        facade = GitlabRepository(gitlab_config())

        tree = facade.get_git_tree("main", recursive=True)

        assert len(tree.tree) == 250

    async def test_a_missing_branch_is_a_404(self, gitlab):
        facade = GitlabRepository(gitlab_config())

        with pytest.raises(GithubException) as ex:
            facade.get_git_ref("heads/main")
        assert ex.value.status == 404

    async def test_creating_a_file_that_exists_reads_as_an_earlier_landing(
        self, gitlab
    ):
        gitlab.commit(
            "main",
            "seed",
            [
                {
                    "action": "create",
                    "file_path": "docs/.gitkeep",
                    "content": "",
                    "encoding": "text",
                },
            ],
        )
        facade = GitlabRepository(gitlab_config())

        with pytest.raises(GithubException) as ex:
            facade.create_file("docs/.gitkeep", "Create directory", "", "main")
        assert ex.value.status == 422
        assert ex.value.data == {"message": '"sha" wasn\'t supplied'}

    async def test_a_ref_update_refuses_a_moved_branch(self, gitlab):
        gitlab.commit("main", "first", [])
        facade = GitlabRepository(gitlab_config())
        ref = facade.get_git_ref("heads/main")

        with pytest.raises(GithubException) as ex:
            ref.edit("not-the-head")
        assert ex.value.status == 422
        assert "fast forward" in str(ex.value.data)

    async def test_a_commit_lands_on_the_branch(self, gitlab):
        gitlab.commit("main", "first", [])
        facade = GitlabRepository(gitlab_config())
        pending = facade.create_git_tree([])
        sha = facade.create_git_commit("empty", pending, []).sha

        assert sha == gitlab.branch_heads["main"]

    async def test_a_commit_creates_a_missing_branch_from_the_default(self, gitlab):
        gitlab.commit(
            "master",
            "seed",
            [
                {
                    "action": "create",
                    "file_path": "readme.md",
                    "content": "hi",
                    "encoding": "text",
                },
            ],
        )
        facade = GitlabRepository(gitlab_config())
        with pytest.raises(GithubException):
            facade.get_git_ref("heads/main")  # marks the branch as missing

        pending = facade.create_git_tree([])
        facade.create_git_commit("bootstrap", pending, [])

        assert gitlab.file_content("main", "readme.md") == "hi"

    async def test_a_delete_element_removes_the_file(self, gitlab):
        """A "sha": null element deletes the entry, like GitHub's tree
        API answers; before, the facade fetched a blob named None."""
        gitlab.commit(
            "main",
            "first",
            [
                {
                    "action": "create",
                    "file_path": "sub/.tgdcfsstats.json",
                    "content": "{}",
                    "encoding": "text",
                },
            ],
        )
        facade = GitlabRepository(gitlab_config())
        ref = facade.get_git_ref("heads/main")
        base_commit = facade.get_git_commit(ref.object.sha)
        pending = facade.create_git_tree(
            [
                InputGitTreeElement(
                    path="sub/.tgdcfsstats.json", mode="100644", type="blob", sha=None
                )
            ],
            base_tree=base_commit.tree,
        )
        facade.create_git_commit("drop the stats", pending, [base_commit])

        assert gitlab.file_content("main", "sub/.tgdcfsstats.json") is None

    async def test_a_delete_element_of_a_missing_file_is_a_noop(self, gitlab):
        gitlab.commit("main", "first", [])
        facade = GitlabRepository(gitlab_config())
        ref = facade.get_git_ref("heads/main")
        base_commit = facade.get_git_commit(ref.object.sha)
        pending = facade.create_git_tree(
            [
                InputGitTreeElement(
                    path="gone/.tgdcfsstats.json", mode="100644", type="blob", sha=None
                )
            ],
            base_tree=base_commit.tree,
        )
        facade.create_git_commit("drop the stats", pending, [base_commit])

        assert gitlab.file_content("main", "gone/.tgdcfsstats.json") is None

    async def test_a_commit_bootstraps_an_empty_project(self, monkeypatch):
        fake = FakeGitlab(empty_project=True)

        def fake_request(self, method, url, params=None, json=None, timeout=None, **kw):
            return fake.handle(method, url, params, json)

        monkeypatch.setattr(requests.Session, "request", fake_request)
        facade = GitlabRepository(gitlab_config())
        pending = facade.create_git_tree(
            [
                InputGitTreeElement(
                    path="hello.txt", mode="100644", type="blob", content="world"
                )
            ]
        )
        facade.create_git_commit("first ever", pending, [])

        assert fake.default_branch == "main"
        assert fake.file_content("main", "hello.txt") == "world"


class TestGitlabTreeStore:
    def repository(self, gitlab, config=None, name_key=None, blob_key=None):
        return GitlabTreeRepoMetadataRepository(
            config or gitlab_config(), name_key=name_key, blob_key=blob_key
        )

    async def test_a_fresh_repository_serves_an_empty_tree(self, gitlab):
        repo = self.repository(gitlab)

        metadata = await repo.get()

        assert metadata.dir.find_files() == []
        assert metadata.dir.find_dirs() == []

    async def test_the_mirrored_tree_is_read(self, gitlab):
        gitlab.commit(
            "main",
            "seed",
            [
                {
                    "action": "create",
                    "file_path": "docs/.gitkeep",
                    "content": "",
                    "encoding": "text",
                },
                {
                    "action": "create",
                    "file_path": "docs/readme.42",
                    "content": "",
                    "encoding": "text",
                },
            ],
        )
        repo = self.repository(gitlab)

        metadata = await repo.get()

        docs = metadata.dir.find_dir("docs")
        assert docs.find_file("readme").message_id == 42

    async def test_an_empty_tree_pushes_the_mirror(self, gitlab):
        repo = self.repository(gitlab)
        await repo.init()
        repo.metadata.dir.create_dir("docs")

        await repo.push_full()

        assert gitlab.file_content("main", "docs/.gitkeep") == ""

    async def test_the_document_fallback_serves_the_old_layout(self, gitlab):
        gitlab.commit(
            "main",
            "seed",
            [
                {
                    "action": "create",
                    "file_path": "metadata.json",
                    "content": document(sample_tree()),
                    "encoding": "text",
                },
            ],
        )
        repo = self.repository(gitlab)

        metadata = await repo.get()

        docs = metadata.dir.find_dir("docs")
        assert docs.find_file("readme").message_id == 42

    async def test_the_document_fallback_decrypts(self, gitlab):
        key = derive_metadata_blob_key(b"k" * 32)
        gitlab.commit(
            "main",
            "seed",
            [
                {
                    "action": "create",
                    "file_path": "metadata.json",
                    "content": encrypt_metadata_blob(
                        key, document(sample_tree()).encode()
                    ),
                    "encoding": "text",
                },
            ],
        )
        repo = self.repository(gitlab, blob_key=key)

        metadata = await repo.get()

        assert metadata.dir.find_dir("docs").find_file("readme").message_id == 42

    async def test_the_first_push_migrates_and_removes_the_document(self, gitlab):
        gitlab.commit(
            "main",
            "seed",
            [
                {
                    "action": "create",
                    "file_path": "metadata.json",
                    "content": document(sample_tree()),
                    "encoding": "text",
                },
            ],
        )
        repo = self.repository(gitlab)
        await repo.get()

        await repo.push_full()

        assert gitlab.file_content("main", "metadata.json") is None
        # The document's tree is mirrored blob by blob.
        assert gitlab.file_content("main", "docs/readme.42") == ""
        # And the tree is served from the mirror from now on, not the
        # (gone) document.
        again = await repo.get()
        assert again.dir.find_dir("docs").find_file("readme").message_id == 42

    async def test_names_are_encrypted_on_the_way_out(self, gitlab):
        key = b"k" * 32
        repo = self.repository(gitlab, name_key=key)
        await repo.init()
        repo.metadata.dir.create_dir("docs")

        await repo.push_full()

        from tgdcfs.crypto.path_names import encrypt_path_name

        assert (
            gitlab.file_content("main", f"{encrypt_path_name(key, 'docs')}/.gitkeep")
            == ""
        )

    async def test_a_missing_document_does_not_shadow_an_empty_tree(self, gitlab):
        repo = self.repository(gitlab)
        await repo.init()

        await repo.push_full()

        assert gitlab.file_content("main", "metadata.json") is None

    async def test_incremental_writes_land_in_the_repository(self, gitlab):
        repo = self.repository(gitlab)
        await repo.init()

        docs = repo.metadata.dir.create_dir("docs")
        await docs.create_file_ref_async("readme", 42)

        assert gitlab.file_content("main", "docs/readme.42") == ""

    async def test_a_wrapped_document_tree_keeps_writing_through(self, gitlab):
        gitlab.commit(
            "main",
            "seed",
            [
                {
                    "action": "create",
                    "file_path": "metadata.json",
                    "content": document(sample_tree()),
                    "encoding": "text",
                },
            ],
        )
        repo = self.repository(gitlab)
        await repo.get()

        docs = repo.metadata.dir.find_dir("docs")
        await docs.create_file_ref_async("other", 7)

        assert gitlab.file_content("main", "docs/other.7") == ""

    async def test_as_a_mirror_it_syncs_a_tree_loaded_elsewhere(self, gitlab):
        """The redundant-metadata shape: a pinned message primary serves a
        plain tree; the GitLab tree mirror receives the full sync."""
        from tgdcfs.core.repository.impl.metadata.replicated import (
            MetadataStore,
            ReplicatedMetadataRepository,
        )

        class PinnedStub(IMetaDataRepository):
            def __init__(self, tree):
                super().__init__()
                self._tree = tree

            async def get(self):
                return self._tree

            async def push(self):
                pass

        tree = sample_tree()
        mirror = self.repository(gitlab)
        layer = ReplicatedMetadataRepository(
            [
                MetadataStore(
                    key="pinned@tg-main", repo=PinnedStub(tree), primary=True
                ),
                MetadataStore(key="gitlab_repo@g/p@main", repo=mirror),
            ]
        )

        await layer.init()

        # One shared tree object, store-backed for the mirror.
        assert layer.metadata is tree
        assert isinstance(tree.dir, GithubDirectory)
        assert gitlab.file_content("main", "docs/readme.42") == ""
