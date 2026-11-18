"""A PyGithub-shaped window on a GitLab repository.

The tree-mirror metadata store (``GithubRepoMetadataRepository``) speaks
PyGithub: branches, commits, recursive trees, tree rewrites. GitLab's
REST API shapes the same git concepts differently -- a commit is posted
as a batch of file actions instead of tree plus commit plus ref update,
listings paginate where GitHub truncates, and a tree object id is not a
valid ``ref``. This facade translates: it implements exactly the subset
of PyGithub's ``Repository`` the metadata store uses, backed by GitLab
REST v4, and raises PyGithub's ``GithubException`` so every retry,
fallback and error check of the store keeps working unchanged.

All methods are blocking and run on the store's writer thread (or a
``to_thread`` read), like their PyGithub counterparts.
"""

import asyncio
import base64
import json
import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, cast
from urllib.parse import quote

import requests
from github import GithubException, InputGitTreeElement
from github.Repository import Repository

from tgdcfs.config import GithubRepoConfig, GitlabRepoConfig
from tgdcfs.core.model import TGFSMetadata
from tgdcfs.core.repository.impl.metadata.github_repo import (
    GithubRepoMetadataRepository,
)
from tgdcfs.core.repository.impl.metadata.github_repo.dir_timestamps import (
    STATS_COMMIT_SCAN_LIMIT,
)
from tgdcfs.crypto.metadata_blob import decrypt_metadata_blob
from tgdcfs.errors import TechnicalError

logger = logging.getLogger(__name__)

# One listing page; the facade always follows the whole pagination, so a
# load never silently misses the tail of a big repository the way a
# truncated GitHub tree would.
PER_PAGE = 100
REQUEST_TIMEOUT_SECONDS = 60.0


class _TreeEntry(NamedTuple):
    path: str
    type: str
    sha: str
    mode: str


class _Tree:
    def __init__(self, entries: Sequence[_TreeEntry]):
        self.truncated = False
        self.tree = list(entries)


class _GitRefObject:
    def __init__(self, sha: str):
        self.sha = sha


class _GitRef:
    def __init__(self, facade: "GitlabRepository", branch: str, sha: str):
        self._facade = facade
        self._branch = branch
        self.object = _GitRefObject(sha)

    def edit(self, sha: str, force: bool = False) -> None:
        self._facade._verify_branch_head(self._branch, sha)


class _GitTreeObject:
    def __init__(self, sha: str):
        self.sha = sha


class _Commit:
    def __init__(self, sha: str):
        self.sha = sha
        # GitLab exposes no tree ids; the tree API accepts the commit sha
        # as its ref, so the commit sha stands in for the tree sha.
        self.tree = _GitTreeObject(sha)


class _CommitMeta:
    def __init__(self, data: dict):
        self.message = data.get("message", "")
        self.committer = type(
            "_Committer", (), {"date": _parse_date(data.get("committed_date"))}
        )()


class _CommitInfo:
    def __init__(self, data: dict):
        self.sha = data.get("id", "")
        self.commit = _CommitMeta(data)


class _Blob:
    def __init__(self, content: str):
        self.content = content


class _Branch:
    def __init__(self, sha: str):
        self.commit = _GitRefObject(sha)


class _DiffFile:
    def __init__(self, filename: str, previous_filename: Optional[str]):
        self.filename = filename
        self.previous_filename = previous_filename


class _Comparison:
    def __init__(self, files: List[_DiffFile]):
        self.files = files


class _ContentFile:
    def __init__(self, type: str, name: str, path: str, sha: str):
        self.type = type
        self.name = name
        self.path = path
        self.sha = sha


class _PendingTree:
    """The actions ``create_git_tree`` derived; applied by the next
    ``create_git_commit`` on the same (single writer) thread."""

    def __init__(self, actions: List[dict]):
        self.actions = actions
        self.sha = ""


def _parse_date(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _error_data(response: requests.Response) -> dict:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"message": str(data)}
    except Exception:
        return {"message": response.text[:500]}


class GitlabRepository:
    """The subset of PyGithub's ``Repository`` the tree-mirror metadata
    store uses, served from the GitLab REST API."""

    def __init__(self, config: GitlabRepoConfig):
        self._base_url = config.base_url.rstrip("/")
        self._project = quote(config.repo, safe="")
        self._branch = config.branch
        self._token = config.access_token
        self._local = threading.local()
        # Tree object ids seen in listings, mapped to their path: GitLab's
        # tree API takes a commit-ish ref plus a path, never a tree id,
        # while the store walks trees by the shas a parent listing showed.
        self._tree_paths: Dict[str, str] = {}
        # Set when ``get_git_ref`` answered 404: the next commit creates
        # the branch, which needs a start point (see ``_post_commit``).
        self._branch_missing = False

    # -- transport -----------------------------------------------------------

    @property
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers["PRIVATE-TOKEN"] = self._token
            self._local.session = session
        return session

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> requests.Response:
        url = f"{self._base_url}/api/v4/projects/{self._project}{path}"
        response = self._session.request(
            method,
            url,
            params=params,
            json=json_body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            raise GithubException(
                response.status_code, _error_data(response), dict(response.headers)
            )
        return response

    def _json(self, response: requests.Response) -> Any:
        if not response.content:
            return {}
        return response.json()

    def _paginated(self, path: str, params: dict) -> List[dict]:
        items: List[dict] = []
        page = 1
        while True:
            response = self._request(
                "GET", path, params=dict(params, page=page, per_page=PER_PAGE)
            )
            batch = self._json(response)
            if not isinstance(batch, list):
                return items if items else []
            items.extend(batch)
            next_page = response.headers.get("X-Next-Page", "").strip()
            if not next_page or len(batch) < PER_PAGE:
                return items
            page = int(next_page)

    # -- project and branch state ---------------------------------------------

    @property
    def full_name(self) -> str:
        return self._project

    def _project_data(self) -> dict:
        return self._json(self._request("GET", ""))

    @property
    def created_at(self) -> Optional[datetime]:
        return _parse_date(self._project_data().get("created_at"))

    @property
    def pushed_at(self) -> Optional[datetime]:
        # GitLab has no pushed_at; last activity is the closest signal.
        return _parse_date(self._project_data().get("last_activity_at"))

    @property
    def updated_at(self) -> Optional[datetime]:
        return _parse_date(self._project_data().get("last_activity_at"))

    def _branch_head(self, branch: Optional[str] = None) -> str:
        name = branch or self._branch
        data = self._json(
            self._request("GET", f"/repository/branches/{quote(name, safe='')}")
        )
        return data["commit"]["id"]

    def get_branch(self, branch: str) -> _Branch:
        return _Branch(self._branch_head(branch))

    def get_git_ref(self, ref: str) -> _GitRef:
        branch = ref
        if branch.startswith("heads/"):
            branch = branch[len("heads/") :]
        try:
            sha = self._branch_head(branch)
        except GithubException as ex:
            if ex.status == 404 and branch == self._branch:
                self._branch_missing = True
            raise
        self._branch_missing = False
        return _GitRef(self, branch, sha)

    def _verify_branch_head(self, branch: str, sha: str) -> None:
        # The commit that was supposed to move the ref already landed (the
        # commits API commits straight onto the branch); a head that does
        # not match means another writer raced in between, which the
        # callers treat exactly like GitHub's non-fast-forward refusal.
        head = self._branch_head(branch)
        if head != sha:
            raise GithubException(422, {"message": "Update is not a fast forward"})

    def create_git_ref(self, ref: str, sha: str) -> None:
        if not sha:
            # An empty sync on a completely empty project: there is no
            # branch to create yet, and the first real write initializes
            # the repository (see ``_bootstrap_branch``).
            return
        branch = ref
        if branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/") :]
        try:
            self._verify_branch_head(branch, sha)
            return
        except GithubException as ex:
            if ex.status != 422:
                raise
        self._request(
            "POST", "/repository/branches", json_body={"branch": branch, "ref": sha}
        )

    # -- reads -----------------------------------------------------------------

    def get_git_commit(self, sha: str) -> _Commit:
        self._json(self._request("GET", f"/repository/commits/{quote(sha, safe='')}"))
        return _Commit(sha)

    def get_git_tree(self, sha: str, recursive: bool = False) -> _Tree:
        # A tree id the facade itself listed maps back to its path; a
        # commit sha or branch name goes through as the ref directly.
        params: Dict[str, Any] = {"ref": sha}
        path = self._tree_paths.get(sha)
        if path:
            params["ref"] = self._branch
            params["path"] = path
        if recursive:
            params["recursive"] = "true"
        try:
            items = self._paginated("/repository/tree", params)
        except GithubException as ex:
            if ex.status == 404 and sha == self._branch:
                # A listing of the branch itself: the branch does not
                # exist yet, which the next write has to bootstrap.
                self._branch_missing = True
            raise
        entries = [
            _TreeEntry(
                path=item["path"],
                type=item["type"],
                sha=item["id"],
                mode=item.get("mode", "100644"),
            )
            for item in items
        ]
        for entry in entries:
            if entry.type == "tree":
                self._tree_paths[entry.sha] = entry.path
        return _Tree(entries)

    def get_git_blob(self, sha: str) -> _Blob:
        data = self._json(
            self._request("GET", f"/repository/blobs/{quote(sha, safe='')}")
        )
        return _Blob(data.get("content", ""))

    def get_contents(self, path: str, ref: Optional[str] = None) -> Any:
        ref = ref or self._branch
        path = path.strip("/")
        if path:
            try:
                data = self._json(
                    self._request(
                        "GET",
                        f"/repository/files/{quote(path, safe='')}",
                        params={"ref": ref},
                    )
                )
                return _ContentFile(
                    type="file",
                    name=path.rsplit("/", 1)[-1],
                    path=path,
                    sha=data.get("blob_id", ""),
                )
            except GithubException as ex:
                if ex.status != 404:
                    raise
        entries = [
            _ContentFile(
                type="dir" if item["type"] == "tree" else "file",
                name=item["path"].rsplit("/", 1)[-1],
                path=item["path"],
                sha=item["id"],
            )
            for item in self._paginated(
                "/repository/tree", {"ref": ref, **({"path": path} if path else {})}
            )
        ]
        if path and not entries:
            # A git tree has no empty directories: nothing under the path
            # means the path does not exist, like GitHub's 404.
            raise GithubException(404, {"message": "not found"})
        return entries

    def get_file_text(self, path: str, ref: Optional[str] = None) -> Optional[str]:
        """The text content of one file, or ``None`` when it does not exist.

        Used by the tree store to read (and so migrate) the single-document
        layout an older ``gitlab_repo`` configuration left behind."""
        path = path.strip("/")
        try:
            data = self._json(
                self._request(
                    "GET",
                    f"/repository/files/{quote(path, safe='')}",
                    params={"ref": ref or self._branch},
                )
            )
        except GithubException as ex:
            if ex.status == 404:
                return None
            raise
        return base64.b64decode(data.get("content", "")).decode()

    def get_commits(
        self, sha: Optional[str] = None, path: Optional[str] = None
    ) -> List[_CommitInfo]:
        params: Dict[str, Any] = {"ref_name": sha or self._branch}
        if path:
            params["path"] = path
        # The consumer scans at most STATS_COMMIT_SCAN_LIMIT entries for
        # the last non-stats commit; never pull more history than that.
        items = self._paginated("/repository/commits", params)
        return [_CommitInfo(item) for item in items[: STATS_COMMIT_SCAN_LIMIT + 1]]

    def compare(self, base: str, head: str) -> _Comparison:
        data = self._json(
            self._request(
                "GET", "/repository/compare", params={"from": base, "to": head}
            )
        )
        files = [
            _DiffFile(
                filename=diff.get("new_path", ""),
                previous_filename=(
                    diff.get("old_path")
                    if diff.get("renamed_file") and diff.get("old_path")
                    else None
                ),
            )
            for diff in data.get("diffs", [])
        ]
        return _Comparison(files)

    # -- writes ------------------------------------------------------------------

    def create_file(
        self,
        path: str,
        message: str,
        content: str,
        branch: str,
        **_kwargs: Any,
    ) -> None:
        body = {
            "branch": branch,
            "content": content,
            "commit_message": message,
            "encoding": "text",
        }
        if self._branch_missing:
            # The branch does not exist yet; the write bootstraps it the
            # same way a commit does (see ``_post_commit``).
            actions = [
                {
                    "action": "create",
                    "file_path": path,
                    "content": content,
                    "encoding": "text",
                }
            ]
            self._bootstrap_branch(
                {"branch": branch, "commit_message": message, "actions": actions}
            )
            if not actions:
                # An empty project: writing the file initialized it.
                return
        try:
            self._request(
                "POST", f"/repository/files/{quote(path, safe='')}", json_body=body
            )
        except GithubException as ex:
            if ex.status == 400 and "already exist" in str(ex.data).lower():
                # GitHub refuses a create of an existing blob with the 422
                # below; the store reads it as "an earlier attempt landed".
                raise GithubException(
                    422, {"message": '"sha" wasn\'t supplied'}
                ) from ex
            raise

    def delete_file(
        self, path: str, message: str, sha: str, branch: str, **_kwargs: Any
    ) -> None:
        self._request(
            "DELETE",
            f"/repository/files/{quote(path, safe='')}",
            json_body={
                "branch": branch,
                "commit_message": message,
            },
        )

    def create_git_tree(
        self,
        elements: Sequence[InputGitTreeElement],
        base_tree: Any = None,
    ) -> _PendingTree:
        """Diff the desired tree against the branch's current blobs and
        translate it into GitLab file actions.

        With ``base_tree`` the elements only overlay the branch (the stats
        commits); without it the elements are the whole desired tree and
        everything not listed is deleted (the sync, move and delete
        rewrites).
        """
        desired: Dict[str, tuple] = {}
        for element in elements:
            identity = element._identity
            desired[identity["path"]] = (
                identity.get("sha"),
                identity.get("content"),
            )

        try:
            current_tree = self.get_git_tree(self._branch, recursive=True)
        except GithubException as ex:
            if ex.status != 404:
                raise
            # The branch does not exist yet (a fresh repository); the
            # desired tree is the whole state, nothing to diff against.
            current_tree = _Tree([])
        current = {
            entry.path: entry.sha for entry in current_tree.tree if entry.type == "blob"
        }

        actions: List[dict] = []
        for path, (sha, content) in sorted(desired.items()):
            if content is None and sha is None:
                # "sha": null removes the entry, like GitHub's tree API.
                if path in current:
                    actions.append({"action": "delete", "file_path": path})
            elif content is not None:
                actions.append(
                    {
                        "action": "update" if path in current else "create",
                        "file_path": path,
                        "content": content,
                        "encoding": "text",
                    }
                )
            elif path not in current:
                # A blob the branch does not hold yet: a fresh mirror, or
                # a path a tree rewrite moved here. GitLab commits by
                # content, so the blob's content travels with the action.
                actions.append(
                    {
                        "action": "create",
                        "file_path": path,
                        "content": self.get_git_blob(sha).content,
                        "encoding": "base64",
                    }
                )
            elif current[path] != sha:
                actions.append(
                    {
                        "action": "update",
                        "file_path": path,
                        "content": self.get_git_blob(sha).content,
                        "encoding": "base64",
                    }
                )
        if base_tree is None:
            actions.extend(
                {"action": "delete", "file_path": path}
                for path in sorted(current)
                if path not in desired
            )
        return _PendingTree(actions)

    def create_git_commit(
        self, message: str, tree: _PendingTree, parents: Sequence[Any]
    ) -> _Commit:
        actions = tree.actions
        if not actions:
            return _Commit(self._noop_commit())
        sha = self._post_commit(message, actions)
        tree.sha = sha
        return _Commit(sha)

    def _noop_commit(self) -> str:
        """The head an empty commit leaves the branch at: the current one,
        with a missing branch pointed at the project's default branch so
        the next write has a home. A completely empty project cannot hold
        a branch at all -- there is nothing to sync onto it yet."""
        try:
            return self._branch_head()
        except GithubException as ex:
            if ex.status != 404:
                raise
        default = self._project_data().get("default_branch")
        if not default:
            return ""
        try:
            head = self._branch_head(default)
        except GithubException:
            return ""
        try:
            self._request(
                "POST",
                "/repository/branches",
                json_body={"branch": self._branch, "ref": head},
            )
        except GithubException as ex:
            if ex.status != 400:
                raise
            # The branch came into existence in the meantime.
            head = self._branch_head()
        self._branch_missing = False
        return head

    def _post_commit(self, message: str, actions: List[dict]) -> str:
        body: Dict[str, Any] = {
            "branch": self._branch,
            "commit_message": message,
            "actions": actions,
        }
        if self._branch_missing:
            # The branch does not exist yet; the commits API only creates
            # it when told where to start from.
            self._bootstrap_branch(body)
            if not body["actions"]:
                # A one-action commit on an empty project: writing the
                # first file already created it.
                self._branch_missing = False
                return self._branch_head()
        try:
            data = self._json(
                self._request("POST", "/repository/commits", json_body=body)
            )
        except GithubException as ex:
            if ex.status != 400:
                raise
            text = str(ex.data).lower()
            if "already exist" in text and any(
                action["action"] == "create" for action in actions
            ):
                # Another writer created one of the paths first; our
                # content is the desired end state either way.
                for action in actions:
                    if action["action"] == "create":
                        action["action"] = "update"
                data = self._json(
                    self._request("POST", "/repository/commits", json_body=body)
                )
            else:
                raise
        self._branch_missing = False
        return data["id"]

    def _bootstrap_branch(self, body: Dict[str, Any]) -> None:
        """Give the commit a branch to land on: from the project's default
        branch, or -- on a completely empty project -- by writing the
        first file, which initializes the repository."""
        default_branch = self._project_data().get("default_branch")
        if default_branch:
            try:
                head = self._branch_head(default_branch)
            except GithubException:
                head = None
            if head:
                try:
                    self._request(
                        "POST",
                        "/repository/branches",
                        json_body={"branch": self._branch, "ref": head},
                    )
                    return
                except GithubException as ex:
                    if ex.status != 400:
                        raise
                # A 400 here means the branch came into existence in the
                # meantime; the commit below lands on it.
        for action in list(body["actions"]):
            if action["action"] not in ("create", "update"):
                continue
            self._request(
                "POST",
                f"/repository/files/{quote(action['file_path'], safe='')}",
                json_body={
                    "branch": self._branch,
                    "content": action["content"],
                    "encoding": action.get("encoding", "text"),
                    "commit_message": body["commit_message"],
                },
            )
            body["actions"].remove(action)
            return


class GitlabTreeRepoMetadataRepository(GithubRepoMetadataRepository):
    """The GitLab repository as a mirrored metadata tree, like ``github_repo``.

    Speaks the PyGithub surface through :class:`GitlabRepository`, so the
    whole tree engine -- incremental writes, the diff sync, the stats
    files, the directory dates -- runs unchanged on GitLab.

    A repository that still holds the older single-document layout (the
    ``gitlab_repo`` store of releases before the layout existed) is read
    through that document until the first push has mirrored the tree; the
    document is then removed, so the migration runs once.
    """

    def __init__(
        self,
        config: GitlabRepoConfig,
        name_key: Optional[bytes] = None,
        blob_key: Optional[bytes] = None,
    ):
        self._gitlab = GitlabRepository(config)
        self._blob_key = blob_key
        self._document_path = config.file_path
        # Set once the document is confirmed gone (or never existed), so
        # the cleanup costs one request per process at most.
        self._document_removed = False
        gh_config = GithubRepoConfig(
            repo=config.repo,
            commit=config.branch,
            access_token=config.access_token,
        )
        # The facade implements the subset of PyGithub's Repository the
        # engine uses; the cast records that duck-typed contract.
        super().__init__(
            gh_config,
            name_key=name_key,
            repo=cast(Repository, self._gitlab),
        )

    async def get(self) -> TGFSMetadata:
        metadata = await super().get()
        if self._tree_is_empty(metadata):
            document = await self._read_document()
            if document is not None:
                try:
                    text = (
                        decrypt_metadata_blob(self._blob_key, document).decode()
                        if self._blob_key is not None
                        else document
                    )
                    loaded = TGFSMetadata.from_dict(json.loads(text))
                except Exception as ex:
                    raise TechnicalError(
                        f"Could not parse the metadata document of "
                        f"{self._ghc.repo_name}@{self._ghc.commit}:"
                        f"{self._document_path}: {ex}"
                    ) from ex
                logger.info(
                    f"{self._ghc.repo_name}@{self._ghc.commit} holds no "
                    f"metadata tree yet; serving the single-document layout "
                    f"from {self._document_path} until the first push has "
                    f"mirrored the tree"
                )
                # Through the setter, so the document's plain tree becomes
                # this store's own node type and every later mutation keeps
                # writing through incrementally.
                self.metadata = loaded
                return self.metadata
        return metadata

    async def push_full(self) -> None:
        await super().push_full()
        await self._remove_document()

    @staticmethod
    def _tree_is_empty(metadata: TGFSMetadata) -> bool:
        return not metadata.dir.find_files() and not metadata.dir.find_dirs()

    async def _read_document(self) -> Optional[str]:
        try:
            return await asyncio.to_thread(
                self._gitlab.get_file_text, self._document_path
            )
        except Exception as ex:
            # A document that cannot be read must not shadow a tree that
            # simply is empty; the next push re-syncs the repo anyway.
            logger.warning(
                f"Could not read the metadata document {self._document_path} "
                f"from {self._ghc.repo_name}@{self._ghc.commit}: {ex}"
            )
            return None

    async def _remove_document(self) -> None:
        if self._document_removed:
            return
        self._document_removed = True
        try:
            exists = (
                await asyncio.to_thread(self._gitlab.get_file_text, self._document_path)
            ) is not None
            if not exists:
                return
            await asyncio.to_thread(
                self._gitlab.delete_file,
                self._document_path,
                "Remove the TGDCFS single-document metadata (migrated to the "
                "mirrored tree layout)",
                "",
                self._ghc.commit,
            )
            logger.info(
                f"Removed the migrated metadata document {self._document_path} "
                f"from {self._ghc.repo_name}@{self._ghc.commit}"
            )
        except Exception as ex:
            # The tree is synced; a document that refuses to go away is
            # retried on the next push_full of this process.
            self._document_removed = False
            logger.warning(
                f"Could not remove the migrated metadata document "
                f"{self._document_path} from {self._ghc.repo_name}@"
                f"{self._ghc.commit}: {ex}"
            )
