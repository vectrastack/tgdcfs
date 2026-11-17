import { Add, Delete } from "@mui/icons-material";
import {
  Box,
  Button,
  Checkbox,
  Chip,
  FormControl,
  FormControlLabel,
  IconButton,
  InputLabel,
  MenuItem,
  OutlinedInput,
  Select,
  Typography,
} from "@mui/material";
import {
  FilesystemConfig,
  MetadataEntry,
  MetadataType,
  MirrorMode,
  StoreConfig,
  SyncMode,
  WriteAck,
  needsReupload,
} from "../types";
import { ConfigTextField } from "./ConfigTextField";

interface FilesystemFieldProps {
  filesystem: FilesystemConfig;
  stores: StoreConfig[];
  onUpdate: <K extends keyof FilesystemConfig>(
    field: K,
    value: FilesystemConfig[K]
  ) => void;
  onDelete?: () => void;
  nameErrors?: string[];
  primaryErrors?: string[];
  mirrorErrors?: string[];
  // Set when a mirror is the primary of another file system.
  needsSharedStore?: boolean;
  // Whether the local cache is on; write-back (write_ack: cache) needs it.
  cacheEnabled?: boolean;
}

const storeLabel = (store: StoreConfig) =>
  `${store.name || "(unnamed)"} · ${store.backend}`;

export function FilesystemField({
  filesystem,
  stores,
  onUpdate,
  onDelete,
  nameErrors = [],
  primaryErrors = [],
  mirrorErrors = [],
  needsSharedStore = false,
  cacheEnabled = false,
}: FilesystemFieldProps) {
  const namedStores = stores.filter((s) => s.name.trim() !== "");
  const mirrorCandidates = namedStores.filter(
    (s) => s.name !== filesystem.primary
  );
  const reuploadMirror = needsReupload(filesystem, namedStores);
  // The stores a pinned message may name: the primary and its mirrors.
  const fsStoreNames = [filesystem.primary, ...filesystem.mirrors];

  const updateEntry = (index: number, patch: Partial<MetadataEntry>) => {
    onUpdate("metadata", [
      ...filesystem.metadata.slice(0, index),
      { ...filesystem.metadata[index], ...patch },
      ...filesystem.metadata.slice(index + 1),
    ]);
  };
  const removeEntry = (index: number) => {
    onUpdate(
      "metadata",
      filesystem.metadata.filter((_, i) => i !== index)
    );
  };
  const updateGitHubRepo = (
    index: number,
    field: "repo" | "commit" | "access_token",
    value: string
  ) => {
    const entry = filesystem.metadata[index];
    updateEntry(index, {
      github_repo: {
        repo: "",
        commit: "master",
        access_token: "",
        ...entry.github_repo,
        [field]: value,
      },
    });
  };
  const updateGitlabRepo = (
    index: number,
    field: "repo" | "branch" | "access_token" | "url",
    value: string
  ) => {
    const entry = filesystem.metadata[index];
    updateEntry(index, {
      gitlab_repo: {
        repo: "",
        branch: "main",
        access_token: "",
        url: "",
        ...entry.gitlab_repo,
        [field]: value,
      },
    });
  };

  return (
    <Box sx={{ mb: 3, pb: 2, borderBottom: 1, borderColor: "divider" }}>
      <Box sx={{ display: "flex", alignItems: "flex-start", gap: 1, mb: 2 }}>
        <ConfigTextField
          label="File System Name"
          value={filesystem.name}
          onChange={(e) => onUpdate("name", e.target.value)}
          required
          error={nameErrors.length > 0}
          helperText={
            nameErrors.length > 0
              ? nameErrors.join("; ")
              : "Top-level directory over WebDAV and SFTP"
          }
          width={220}
        />
        <FormControl
          size="small"
          sx={{ minWidth: 220 }}
          error={primaryErrors.length > 0}
        >
          <InputLabel>Primary Store</InputLabel>
          <Select
            value={
              namedStores.some((s) => s.name === filesystem.primary)
                ? filesystem.primary
                : ""
            }
            label="Primary Store"
            onChange={(e) => onUpdate("primary", e.target.value)}
          >
            {namedStores.map((store) => (
              <MenuItem key={store.name} value={store.name}>
                {storeLabel(store)}
              </MenuItem>
            ))}
          </Select>
          {primaryErrors.length > 0 && (
            <Typography variant="caption" color="error" sx={{ pl: 2 }}>
              {primaryErrors.join("; ")}
            </Typography>
          )}
        </FormControl>
        {onDelete && (
          <IconButton
            color="error"
            onClick={onDelete}
            sx={{ mt: 0.5 }}
            size="small"
          >
            <Delete />
          </IconButton>
        )}
      </Box>

      <Typography variant="subtitle2" sx={{ mb: 1 }}>
        Metadata (redundancy set)
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1, pl: 2 }}>
        Every entry below is a full copy of the directory tree, kept in step
        like the files themselves: the first entry that can be read wins, and
        a store that is temporarily unreachable is caught up when it answers
        again. A pinned message names its own store, so a Telegram pinned
        message and a Discord pinned message are two separate copies.
      </Typography>
      {filesystem.metadata.map((entry, entryIndex) => (
        <Box
          key={entryIndex}
          sx={{
            mb: 1.5,
            p: 1.5,
            pl: 2,
            border: 1,
            borderColor: "divider",
            borderRadius: 1,
          }}
        >
          <Box sx={{ display: "flex", alignItems: "flex-start", gap: 1 }}>
            <FormControl size="small" sx={{ minWidth: 200 }}>
              <InputLabel>Metadata Type</InputLabel>
              <Select
                value={entry.type}
                label="Metadata Type"
                onChange={(e) =>
                  updateEntry(entryIndex, {
                    type: e.target.value as MetadataType,
                    ...(e.target.value === "github_repo"
                      ? {
                          github_repo:
                            entry.github_repo ?? {
                              repo: "",
                              commit: "master",
                              access_token: "",
                            },
                        }
                      : {}),
                    ...(e.target.value === "gitlab_repo"
                      ? {
                          gitlab_repo:
                            entry.gitlab_repo ?? {
                              repo: "",
                              branch: "main",
                              access_token: "",
                              url: "",
                            },
                        }
                      : {}),
                  })
                }
              >
                <MenuItem value="pinned_message">Pinned Message</MenuItem>
                <MenuItem value="github_repo">GitHub Repository</MenuItem>
                <MenuItem value="gitlab_repo">GitLab Repository</MenuItem>
              </Select>
            </FormControl>
            {entry.type === "pinned_message" && (
              <FormControl size="small" sx={{ minWidth: 200 }}>
                <InputLabel>Pinned In Store</InputLabel>
                <Select
                  value={
                    entry.store !== undefined &&
                    fsStoreNames.includes(entry.store)
                      ? entry.store
                      : ""
                  }
                  label="Pinned In Store"
                  onChange={(e) =>
                    updateEntry(entryIndex, {
                      store: e.target.value || undefined,
                    })
                  }
                >
                  <MenuItem value="">Primary store (default)</MenuItem>
                  {fsStoreNames.map((name) => (
                    <MenuItem key={name} value={name}>
                      {name}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
            )}
            {filesystem.metadata.length > 1 && (
              <IconButton
                color="error"
                onClick={() => removeEntry(entryIndex)}
                sx={{ mt: 0.5 }}
                size="small"
              >
                <Delete />
              </IconButton>
            )}
          </Box>
          {entry.type === "pinned_message" && (
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ display: "block", pl: 2, mt: 0.5 }}
            >
              The tree is a JSON document pinned in that store&rsquo;s channel. Never
              delete the pinned file or pin anything else. On Discord the tree
              has to fit one attachment; add a GitHub or GitLab copy for
              anything but small trees.
            </Typography>
          )}
          {entry.type === "github_repo" && (
            <Box
              sx={{ display: "flex", alignItems: "flex-start", gap: 1, mt: 1 }}
            >
              <ConfigTextField
                label="Repository"
                value={(entry.github_repo ?? { repo: "" }).repo}
                onChange={(e) =>
                  updateGitHubRepo(entryIndex, "repo", e.target.value)
                }
                helperText="Format: username/repository-name"
                required
                style={{ flex: 1 }}
              />
              <ConfigTextField
                label="Commit/Branch"
                value={(entry.github_repo ?? { commit: "master" }).commit}
                onChange={(e) =>
                  updateGitHubRepo(entryIndex, "commit", e.target.value)
                }
                width={200}
              />
              <ConfigTextField
                label="Access Token"
                value={(entry.github_repo ?? { access_token: "" }).access_token}
                onChange={(e) =>
                  updateGitHubRepo(entryIndex, "access_token", e.target.value)
                }
                type="password"
                required
                style={{ flex: 1 }}
              />
              <Button
                variant="outlined"
                component="a"
                size="large"
                href="https://github.com/settings/personal-access-tokens/new"
                target="_blank"
                rel="noopener noreferrer"
                sx={{ textTransform: "none" }}
              >
                Get Token
              </Button>
            </Box>
          )}
          {entry.type === "gitlab_repo" && (
            <Box
              sx={{ display: "flex", alignItems: "flex-start", gap: 1, mt: 1 }}
            >
              <ConfigTextField
                label="Project"
                value={(entry.gitlab_repo ?? { repo: "" }).repo}
                onChange={(e) =>
                  updateGitlabRepo(entryIndex, "repo", e.target.value)
                }
                helperText="Format: group/project-name"
                required
                style={{ flex: 1 }}
              />
              <ConfigTextField
                label="Branch"
                value={(entry.gitlab_repo ?? { branch: "main" }).branch}
                onChange={(e) =>
                  updateGitlabRepo(entryIndex, "branch", e.target.value)
                }
                width={150}
              />
              <ConfigTextField
                label="Access Token"
                value={(entry.gitlab_repo ?? { access_token: "" })
                  .access_token}
                onChange={(e) =>
                  updateGitlabRepo(entryIndex, "access_token", e.target.value)
                }
                type="password"
                required
                style={{ flex: 1 }}
              />
              <ConfigTextField
                label="GitLab URL (optional)"
                value={(entry.gitlab_repo ?? { url: "" }).url}
                onChange={(e) =>
                  updateGitlabRepo(entryIndex, "url", e.target.value)
                }
                helperText="A private installation; empty means gitlab.com"
                style={{ flex: 1 }}
              />
            </Box>
          )}
        </Box>
      ))}
      <Button
        startIcon={<Add />}
        onClick={() =>
          onUpdate("metadata", [
            ...filesystem.metadata,
            { type: "pinned_message" },
          ])
        }
        variant="outlined"
        size="small"
        sx={{ textTransform: "none", mb: 2 }}
      >
        Add Metadata Copy
      </Button>

      <Typography variant="subtitle2" sx={{ mb: 1 }}>
        Mirrors
      </Typography>
      <Box sx={{ display: "flex", alignItems: "flex-start", gap: 1, mb: 1 }}>
        <FormControl
          size="small"
          sx={{ minWidth: 300, flex: 1 }}
          error={mirrorErrors.length > 0}
        >
          <InputLabel>Mirror Stores</InputLabel>
          <Select
            multiple
            value={filesystem.mirrors.filter((name) =>
              mirrorCandidates.some((s) => s.name === name)
            )}
            label="Mirror Stores"
            input={<OutlinedInput label="Mirror Stores" />}
            onChange={(e) =>
              onUpdate(
                "mirrors",
                (typeof e.target.value === "string"
                  ? e.target.value.split(",")
                  : e.target.value) as string[]
              )
            }
            renderValue={(selected) => (
              <Box sx={{ display: "flex", flexWrap: "wrap", gap: 0.5 }}>
                {(selected as string[]).map((value) => (
                  <Chip key={value} label={value} size="small" />
                ))}
              </Box>
            )}
          >
            {mirrorCandidates.map((store) => (
              <MenuItem key={store.name} value={store.name}>
                {storeLabel(store)}
              </MenuItem>
            ))}
          </Select>
          {mirrorErrors.length > 0 && (
            <Typography variant="caption" color="error" sx={{ pl: 2 }}>
              {mirrorErrors.join("; ")}
            </Typography>
          )}
        </FormControl>
      </Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1, pl: 2 }}>
        Every file lands in the primary and is copied to each mirror. A
        mirror in another backend, or with smaller messages, receives a
        re-uploaded copy. Swapping primary and a mirror later is a config
        change.
      </Typography>

      {filesystem.mirrors.length > 0 && (
        <>
          <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1, mb: 1 }}>
            <FormControl size="small" sx={{ minWidth: 200 }}>
              <InputLabel>Copy Mode</InputLabel>
              <Select
                value={filesystem.mode}
                label="Copy Mode"
                onChange={(e) =>
                  onUpdate("mode", e.target.value as MirrorMode)
                }
              >
                <MenuItem value="auto">Auto (recommended)</MenuItem>
                <MenuItem value="forward">Forward only</MenuItem>
                <MenuItem value="reupload">Re-upload only</MenuItem>
              </Select>
            </FormControl>
            <FormControl size="small" sx={{ minWidth: 200 }}>
              <InputLabel>Sync</InputLabel>
              <Select
                value={filesystem.sync}
                label="Sync"
                onChange={(e) => onUpdate("sync", e.target.value as SyncMode)}
              >
                <MenuItem value="inline">Inline (during the upload)</MenuItem>
                <MenuItem value="background">Background queue</MenuItem>
                <MenuItem value="tee">Tee (every store at once)</MenuItem>
              </Select>
            </FormControl>
          </Box>
          <Typography
            variant="body2"
            color="text.secondary"
            sx={{ mb: 1, pl: 2 }}
          >
            &quot;Auto&quot; copies server-side between Telegram channels
            (no bandwidth) and re-uploads where that is impossible.
            &quot;Inline&quot; makes an upload wait for its copies;
            &quot;Background&quot; queues the file and a worker copies it
            afterwards; &quot;Tee&quot; feeds a re-uploading mirror from
            the upload itself, so primary and mirror fill at the same time
            without a read-back or a cache, and the upload runs at the pace
            of the slowest store. Background is preselected as soon as a
            mirror re-uploads, since a client would otherwise time out
            waiting for a large upload to pass through the mirror a second
            time. The server picks the same default when the config leaves
            Sync out, so it is only written when your choice differs.
            {reuploadMirror && filesystem.sync === "inline" && (
              <>
                {" "}
                <strong>
                  One of the mirrors re-uploads every byte; switch Sync to
                  Background unless uploads may wait for it.
                </strong>
              </>
            )}
          </Typography>
          <FormControlLabel
            label="Read from every store in parallel (pieces of one download are shared between the stores)"
            control={
              <Checkbox
                checked={filesystem.read_parallel}
                onChange={(e) => onUpdate("read_parallel", e.target.checked)}
              />
            }
          />
          {filesystem.read_parallel && (
            <>
              <FormControl size="small" sx={{ minWidth: 300, mb: 1 }}>
                <InputLabel>Stores taking part (empty: all)</InputLabel>
                <Select
                  multiple
                  value={filesystem.read_sources.filter((name) =>
                    [filesystem.primary, ...filesystem.mirrors].includes(name)
                  )}
                  label="Stores taking part (empty: all)"
                  input={<OutlinedInput label="Stores taking part (empty: all)" />}
                  onChange={(e) =>
                    onUpdate(
                      "read_sources",
                      (typeof e.target.value === "string"
                        ? e.target.value.split(",")
                        : e.target.value) as string[]
                    )
                  }
                  renderValue={(selected) => (
                    <Box sx={{ display: "flex", flexWrap: "wrap", gap: 0.5 }}>
                      {(selected as string[]).map((value) => (
                        <Chip key={value} label={value} size="small" />
                      ))}
                    </Box>
                  )}
                >
                  {namedStores
                    .filter((s) =>
                      [filesystem.primary, ...filesystem.mirrors].includes(s.name)
                    )
                    .map((store) => (
                      <MenuItem key={store.name} value={store.name}>
                        {storeLabel(store)}
                      </MenuItem>
                    ))}
                </Select>
              </FormControl>
              <Typography
                variant="body2"
                color="text.secondary"
                sx={{ mb: 1, pl: 2 }}
              >
                Only pays off with more than one store holding the file: a
                fast store simply takes more pieces than a slow one. Small
                reads stay on a single store. Leave a Discord mirror out to
                spare its rate limit for bulk reads.
              </Typography>
            </>
          )}
          {cacheEnabled && (
            <>
              <FormControl size="small" sx={{ minWidth: 260, mb: 1 }}>
                <InputLabel>Write acknowledgement</InputLabel>
                <Select
                  value={filesystem.write_ack}
                  label="Write acknowledgement"
                  disabled={filesystem.strict}
                  onChange={(e) =>
                    onUpdate("write_ack", e.target.value as WriteAck)
                  }
                >
                  <MenuItem value="primary">
                    When the primary store has the file
                  </MenuItem>
                  <MenuItem value="cache">
                    When the local cache has the file (write-back)
                  </MenuItem>
                </Select>
              </FormControl>
              <Typography
                variant="body2"
                color="text.secondary"
                sx={{ mb: 1, pl: 2 }}
              >
                Write-back answers an upload as soon as it is on the local
                disk; a worker then fills the primary and every mirror at
                once from that copy. Until the primary has it, the file
                exists only on this host: a lost disk in that window loses
                the version. Leave it on &quot;primary&quot; for a
                single-disk host holding the only copy of irreplaceable
                data. Not available with Strict.
              </Typography>
            </>
          )}
          <FormControlLabel
            label="Strict: fail an upload when a mirror copy fails (needs inline sync)"
            control={
              <Checkbox
                checked={filesystem.strict}
                disabled={filesystem.sync !== "inline"}
                onChange={(e) => onUpdate("strict", e.target.checked)}
              />
            }
          />
          {needsSharedStore && (
            <FormControlLabel
              label="Allow a mirror that is the primary of another file system"
              control={
                <Checkbox
                  checked={filesystem.allow_shared_store}
                  onChange={(e) =>
                    onUpdate("allow_shared_store", e.target.checked)
                  }
                />
              }
            />
          )}
        </>
      )}
    </Box>
  );
}
