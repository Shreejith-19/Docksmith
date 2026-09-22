"""
Isolation primitive — single mechanism for both RUN (build) and `docksmith run`.

Uses:
  - chroot(2)              : filesystem root isolation
  - unshare(NEWPID|NEWUTS) : process and hostname namespace isolation
  - ctypes → libc          : direct syscall access from Python

run_isolated(rootfs, cmd, env, workdir) → int (exit code)
"""

import ctypes
import ctypes.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

# Linux namespace flags (from <sched.h>)
CLONE_NEWNS  = 0x00020000
CLONE_NEWUTS = 0x04000000
CLONE_NEWPID = 0x20000000

MS_NOSUID = 2
MS_NODEV  = 4
MS_NOEXEC = 8


def _libc():
    name = ctypes.util.find_library("c") or "libc.so.6"
    return ctypes.CDLL(name, use_errno=True)


def run_isolated(
    rootfs: str,
    cmd: List[str],
    env: Optional[Dict[str, str]] = None,
    workdir: str = "/",
) -> int:
    """
    Execute cmd inside rootfs with full filesystem isolation.
    Requires Linux + root (sudo).
    Returns exit code.
    """
    if not sys.platform.startswith("linux"):
        print("Error: isolation requires Linux.", file=sys.stderr)
        sys.exit(1)

    rootfs  = str(Path(rootfs).resolve())
    workdir = workdir or "/"
    env     = env or {}

    child_env = _build_env(env)
    shell     = _find_shell(rootfs)

    # Wrap in shell if command contains shell syntax
    if len(cmd) == 1 and _needs_shell(cmd[0]):
        actual_cmd = [shell, "-c", cmd[0]]
    else:
        actual_cmd = list(cmd)

    def preexec_fn():
        libc = _libc()

        # 1. Unshare namespaces — non-fatal if no CAP_SYS_ADMIN
        flags = CLONE_NEWPID | CLONE_NEWUTS | CLONE_NEWNS
        libc.unshare(ctypes.c_int(flags))

        # 2. Mount /proc inside rootfs ONLY if proc dir exists
        #    We do NOT mount it so the snapshot won't see /proc files
        #    This avoids the flood of "skipping /proc/..." warnings
        # (intentionally skipped)

        # 3. chroot into assembled rootfs
        try:
            os.chroot(rootfs)
        except PermissionError:
            sys.stderr.write(
                "Error: chroot() requires root.\n"
                "  Run with: sudo python3 -m docksmith ...\n"
            )
            os._exit(126)
        except Exception as e:
            sys.stderr.write(f"Error: chroot failed: {e}\n")
            os._exit(1)

        # 4. Set working directory inside new root
        try:
            os.chdir(workdir)
        except Exception:
            try:
                os.chdir("/")
            except Exception:
                pass

    try:
        proc = subprocess.Popen(
            actual_cmd,
            env=child_env,
            preexec_fn=preexec_fn,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        proc.wait()
        return proc.returncode
    except FileNotFoundError:
        print(
            f"Error: executable not found inside container: {actual_cmd[0]}\n"
            f"  Make sure the binary exists inside the image rootfs.",
            file=sys.stderr,
        )
        return 127
    except Exception as e:
        print(f"Error: failed to start container process: {e}", file=sys.stderr)
        return 1


def _build_env(image_env: Dict[str, str]) -> Dict[str, str]:
    """Clean environment for the container — no host env leaks."""
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "TERM": os.environ.get("TERM", "xterm"),
    }
    env.update(image_env)
    return env


def _find_shell(rootfs: str) -> str:
    """Find a usable shell inside the rootfs."""
    for sh in ("/bin/sh", "/bin/bash", "/usr/bin/sh", "/usr/bin/bash"):
        full = rootfs + sh
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return sh
    return "/bin/sh"


def _needs_shell(cmd: str) -> bool:
    """Does this command string require a shell to interpret?"""
    shell_chars = set("|&;<>()$`\\\"'{}[]!#~*?")
    return any(c in cmd for c in shell_chars) or any(c.isspace() for c in cmd)
