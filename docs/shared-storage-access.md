# Shared storage access from a client

[English](shared-storage-access.md) | [简体中文](shared-storage-access.zh.md)

Determined tasks use the shared host/container mappings in the compute profile. The optional storage client lets a machine that does not mount those filesystems check, stage, and fetch files through a login node. It does not upload source through Determined.

An already prepared workload needs only Determined authentication to launch. SSH is needed only for `storage_check`, `storage_sync`, or `storage_fetch` when the selected path is not locally mounted.

## Configure access separately

Keep storage access in its own YAML file and pass it with `--storage-config PATH` or `DETERMINED_COMPUTE_STORAGE`. It does not change the compute profile or how tasks are submitted.

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
```

`local_mounts` maps a compute-profile host root or subdirectory to an absolute path on the client. `auto` uses an explicit mapping or the same host root when that root exists locally, then falls back to configured SSH. `local` requires local access; `ssh` always accesses the login node. The `shared_dir` argument is always a container-namespace path. The service first resolves the authoritative compute-profile mount, then translates to its cluster host path and the chosen client backend.

Without a storage file, the service uses empty `auto` configuration. A storage operation with neither local access nor SSH returns `configuration_required`; compute planning and launch remain available. `connect_timeout_seconds` accepts 1–120 seconds and defaults to 10; `timeout_seconds` accepts 1–3600 seconds and defaults to 120. `preserve_permissions` is a boolean and defaults to `true`.

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

The Python boundary is `StorageService.check(path)`, `sync(local_dir, shared_dir, dry_run=True)`, and `fetch(shared_dir, local_dir, dry_run=True)`. The MCP storage tools call these methods. `check` reports the selected backend, container path, translated host path, optional local path, existence, type, and read/write access. Sync/fetch copy directory contents and report the operation, backend, resolved endpoints, host path, exclusions, effective `preserve_permissions`, dry-run/completion state, and bounded output with a `truncated` flag. Local results include the mapped path; SSH results expose only the configured host alias, never the user, identity path, or credential.

MCP exposes `storage_check(path)`, `storage_sync(local_dir, shared_dir, dry_run=True)`, and `storage_fetch(shared_dir, local_dir, dry_run=True)`. Preview is the default; pass `dry_run=false` only after reviewing resolved source, destination, transport, and exclusions.

Client-side `local_dir` values must be absolute paths. Transfer uses `rsync -a --safe-links --mkpath --itemize-changes`; SSH transfers also use secluded arguments (`-s`). With the default `preserve_permissions: true`, archive mode preserves permissions, owner, group, and directory times. Set it to `false` only for a verified mount that rejects those operations; the backend then adds `--no-owner --no-group --no-perms --omit-dir-times` for local and SSH transfers. Do not disable preservation globally, infer it from a storage name, or retry automatically with different flags.

Dry-run creates no destination directories and returns bounded preview output. Transfer never adds `--delete`, `--copy-links`, or a password to the process arguments. Sync excludes VCS metadata, local caches, SSH/cloud configuration, common environment/credential/key files, and the exact configured secrets file when it lies under the source. These exclusions are defense in depth, not a complete secret scanner; review project-specific names before execution.

The absence of `--delete` means extra destination files remain. An executed transfer can still replace same-named destination files according to normal rsync archive-mode rules; review the itemized dry-run output first.

Rsync exit code 23 means some files or attributes were not transferred and the destination may already contain a partial copy. Inspect the bounded output, correct the filesystem/configuration cause, run a fresh preview, and review it before executing again. The service must not blindly retry a failed transfer.

Sync requires an existing local source directory, and its shared destination must be below a mount root rather than equal to it. A local mapped root must already exist before execution can create nested destinations. Fetch accepts a local output directory or a new directory whose parent already exists. Local mappings are canonicalized so symlinks cannot escape their configured roots; the operator remains responsible for the policy and permissions of the configured remote SSH host.

Confirm rsync 3.2.3 or newer is installed on both the client and login node because the backend always uses `--mkpath`. The official [rsync manual](https://rsync.samba.org/ftp/rsync/rsync.1) also explains that `-s` sends arguments through the protocol rather than the remote shell.

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
