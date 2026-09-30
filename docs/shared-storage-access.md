# Shared storage access from a client

[English](shared-storage-access.md) | [简体中文](shared-storage-access.zh.md)

Determined tasks use the shared host/container mappings in the compute profile. The optional storage client lets a machine that does not mount those filesystems check, stage, and fetch files through a login node. It does not upload source through Determined.

An already prepared workload needs only Determined authentication to launch. SSH is needed only for `storage_check`, `storage_sync`, or `storage_fetch` when the selected path is not locally mounted.

## Configure access separately

Keep storage access in its own YAML file and pass it with `--storage-config PATH` or `DETERMINED_COMPUTE_STORAGE`. It does not change the compute-profile fingerprint or task identity.

```yaml
mode: auto                 # auto, local, or ssh
local_mounts:
  - host_path: /SSD
    local_path: /Volumes/cluster-ssd
ssh:
  host: cluster-login      # preferably a Host alias from ~/.ssh/config
  # user: alice            # omit when the SSH config supplies it
  # port: 22
  # identity_file: ~/.ssh/id_ed25519
  # config_file: ~/.ssh/config
  auth: openssh            # openssh, password, or keyring
  # keyring_service: determined-compute
connect_timeout_seconds: 10
timeout_seconds: 120
preserve_permissions: true # set false only for a verified incompatible filesystem
# snapshots:               # optional; see "Publish a code snapshot"
#   root: /SSD/project/snapshots  # container path below a writable mount
#   link_mode: auto        # auto, reflink, hardlink, or copy
```

`local_mounts` maps a compute-profile host root or subdirectory to an absolute path on the client. `auto` uses an explicit mapping or the same host root when that root exists locally, then falls back to configured SSH. `local` requires local access; `ssh` always accesses the login node. The `shared_dir` argument is always a container-namespace path. The service first resolves the authoritative compute-profile mount, then translates to its cluster host path and the chosen client backend.

Without a storage file, the service uses empty `auto` configuration. A storage operation with neither local access nor SSH returns `configuration_required`; compute planning and launch remain available. `connect_timeout_seconds` accepts 1–120 seconds and defaults to 10; `timeout_seconds` accepts 1–3600 seconds and defaults to 120. `preserve_permissions` is a boolean and defaults to `true`.

The CLI and MCP server also use this file when planning and launching: it decides which
launch paths can be checked locally and how directories named in a request's
`create_directories` are created, through the local view or with `mkdir -p` on the SSH
login node. These launch paths trust only an explicit `local_mounts` entry, without
falling back to the host root when that entry is unavailable, or a same-named host root
that is detected as a mount point on this machine (a same-filesystem bind mount and a
directory below a mount are not detected; map such a root to itself with `local_mounts`).
See [launch-path checks](compute-service.md#launch-path-checks). Older releases
reject unknown keys, so add `snapshots` only after every process that reads this file,
including every MCP server that shares the task database, runs a release that supports it.

Use the login node as `ssh.host`. A gateway is only an optional `ProxyJump`; do not mistake it for the storage endpoint. Prefer an SSH alias so user, identity, port, jump route, and host-key policy stay in `~/.ssh/config`:

```sshconfig
Host cluster-gateway
  HostName gateway.example.org
  User alice

Host cluster-login
  HostName login.internal.example.org
  User alice
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  # ProxyJump cluster-gateway
```

OpenSSH documents that `ProxyJump` connects through the jump host and that destination and jump-host settings should live in `~/.ssh/config`. Verify a new host key interactively against a trusted fingerprint before automation. The storage backend enforces `StrictHostKeyChecking=yes`, so the verified key must already be in `known_hosts`; never use `StrictHostKeyChecking=no`. See the official [`ssh_config(5)`](https://man.openbsd.org/ssh_config.5).

## Authentication choices

### SSH key and agent (`auth: openssh`)

The service inherits an existing agent; it does not start or unlock one. Check the current agent, and start one only when the environment has no usable socket:

```bash
if [ -z "${SSH_AUTH_SOCK:-}" ]; then
  eval "$(ssh-agent -s)"
fi
ssh-add -l
ssh-add ~/.ssh/id_ed25519
```

`ssh-add` requires a running agent and `SSH_AUTH_SOCK`, and asks for an encrypted key's passphrase on the user's terminal. See [`ssh-agent(1)`](https://man.openbsd.org/ssh-agent.1) and [`ssh-add(1)`](https://man.openbsd.org/ssh-add).

On macOS, use Apple's system binary when storing the key passphrase in Keychain:

```bash
/usr/bin/ssh-add --apple-use-keychain ~/.ssh/id_ed25519
```

Apple's OpenSSH launch agent publishes `SSH_AUTH_SOCK`; GitHub's official macOS guidance notes that `--apple-use-keychain` belongs to `/usr/bin/ssh-add`. See [Apple's launch-agent source](https://github.com/apple-oss-distributions/OpenSSH/blob/main/com.openssh.ssh-agent.plist) and [GitHub's macOS note](https://docs.github.com/en/authentication/troubleshooting-ssh/error-ssh-add-illegal-option----apple-use-keychain).

If Codex starts the MCP server, forward the socket from the local Codex environment:

```toml
[mcp_servers.determined-compute]
env_vars = ["SSH_AUTH_SOCK"]
```

Official Codex documentation defines `env_vars` as the allowlist forwarded to a stdio MCP server. A GUI client must itself inherit `SSH_AUTH_SOCK`: quit any already-running instance and launch a new one from the prepared terminal, or provide the socket through the client's launcher environment before restarting the MCP server. See [Codex MCP configuration](https://developers.openai.com/codex/mcp).

### Password (`auth: password`)

Put login-node credentials in the existing secrets file, never in the storage profile, MCP arguments, task request, or report:

```dotenv
SSH_USERNAME=alice
SSH_PASSWORD=replace-me
```

Restrict the file to the user. Password authentication is separate from `DET_*` credentials, and the internal askpass helper must never expose the password in process arguments or output.

Select a non-default secrets file with the existing global `--secrets-file` option or `DETERMINED_COMPUTE_SECRETS`. `SSH_USERNAME` can supply the user when `ssh.user` is omitted; if both are present, they must match.

### OS keyring (`auth: keyring`)

Install the optional credential backend in the service environment:

```bash
python -m pip install -e '.[mcp,keyring]'
```

Set both `ssh.user` and `ssh.keyring_service`, then store the login-node password for that username:

```bash
python -m keyring set determined-compute alice
```

The selected keyring backend must already be unlocked and available to the service process. The keyring stores an account password. It does not unlock a private key; load a key passphrase into `ssh-agent` with `ssh-add`. The [keyring documentation](https://keyring.readthedocs.io/en/stable/) describes backend selection, diagnostics, and `get_password`/`set_password` behavior.

## Check, preview, and transfer

The Python boundary is `StorageService.check(path)`, `sync(local_dir, shared_dir, dry_run=True)`, and `fetch(shared_dir, local_dir, dry_run=True)`. CLI and MCP operations use the same path rules. `check` reports the selected backend, container path, translated host path, optional local path, existence, type, and read/write access. Sync/fetch copy directory contents and report the operation, backend, resolved endpoints, host path, exclusions, effective `preserve_permissions`, dry-run/completion state, and bounded output with a `truncated` flag. Local results include the mapped path; SSH results expose only the configured host alias, never the user, identity path, or credential.

```bash
export DETERMINED_COMPUTE_PROFILE=/path/to/compute-profile.yaml
export DETERMINED_COMPUTE_STORAGE=/path/to/storage-access.yaml

determined-compute storage-check /SSD/project/run

# Preview by default; no files change.
determined-compute storage-sync "$PWD/repo" /SSD/project/run/repo
determined-compute storage-fetch /SSD/project/run/results "$PWD/results"

# Transfer only after reviewing the preview.
determined-compute storage-sync "$PWD/repo" /SSD/project/run/repo --execute
determined-compute storage-fetch /SSD/project/run/results "$PWD/results" --execute
```

MCP exposes `storage_check(path)`, `storage_sync(local_dir, shared_dir, dry_run=True)`, and `storage_fetch(shared_dir, local_dir, dry_run=True)`. Preview is the default; pass `dry_run=false` only after reviewing resolved source, destination, transport, and exclusions. The CLI uses `--execute` for the same authorization. The read-only consultation worker has no SSH/storage credentials or tools and must never be asked to test them.

Client-side `local_dir` values must be absolute paths. Transfer uses `rsync -a --safe-links --mkpath --itemize-changes`; SSH transfers also use secluded arguments (`-s`). With the default `preserve_permissions: true`, archive mode preserves permissions, owner, group, and directory times. Set it to `false` only for a verified mount that rejects those operations; the backend then adds `--no-owner --no-group --no-perms --omit-dir-times` for local and SSH transfers. Do not disable preservation globally, infer it from a storage name, or retry automatically with different flags.

Dry-run creates no destination directories and returns bounded preview output. Transfer never adds `--delete`, `--copy-links`, or a password to the process arguments. Sync excludes VCS metadata, local caches, SSH/cloud configuration, common environment/credential/key files, and the exact configured secrets file when it lies under the source. These exclusions are defense in depth, not a complete secret scanner; review project-specific names before execution.

The absence of `--delete` means extra destination files remain. An executed transfer can still replace same-named destination files according to normal rsync archive-mode rules; review the itemized dry-run output first.

Rsync exit code 23 means some files or attributes were not transferred and the destination may already contain a partial copy. Inspect the bounded output, correct the filesystem/configuration cause, run a fresh preview, and review it before executing again. The service must not blindly retry a failed transfer.

Sync requires an existing local source directory, and its shared destination must be below a mount root rather than equal to it. A local mapped root must already exist before execution can create nested destinations. Fetch accepts a local output directory or a new directory whose parent already exists. Local mappings are canonicalized so symlinks cannot escape their configured roots; the operator remains responsible for the policy and permissions of the configured remote SSH host.

Confirm rsync 3.2.3 or newer is installed on both the client and login node because the backend always uses `--mkpath`. The official [rsync manual](https://rsync.samba.org/ftp/rsync/rsync.1) also explains that `-s` sends arguments through the protocol rather than the remote shell.

## Publish a code snapshot

`storage_snapshot(repo_dir, revision="HEAD", include=None, exclude=None, dry_run=True, verify=False)`
and `determined-compute snapshot REPO_DIR [--revision REV] [--include PATH]... [--exclude GLOB]... [--execute] [--verify]`
publish the exact tracked content of one git commit as a read-only working directory on
shared storage, so repeated jobs reuse one copy instead of each copying the workspace.
Configure `snapshots.root`, a container path below a writable mount but not the mount
root, and optionally `snapshots.link_mode`. This release needs a local, writable view of
the root; SSH-only access returns `configuration_required`. Preview is the default, as for
transfers.

`repo_dir` is the top level of a git work tree on the machine running the service. The
revision is resolved to a full commit, and files are read from the git object database,
not the working tree, so without includes the snapshot equals that commit. `include` adds
working-tree files or directories, such as generated or untracked files, and overrides
tracked paths with their working-tree content; each must stay inside the repository and
cannot be or traverse a symlink. Inside an included directory, a symlink that git tracks
with the same target is kept from the revision, and any other symlink or special file is
`invalid_include` unless an exclusion below skips its directory. Executable bits are
kept. Relative symlinks that stay inside the tree, also when resolved through the
snapshot's other symlinks, are recreated. A symlink that leaves the tree, directly or
through such a chain, or that loops, fails with `unsafe_symlink`; a preview already
reports it. Submodules are skipped and reported, and a Git LFS pointer produces a warning.
A `.git` file or directory inside an included directory, as in a git worktree or a
submodule checkout, is skipped and reported with reason `git_metadata`; naming a path
inside `.git` as an include is `invalid_include`.

Secret-like files are left out under two kinds of rules. Credential stores are never
snapshotted: the configured secrets file when it lies in the repository, `.ssh/`, `.aws/`,
`.config/gcloud/`, `.netrc`, `.npmrc`, `.pypirc`, and `id_rsa*`, `id_ed25519*`, and
`id_ecdsa*`. A tracked one is excluded, and an include that reaches one, by name or inside
an included directory, fails with `secret_like_include`. Name heuristics cover the other
transfer exclusions above (such as `.env*`, `*.env`, `*.key`, `*.pem`, `.secrets*`, and
`credentials/`), any path component containing `credential` or `secret`, and files named
`token`, `.token`, or `*.token`. They exclude tracked files and files found in an included
directory, but an include that names the file itself overrides them, for example for a
`secrets.py` module: its include source in the manifest records `included_despite` with the
rule, and a `secret_like_included` warning names it. Confirm that such a file holds no
secret before publishing.

Cache directories and `*.pyc` from the same exclusion list are also left out. Inside an
included directory they apply only below the directory named, so including `mylib/cache`
restores a `cache/` package while its `__pycache__/` stays out, and an include that names a
file always restores it. `exclude` adds rsync-style patterns matched per path component
against the repository-relative path, also inside included directories: a trailing `/`
matches directories and a leading `/` anchors at the repository root. An excluded
directory inside an included directory is not walked and is reported once as `path/`.
Every excluded path is reported with its `reason` and `rule`. Review the preview before
publishing to shared storage.

`content_id` is the SHA-256 of the canonical list of files (path, SHA-256, size, and mode)
and symlink targets, and the workdir is `<root>/trees/<content_id>`, so identical content is
published once whichever revision produced it. A manifest at
`<root>/manifests/<snapshot_key>.json` records `schema_version`, the revision, tree,
sources, files, symlinks, exclusions, skipped entries, warnings, and `created_utc`. It never
records a remote URL. Repeating a snapshot returns the existing manifest with the same
`manifest_sha256`; another revision with the same content gets its own manifest and shares
the tree.

Files are stored once under `<root>/objects/sha256/`, executables separately with a `.x`
suffix because hard links share one mode, and cloned or linked into each tree. `auto` uses
reflink when the filesystem supports it and copies otherwise. `hardlink` is used only when
configured: it saves space without reflink support, but each tree file is then the same
inode as its object and as that file in every other tree, so one in-place write changes
all of them and later snapshots too. A file that cannot be cloned or linked, for example
at a link limit or across devices, is copied; the result reports `link_mode` and
`link_fallbacks`. `copy` skips the object store and deduplicates whole trees only.
Objects, trees, and manifests are written under temporary names, published without
replacing an existing name, and never modified or deleted afterwards, and concurrent
snapshots of the same content publish one tree. Only on a filesystem that supports
neither hard links nor `renameat2` with `RENAME_NOREPLACE` can a manifest published
concurrently for the same `snapshot_key` be replaced by an equivalent one that differs in
`created_utc`; each caller then reports the bytes it reads back.

An existing tree is checked by file size before reuse. With `verify`, the whole tree is
checked instead: no unrecorded entries, the type of each entry, symlink targets,
executable bits, sizes, and the SHA-256 of every file; each existing object is also hashed
before a new tree reuses it. A mismatch returns `snapshot_corrupt` and is never repaired.
Trees are read-only only through mode bits, so a job must write under its `output_dir`;
a task running as root on storage without root squashing, or the owner after `chmod`, can
still change them. There, prefer a reflink-capable filesystem or `link_mode: copy`, and
use `verify` before reusing a tree. To recover from `snapshot_corrupt`, remove the damaged
tree or object (make its directory writable first) and publish again; with hard links,
every tree that shares a damaged object is damaged too.

The result reports `dry_run`, `revision`, `tree`, `content_id`, `snapshot_key`, `workdir`
(a container path), `host_path`, `local_path`, `manifest_path` and `manifest_sha256` (null
in a preview unless already published), `existing` (the manifest already existed),
`tree_existing`, the `files`, `symlinks`, and `bytes` totals, `new_objects`, `new_bytes`,
`link_mode`, `link_fallbacks`, `excluded`, `skipped`, `warnings`, and `request_fields`,
whose `workdir` and `code_revision` go directly into a compute request. `code_revision` is
the commit, or `<commit>+<snapshot_key>` when includes added or replaced files, because the
content then differs from the commit; it also names the manifest. The `content_id` in
`workdir` identifies the content itself.

```bash
determined-compute snapshot "$PWD"                   # preview; writes nothing
determined-compute snapshot "$PWD" --execute         # publish
determined-compute snapshot "$PWD" --revision v1.2 --include generated/ --exclude '*.log'
```

## Reuse an SSH connection

Connection multiplexing reduces repeated authentication. Put the socket in a directory writable only by the user:

```sshconfig
Host cluster-login
  ControlMaster auto
  ControlPath ~/.ssh/controlmasters/%C
  ControlPersist 10m
```

```bash
install -d -m 700 ~/.ssh/controlmasters
ssh -MNf cluster-login
ssh -O check cluster-login
# Later, when no more storage operations need it:
ssh -O exit cluster-login
```

OpenSSH recommends a private `ControlPath` containing `%h/%p/%r` or `%C`; `-M` creates a master, `-N` runs no remote command, and `-f` backgrounds after authentication. This persists the login-node transport for later storage operations. It is unrelated to GPU activity and does not keep Determined tasks or shells alive. See [`ssh_config(5)`](https://man.openbsd.org/ssh_config.5) and [`ssh(1)`](https://man.openbsd.org/ssh.1).

Compute-profile `read_only: true` also applies to storage operations: uploads are rejected, while checks and downloads remain allowed. A download destination cannot map back into read-only shared storage. `storage_check` reports `read_only`; its `writable` flag combines filesystem access with profile policy.
