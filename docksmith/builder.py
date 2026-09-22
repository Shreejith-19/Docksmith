"""
Build engine — executes a Docksmithfile and produces an image manifest.
"""

import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .cache import collect_copy_file_digests, compute_cache_key
from .isolation import run_isolated
from .layers import digest_bytes, extract_layers, make_delta_tar
from .parser import Instruction, parse_docksmithfile
from .store import ImageStore

# Top-level dirs to never snapshot or include in layers
_EXCLUDE_DIRS = {"proc", "sys", "dev", "run", "tmp"}


class Builder:
    def __init__(self, context_dir: str, name: str, tag: str, no_cache: bool = False):
        self.context_dir  = str(Path(context_dir).resolve())
        self.name         = name
        self.tag          = tag
        self.no_cache     = no_cache
        self.store        = ImageStore()
        self.layers:      List[dict]     = []
        self.env:         Dict[str, str] = {}
        self.workdir:     str            = ""
        self.cmd_list:    List[str]      = []
        self.prev_digest: str            = ""
        self.cache_cascade: bool         = False

    def build(self):
        docksmithfile = os.path.join(self.context_dir, "Docksmithfile")
        instructions  = parse_docksmithfile(docksmithfile)
        total_steps   = len(instructions)
        start_wall    = time.monotonic()

        for step_idx, instr in enumerate(instructions, 1):
            print(f"Step {step_idx}/{total_steps} : {instr.op} {instr.raw_args}", end="", flush=True)

            if   instr.op == "FROM":    print(); self._exec_from(instr)
            elif instr.op == "COPY":    self._exec_copy(instr)
            elif instr.op == "RUN":     self._exec_run(instr)
            elif instr.op == "WORKDIR": print(); self._exec_workdir(instr)
            elif instr.op == "ENV":     print(); self._exec_env(instr)
            elif instr.op == "CMD":     print(); self._exec_cmd(instr)

        elapsed  = time.monotonic() - start_wall
        manifest = self._write_manifest()
        short_id = manifest["digest"].replace("sha256:", "")[:12]
        print(f"\nSuccessfully built sha256:{short_id} {self.name}:{self.tag} ({elapsed:.2f}s)")

    # ── instruction handlers ──────────────────────────────────────────────────

    def _exec_from(self, instr: Instruction):
        manifest         = self.store.load_manifest(instr.from_name, instr.from_tag)
        self.layers      = list(manifest.get("layers", []))
        self.env         = {}
        self.workdir     = manifest.get("config", {}).get("WorkingDir", "")
        self.cmd_list    = manifest.get("config", {}).get("Cmd", [])
        self.prev_digest = manifest["digest"]
        for kv in manifest.get("config", {}).get("Env", []):
            if "=" in kv:
                k, v = kv.split("=", 1)
                self.env[k] = v

    def _exec_copy(self, instr: Instruction):
        t0         = time.monotonic()
        file_pairs = self._collect_copy_sources(instr)
        copy_digests = collect_copy_file_digests(self.context_dir, instr.copy_srcs)
        cache_key  = compute_cache_key(
            prev_digest       = self.prev_digest,
            instruction_text  = f"COPY {instr.raw_args}",
            workdir           = self.workdir,
            env_state         = self.env,
            copy_file_digests = copy_digests,
        )

        hit, layer_digest = self._cache_lookup(cache_key)
        if hit:
            print(f" [CACHE HIT] {time.monotonic()-t0:.2f}s")
            self._record_layer(layer_digest, f"COPY {instr.raw_args}")
            return

        dest_pairs   = self._map_copy_dest(file_pairs, instr.copy_dest)
        tar_data     = make_delta_tar(dest_pairs)
        layer_digest = digest_bytes(tar_data)
        self.store.write_layer(layer_digest, tar_data)
        if not self.no_cache:
            self.store.cache_set(cache_key, layer_digest)
        self.cache_cascade = True

        print(f" [CACHE MISS] {time.monotonic()-t0:.2f}s")
        self._record_layer(layer_digest, f"COPY {instr.raw_args}")

    def _exec_run(self, instr: Instruction):
        t0        = time.monotonic()
        cache_key = compute_cache_key(
            prev_digest      = self.prev_digest,
            instruction_text = f"RUN {instr.raw_args}",
            workdir          = self.workdir,
            env_state        = self.env,
        )

        hit, layer_digest = self._cache_lookup(cache_key)
        if hit:
            print(f" [CACHE HIT] {time.monotonic()-t0:.2f}s")
            self._record_layer(layer_digest, f"RUN {instr.raw_args}")
            return

        with tempfile.TemporaryDirectory(prefix="docksmith_run_") as rootfs:
            extract_layers(self.store, [l["digest"] for l in self.layers], rootfs)

            # Create WORKDIR inside rootfs if needed
            if self.workdir:
                os.makedirs(rootfs + self.workdir, exist_ok=True)

            before    = _snapshot(rootfs)
            exit_code = run_isolated(
                rootfs  = rootfs,
                cmd     = [instr.run_cmd],
                env     = self.env,
                workdir = self.workdir or "/",
            )
            if exit_code != 0:
                print(f"\nError: RUN exited with code {exit_code}: {instr.run_cmd}", file=sys.stderr)
                sys.exit(exit_code)

            after   = _snapshot(rootfs)
            changed = _diff_snapshots(before, after, rootfs)
            tar_data     = make_delta_tar(changed) if changed else make_delta_tar([])
            layer_digest = digest_bytes(tar_data)

        self.store.write_layer(layer_digest, tar_data)
        if not self.no_cache:
            self.store.cache_set(cache_key, layer_digest)
        self.cache_cascade = True

        print(f" [CACHE MISS] {time.monotonic()-t0:.2f}s")
        self._record_layer(layer_digest, f"RUN {instr.raw_args}")

    def _exec_workdir(self, instr: Instruction):
        self.workdir = instr.workdir

    def _exec_env(self, instr: Instruction):
        self.env[instr.env_key] = instr.env_val

    def _exec_cmd(self, instr: Instruction):
        self.cmd_list = instr.cmd_list

    # ── helpers ───────────────────────────────────────────────────────────────

    def _cache_lookup(self, cache_key: str) -> Tuple[bool, str]:
        if self.no_cache or self.cache_cascade:
            self.cache_cascade = True
            return False, ""
        stored = self.store.cache_get(cache_key)
        if stored and self.store.layer_exists(stored):
            return True, stored
        self.cache_cascade = True
        return False, ""

    def _record_layer(self, digest: str, created_by: str):
        size = self.store.layer_path(digest).stat().st_size if self.store.layer_exists(digest) else 0
        self.layers.append({"digest": digest, "size": size, "createdBy": created_by})
        self.prev_digest = digest

    def _collect_copy_sources(self, instr: Instruction) -> List[Tuple[str, str]]:
        import glob as _glob
        context = Path(self.context_dir)
        results: Dict[str, str] = {}

        for pattern in instr.copy_srcs:
            hits = _glob.glob(str(context / pattern), recursive=True)
            if not hits:
                print(f"Warning: COPY pattern '{pattern}' matched nothing.", file=sys.stderr)
                continue
            for hit in hits:
                hp = Path(hit)
                if hp.is_file():
                    rel = str(hp.relative_to(context))
                    results[rel] = str(hp)
                elif hp.is_dir():
                    for root, dirs, files in os.walk(hp):
                        dirs.sort()
                        for fn in sorted(files):
                            fp = Path(root) / fn
                            rel = str(fp.relative_to(context))
                            results[rel] = str(fp)

        return sorted(results.items(), key=lambda x: x[0])

    def _map_copy_dest(self, file_pairs: List[Tuple[str, str]], dest: str) -> List[Tuple[str, str]]:
        is_dir = dest.endswith(("/", "/.", "/..")) or dest in (".", "..")
        if not dest.startswith("/"):
            base = self.workdir or "/"
            dest = os.path.join(base, dest)
            if is_dir and not dest.endswith("/"):
                dest += "/"
        elif is_dir and not dest.endswith("/"):
            dest += "/"

        dest_clean = dest.lstrip("/")
        dest_is_dir = dest.endswith("/") or is_dir
        multi      = len(file_pairs) > 1
        mapped     = []
        for rel_src, abs_src in file_pairs:
            if dest_is_dir or multi:
                arcname = os.path.join(dest_clean, rel_src) if dest_clean else rel_src
            else:
                arcname = dest_clean or Path(rel_src).name
            arcname = os.path.normpath(arcname).replace("\\", "/").lstrip("/")
            mapped.append((arcname, abs_src))
        return mapped

    def _write_manifest(self) -> dict:
        env_list = [f"{k}={v}" for k, v in sorted(self.env.items())]
        config: dict = {}
        if env_list:             config["Env"]        = env_list
        if self.cmd_list:        config["Cmd"]        = self.cmd_list
        if self.workdir:         config["WorkingDir"] = self.workdir

        # Preserve created timestamp on pure cache-hit rebuilds
        created = None
        if self.store.image_exists(self.name, self.tag):
            try:
                created = self.store.load_manifest(self.name, self.tag).get("created")
            except Exception:
                pass
        created = created or datetime.now(timezone.utc).isoformat()

        manifest = {
            "name":    self.name,
            "tag":     self.tag,
            "digest":  "",
            "created": created,
            "config":  config,
            "layers":  self.layers,
        }
        canonical        = json.dumps(manifest, separators=(",", ":"), sort_keys=True)
        manifest["digest"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
        self.store.save_manifest(manifest)
        return manifest


# ── filesystem snapshot helpers ───────────────────────────────────────────────

def _snapshot(rootfs: str) -> Dict[str, Tuple[float, int]]:
    """Record mtime+size of every regular file, skipping virtual dirs."""
    snap = {}
    root = Path(rootfs)
    for dirpath, dirnames, filenames in os.walk(rootfs):
        dp      = Path(dirpath)
        rel_dir = dp.relative_to(root)
        parts   = rel_dir.parts
        # Prune excluded top-level dirs in-place so os.walk skips them
        if parts and parts[0] in _EXCLUDE_DIRS:
            dirnames.clear()
            continue
        if not parts:
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        for fn in filenames:
            fp  = dp / fn
            rel = str(fp.relative_to(root))
            try:
                st = fp.stat()
                snap[rel] = (st.st_mtime, st.st_size)
            except OSError:
                pass
    return snap


def _diff_snapshots(
    before: Dict[str, Tuple[float, int]],
    after:  Dict[str, Tuple[float, int]],
    rootfs: str,
) -> List[Tuple[str, str]]:
    """Return (arcname, abs_path) for files that are new or changed."""
    root    = Path(rootfs)
    changed = []
    for rel, stats in sorted(after.items()):
        if before.get(rel) != stats:
            changed.append((rel, str(root / rel)))
    return changed
