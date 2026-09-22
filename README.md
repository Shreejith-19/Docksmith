# Docksmith

A simplified Docker-like build and runtime system built from scratch in Python.
Implements a build engine, deterministic cache, and container runtime using Linux
`chroot` and namespaces — no Docker, no runc, no containerd.

---

## Requirements

| Requirement | Detail |
|---|---|
| OS | Linux only (Ubuntu 20.04+ recommended) |
| Python | 3.8 or newer |
| Privileges | `sudo` required for `build` and `run` (chroot needs root) |
| Docker | NOT required — base images imported directly from rootfs tars |

> **Windows users:** Use WSL2 (Ubuntu 22.04) or a VirtualBox/VMware Linux VM.
> The `chroot(2)` and namespace syscalls are Linux kernel features only.

---

## Project Structure

```
docksmith/
├── setup.py                  ← install entry point
├── setup_base_image.sh       ← one-time base image import (no Docker needed)
├── demo.sh                   ← runs all 8 demo scenarios
├── README.md
│
├── sample_app/
│   ├── Docksmithfile         ← uses all 6 instructions
│   └── run.sh                ← sample app script
│
└── docksmith/                ← Python package
    ├── __init__.py
    ├── __main__.py           ← enables: python -m docksmith
    ├── cli.py                ← argument parsing, 5 subcommands
    ├── parser.py             ← Docksmithfile parser
    ├── store.py              ← ~/.docksmith/ state management
    ├── layers.py             ← deterministic tar, digest, extraction
    ├── cache.py              ← cache key computation
    ├── builder.py            ← build engine
    ├── runtime.py            ← docksmith run
    ├── isolation.py          ← chroot + Linux namespaces
    └── importer.py           ← import rootfs tar into local store
```

State directory created automatically at `~/.docksmith/`:

```
~/.docksmith/
├── images/     ← JSON manifests (one per image)
├── layers/     ← content-addressed tar files named by sha256
└── cache/      ← cache key → layer digest index
```

---

## Installation

```bash
git clone <your-repo>
cd docksmith
pip install -e .
```

Verify:

```bash
python3 -m docksmith --help
```

---

## One-Time Setup: Import a Base Image

Docksmith **never downloads anything** during build or run.
You must import a base image once before building.

**No Docker needed** — import directly from the Alpine minirootfs tar:

```bash
# Download Alpine minirootfs (one-time, needs internet)
wget https://dl-cdn.alpinelinux.org/alpine/v3.18/releases/x86_64/alpine-minirootfs-3.18.0-x86_64.tar.gz

# Import directly into Docksmith store
python3 -m docksmith import alpine-minirootfs-3.18.0-x86_64.tar.gz alpine:3.18

# Verify
python3 -m docksmith images
```

After this, **no internet access is needed** for any build or run operation.

If you have Docker available (optional):

```bash
docker pull alpine:3.18
docker save alpine:3.18 -o alpine.tar
python3 -m docksmith import alpine.tar alpine:3.18
```

---

## CLI Reference

> **Important:** Always use `sudo` for `build` and `run`.
> Always run from the project root (`~/Docksmith`), never from inside the `docksmith/` package folder.

### `docksmith build`

```bash
sudo python3 -m docksmith build -t myapp:latest ./sample_app
sudo python3 -m docksmith build -t myapp:latest --no-cache ./sample_app
```

Reads `Docksmithfile` from the context directory, executes all steps in isolation,
writes the image manifest to `~/.docksmith/images/`.
Prints step number, `[CACHE HIT]` / `[CACHE MISS]`, and timing per step.

### `docksmith images`

```bash
python3 -m docksmith images
```

Lists all images. Columns: NAME, TAG, ID (first 12 chars of digest), CREATED.

### `docksmith rmi`

```bash
python3 -m docksmith rmi myapp:latest
```

Removes the image manifest and all associated layer files from disk.

### `docksmith run`

```bash
sudo python3 -m docksmith run myapp:latest
sudo python3 -m docksmith run -e GREETING=Howdy myapp:latest
sudo python3 -m docksmith run myapp:latest sh -c "echo hello"
```

Assembles the image filesystem, runs the process in full isolation, waits for exit.
- `-e KEY=VALUE` overrides or adds an environment variable (repeatable)
- Trailing arguments override the image CMD

### `docksmith import`

```bash
python3 -m docksmith import <image.tar> <name:tag>
```

Imports a base image into the local store. Accepts:
- Raw rootfs tars: `alpine-minirootfs-3.18.0-x86_64.tar.gz`
- Docker-saved tars: output of `docker save`

---

## Docksmithfile Reference

All 6 instructions must appear in the sample app. Only these 6 are supported —
any other instruction causes an immediate error with the line number.

| Instruction | Example | Produces layer? |
|---|---|---|
| `FROM` | `FROM alpine:3.18` | No |
| `WORKDIR` | `WORKDIR /app` | No |
| `ENV` | `ENV KEY=value` | No |
| `COPY` | `COPY . .` | Yes |
| `RUN` | `RUN sh -c "echo hi"` | Yes |
| `CMD` | `CMD ["sh", "/app/run.sh"]` | No |

**Example Docksmithfile:**

```dockerfile
FROM alpine:3.18

WORKDIR /app

ENV APP_VERSION=1.0.0
ENV GREETING=Hello

COPY . .

RUN sh -c "echo 'Build complete. Version: '${APP_VERSION}"

CMD ["sh", "/app/run.sh"]
```

---

## Build Cache

A cache key is computed before every `COPY` and `RUN` step from:
- Previous layer digest (or base image manifest digest for the first step)
- Full instruction text as written
- Current WORKDIR value
- All ENV pairs sorted lexicographically
- COPY only: SHA-256 of each source file sorted by path

| Situation | Behaviour |
|---|---|
| Cache hit | Reuse stored layer, print `[CACHE HIT]` |
| Cache miss | Execute, store layer, print `[CACHE MISS]` |
| Any miss | All subsequent steps also miss (cascade) |
| `--no-cache` | Skip all cache lookups and writes |
| Layer file missing | Treated as miss, cascade |

---

## Demo Walkthrough

```bash
# 1. Cold build — all CACHE MISS
sudo python3 -m docksmith build -t myapp:latest ./sample_app

# 2. Warm build — all CACHE HIT
sudo python3 -m docksmith build -t myapp:latest ./sample_app

# 3. Partial invalidation — edit a file, steps above it HIT, steps below MISS
echo "# change" >> sample_app/run.sh
sudo python3 -m docksmith build -t myapp:latest ./sample_app

# 4. List images
python3 -m docksmith images

# 5. Run container
sudo python3 -m docksmith run myapp:latest

# 6. ENV override
sudo python3 -m docksmith run -e GREETING=Howdy myapp:latest

# 7. Isolation check — file written inside must NOT appear on host
sudo python3 -m docksmith run myapp:latest
ls /tmp/isolation_test.txt   # must say: No such file or directory

# 8. Remove image
python3 -m docksmith rmi myapp:latest
python3 -m docksmith images
```

---

## Isolation Mechanism

Both `RUN` during build and `docksmith run` use the **same function**: `run_isolated()` in `isolation.py`.

It uses:
- `chroot(2)` — restricts the process filesystem view to the assembled rootfs
- `unshare(CLONE_NEWPID)` — new PID namespace (container gets PID 1, cannot see host processes)
- `unshare(CLONE_NEWUTS)` — new hostname namespace
- `unshare(CLONE_NEWNS)` — new mount namespace

A file written inside the container **cannot appear on the host filesystem**.
This is verified live at demo time (scenario 7 above).

---

## Troubleshooting

**`chroot: Operation not permitted`**
```bash
# Always use sudo for build and run
sudo python3 -m docksmith build -t myapp:latest ./sample_app
```

**`No module named docksmith`**
```bash
# Must be run from project root, not from inside the docksmith/ package folder
cd ~/Docksmith          # correct
pip install -e .
python3 -m docksmith --help
```

**`image 'alpine:3.18' not found`**
```bash
# Import the base image first
python3 -m docksmith import alpine-minirootfs-3.18.0-x86_64.tar.gz alpine:3.18
python3 -m docksmith images
```

**`sudo` can't find the image imported without sudo**
```bash
# The store.py fix handles this automatically via SUDO_USER env var.
# If still failing, import as sudo too:
sudo python3 -m docksmith import alpine-minirootfs-3.18.0-x86_64.tar.gz alpine:3.18
```

**Cache never hits on rebuild**
```bash
# Ensure you are not passing --no-cache
# Ensure source files haven't changed between builds
sudo python3 -m docksmith build -t myapp:latest ./sample_app
```

**`/bin/sh: not found` inside container**
```bash
# The Alpine minirootfs has /bin/sh — make sure it was imported correctly
python3 -m docksmith images
# Should show alpine:3.18 with a valid digest
```
