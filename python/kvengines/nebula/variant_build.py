#!/usr/bin/env python3
"""
Build NebulaGraph binary variants by applying patches and custom cmake flags.

Usage:
  python -m kvengines.nebula.variant_build --list              # List available variants
  python -m kvengines.nebula.variant_build profile              # Build one variant
  python -m kvengines.nebula.variant_build --all                # Build all variants
  python -m kvengines.nebula.variant_build --all --skip-existing # Only build new/changed

Config format (nebula-binaries.conf):
  name|output_dir|nebula_patches|rocksdb_patches|cmake_flags|description

Built binaries go into NEBULA_BINARYDIR (default: ./bin) as:
  bin/<output_dir>/nebula-graphd     — the binaries
  bin/<output_dir>/nebula-metad
  bin/<output_dir>/nebula-storaged
  bin/<output_dir>/buildinfo         — build metadata
  bin/<output_dir>/patches/          — archived patch files
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

NEBULA_BINARIES = ["nebula-graphd", "nebula-metad", "nebula-storaged"]

# Base cmake flags matching scripts/build.sh defaults
BASE_CMAKE_FLAGS = {
    "CMAKE_CXX_COMPILER": "$TOOLSET_CLANG_DIR/bin/g++",
    "CMAKE_C_COMPILER": "$TOOLSET_CLANG_DIR/bin/gcc",
    "ENABLE_WERROR": "OFF",
    "CMAKE_BUILD_TYPE": "Release",
    "ENABLE_TESTING": "OFF",
    "Rocksdb_INCLUDE_DIR": "/workspace/rocksdb/include",
    "Rocksdb_LIBRARY": "/workspace/rocksdb/librocksdb.a",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Variant:
    name: str
    output_dir: str
    nebula_patches: list[str]
    rocksdb_patches: list[str]
    cmake_flags: dict[str, str]
    description: str


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_list(value: str) -> list[str]:
    """Parse comma-separated list, treating '-' as empty."""
    value = value.strip()
    return [] if not value or value == "-" else [x.strip() for x in value.split(",") if x.strip() and x != "-"]


def parse_cmake_flags(value: str) -> dict[str, str]:
    """Parse semicolon-separated KEY=VALUE pairs, treating '-' as empty."""
    value = value.strip()
    if not value or value == "-":
        return {}
    out = {}
    for item in value.split(";"):
        if not item.strip():
            continue
        if "=" not in item:
            raise ValueError(f"Invalid cmake flag '{item}'. Expected KEY=VALUE.")
        key, val = item.split("=", 1)
        out[key.strip()] = val.strip()
    return out


def load_variants(config_file: Path) -> dict[str, Variant]:
    """Load variant definitions from config file."""
    if not config_file.exists():
        raise FileNotFoundError(f"Config not found: {config_file}")

    variants = {}
    for line_no, line in enumerate(config_file.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split("|", 5)]
        parts += [""] * (6 - len(parts))  # Pad to 6 fields
        name, out_dir, nebula_raw, rocks_raw, flags_raw, desc = parts[:6]

        if not name or not out_dir:
            raise ValueError(f"{config_file}:{line_no}: name and output_dir required")
        if name in variants:
            raise ValueError(f"{config_file}:{line_no}: duplicate variant '{name}'")

        variants[name] = Variant(
            name=name,
            output_dir=out_dir,
            nebula_patches=parse_list(nebula_raw),
            rocksdb_patches=parse_list(rocks_raw),
            cmake_flags=parse_cmake_flags(flags_raw),
            description=desc,
        )
    return variants


def list_variants(variants: dict[str, Variant]) -> None:
    """Print table of available variants."""
    print(f"{'NAME':<12} {'OUTPUT':<18} {'NEBULA':<30} {'ROCKSDB':<12} DESCRIPTION")
    for v in variants.values():
        nebula = ",".join(Path(p).name for p in v.nebula_patches) or "-"
        rocks = ",".join(Path(p).name for p in v.rocksdb_patches) or "-"
        print(f"{v.name:<12} {v.output_dir:<18} {nebula:<30} {rocks:<12} {v.description}")


# ---------------------------------------------------------------------------
# Shell / git helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], cwd: Path | None = None, dry_run: bool = False) -> None:
    """Run command, print if dry_run."""
    if dry_run:
        print(f"[dry-run] {' '.join(cmd)}" + (f" (in {cwd})" if cwd else ""))
    else:
        subprocess.run(cmd, cwd=cwd, check=True)


def git_rev(path: Path) -> str:
    """Get short git revision, or 'unknown' if not a git repo."""
    try:
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            check=True, capture_output=True, text=True
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return "unknown"


def check_source_trees_clean(nebula_src: Path, rocks_src: Path) -> list[str]:
    """Check if source trees have uncommitted changes.

    Uses 'git status -- <path>' scoped to each source directory, since
    nebula/ and rocksdb/ share the parent repo's .git.
    Returns list of warning strings (empty if clean).
    """
    warnings = []
    for label, path in [("nebula", nebula_src), ("rocksdb", rocks_src)]:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", str(path)],
            capture_output=True, text=True, check=True
        )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if lines:
            modified = sum(1 for line in lines if not line.startswith("?"))
            untracked = sum(1 for line in lines if line.startswith("?"))
            parts = []
            if modified:
                parts.append(f"{modified} modified/staged")
            if untracked:
                parts.append(f"{untracked} untracked")
            warnings.append(f"  {path.name}/: {', '.join(parts)}")
    return warnings


# ---------------------------------------------------------------------------
# Source tree management
# ---------------------------------------------------------------------------

def reset_sources(nebula_src: Path, rocks_src: Path, root_dir: Path, dry_run: bool) -> None:
    """Reset nebula and rocksdb source directories to clean git state."""
    # Nebula: delete src/ only (preserve build/ for incremental cmake)
    src_dir = nebula_src / "src"
    if src_dir.exists() or dry_run:
        run(["rm", "-rf", str(src_dir)], dry_run=dry_run)
    run(["git", "restore", str(nebula_src)], cwd=root_dir, dry_run=dry_run)

    # RocksDB: restore tracked source files only (preserves librocksdb.a)
    run(["git", "restore", str(rocks_src)], cwd=root_dir, dry_run=dry_run)


def apply_patches(root_dir: Path, patches: list[str], dry_run: bool) -> None:
    """Apply patches from root directory."""
    for patch in patches:
        patch_path = Path(patch) if Path(patch).is_absolute() else root_dir / patch
        if not patch_path.exists():
            raise FileNotFoundError(f"Patch not found: {patch}")
        print(f"  Applying: {patch_path.name}")
        run(["patch", "-p1", "-i", str(patch_path)], cwd=root_dir, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Config hash — deterministic fingerprint for staleness detection
# ---------------------------------------------------------------------------

def config_hash(variant: Variant) -> str:
    """Compute a deterministic hash of the variant's build-relevant config."""
    canonical = "\n".join([
        f"nebula_patches={','.join(sorted(variant.nebula_patches))}",
        f"rocksdb_patches={','.join(sorted(variant.rocksdb_patches))}",
        f"cmake_flags={';'.join(f'{k}={v}' for k, v in sorted(variant.cmake_flags.items()))}",
    ])
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def read_buildinfo_hash(out_dir: Path) -> str | None:
    """Read config_hash from a variant's buildinfo file, or None if missing."""
    info_path = out_dir / "buildinfo"
    if not info_path.exists():
        return None
    for line in info_path.read_text().splitlines():
        if line.startswith("config_hash="):
            return line.split("=", 1)[1].strip()
    return None


def should_skip_variant(variant: Variant, binary_dir: Path, skip_existing: bool) -> tuple[bool, str]:
    """Decide whether to skip building a variant.

    Returns (should_skip, reason).
    """
    if not skip_existing:
        return False, ""

    out_dir = binary_dir / variant.output_dir

    if not all((out_dir / b).exists() for b in NEBULA_BINARIES):
        return False, ""

    stored_hash = read_buildinfo_hash(out_dir)
    current_hash = config_hash(variant)

    if stored_hash is None:
        # Old buildinfo without hash — rebuild to generate one
        return False, ""

    if stored_hash == current_hash:
        return True, "up-to-date"

    # Config changed since last build
    print(f"  Note: config changed for {variant.name}, rebuilding")
    return False, ""


# ---------------------------------------------------------------------------
# RocksDB build
# ---------------------------------------------------------------------------

def build_rocksdb_in_container(container: str, clean: bool, dry_run: bool) -> None:
    """Build RocksDB static library inside the Docker container.

    Mirrors the make static_lib invocation in scripts/build.sh.
    When clean=True, runs 'make clean-rocks' first (for patched source).
    """
    parts = ["set -euo pipefail", "cd /workspace/rocksdb"]
    if clean:
        parts.append("make clean-rocks")
    parts.append(
        "make static_lib -j10"
        " CC=$TOOLSET_CLANG_DIR/bin/gcc"
        " CXX=$TOOLSET_CLANG_DIR/bin/g++"
        " DEBUG_LEVEL=0 USE_RTTI=1"
        " SNAPPY=1 ZLIB=1 BZ2=1 LZ4=1 ZSTD=1"
        ' EXTRA_CFLAGS="-I/opt/vesoft/third-party/3.3/include"'
        ' EXTRA_CXXFLAGS="-I/opt/vesoft/third-party/3.3/include"'
        ' EXTRA_LDFLAGS="-L/opt/vesoft/third-party/3.3/lib -L/opt/vesoft/third-party/3.3/lib64"'
    )
    run(["sudo", "docker", "exec", container, "bash", "-c", " && ".join(parts)],
        dry_run=dry_run)


def ensure_rocksdb(variant: Variant, rocks_src: Path, container: str, dry_run: bool) -> None:
    """Build RocksDB if needed: always when patches applied, otherwise only if librocksdb.a is missing."""
    has_patches = bool(variant.rocksdb_patches)
    lib_exists = (rocks_src / "librocksdb.a").exists()

    if has_patches:
        print("  Building RocksDB (patched source, clean build)...")
        build_rocksdb_in_container(container, clean=True, dry_run=dry_run)
    elif not lib_exists:
        print("  Building RocksDB (librocksdb.a missing)...")
        build_rocksdb_in_container(container, clean=False, dry_run=dry_run)
    else:
        print("  RocksDB: reusing existing librocksdb.a")


# ---------------------------------------------------------------------------
# Build metadata and artifacts
# ---------------------------------------------------------------------------

def build_cmake_args(variant: Variant) -> str:
    """Build cmake command line flags by merging base defaults with variant overrides."""
    flags = dict(BASE_CMAKE_FLAGS)
    flags.update(variant.cmake_flags)
    return " ".join(f"-D{k}={v}" for k, v in flags.items())


def build_in_container(container: str, cmake_args: str, dry_run: bool) -> None:
    """Run cmake + make inside the Docker container."""
    build_cmd = (
        "set -euo pipefail && "
        "cd /workspace/nebula && mkdir -p build && cd build && "
        f"cmake {cmake_args} .. && "
        "make -j10 && "
        "make install"
    )
    run(["sudo", "docker", "exec", container, "bash", "-c", build_cmd], dry_run=dry_run)


def collect_binaries(container: str, out_dir: Path, dry_run: bool) -> None:
    """Copy built binaries from container to output directory."""
    if dry_run:
        print(f"[dry-run] mkdir -p {out_dir}")
        for b in NEBULA_BINARIES:
            print(f"[dry-run] docker cp {container}:/usr/local/nebula/bin/{b} {out_dir}/{b}")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    for b in NEBULA_BINARIES:
        dest = str(out_dir / b)
        subprocess.run(
            ["sudo", "docker", "cp", f"{container}:/usr/local/nebula/bin/{b}", dest],
            check=True,
        )
        # docker cp creates root-owned files; reclaim ownership so chmod works
        subprocess.run(["sudo", "chown", f"{os.getuid()}:{os.getgid()}", dest], check=True)
        (out_dir / b).chmod((out_dir / b).stat().st_mode | 0o111)


def write_buildinfo(out_dir: Path, variant: Variant, root_dir: Path, container: str, dry_run: bool) -> None:
    """Write build metadata to buildinfo file inside the variant directory."""
    info_path = out_dir / "buildinfo"
    if dry_run:
        print(f"[dry-run] write {info_path}")
        return

    info = [
        f"variant={variant.name}",
        f"built_at={datetime.now(timezone.utc).isoformat()}",
        f"binaries={','.join(NEBULA_BINARIES)}",
        f"nebula_patches={','.join(variant.nebula_patches) or '-'}",
        f"rocksdb_patches={','.join(variant.rocksdb_patches) or '-'}",
        f"cmake_flags={';'.join(f'{k}={v}' for k, v in variant.cmake_flags.items()) or '-'}",
        f"commit={git_rev(root_dir)}",
        f"container_image={container}",
        f"config_hash={config_hash(variant)}",
    ]
    info_path.write_text("\n".join(info) + "\n")


def archive_patches(out_dir: Path, variant: Variant, root_dir: Path, dry_run: bool) -> None:
    """Copy patch files alongside the built binaries for traceability."""
    patch_dir = out_dir / "patches"
    if dry_run:
        print(f"[dry-run] archive patches to {patch_dir}")
        return

    if patch_dir.exists():
        shutil.rmtree(patch_dir)
    patch_dir.mkdir(parents=True)

    for patch in variant.nebula_patches + variant.rocksdb_patches:
        patch_path = Path(patch) if Path(patch).is_absolute() else root_dir / patch
        if patch_path.exists():
            shutil.copy2(patch_path, patch_dir / patch_path.name)


# ---------------------------------------------------------------------------
# Build a single variant (pure build mechanics, no skip/cleanup logic)
# ---------------------------------------------------------------------------

def build_variant(variant: Variant, root_dir: Path, nebula_src: Path, rocks_src: Path,
                  binary_dir: Path, container: str, dry_run: bool) -> None:
    """Apply patches, compile in container, and collect variant binaries."""
    out_dir = binary_dir / variant.output_dir

    print(f"\n=== Building: {variant.name} ===")
    print(f"  Output: {out_dir}")
    if variant.nebula_patches or variant.rocksdb_patches:
        print(f"  Patches: nebula={len(variant.nebula_patches)}, rocksdb={len(variant.rocksdb_patches)}")
    if variant.cmake_flags:
        print(f"  CMake flags: {', '.join(variant.cmake_flags.keys())}")

    if variant.nebula_patches:
        apply_patches(root_dir, variant.nebula_patches, dry_run)
    if variant.rocksdb_patches:
        apply_patches(root_dir, variant.rocksdb_patches, dry_run)

    ensure_rocksdb(variant, rocks_src, container, dry_run)

    cmake_args = build_cmake_args(variant)
    print("  Building Nebula in container...")
    build_in_container(container, cmake_args, dry_run)

    print("  Collecting binaries...")
    collect_binaries(container, out_dir, dry_run)

    write_buildinfo(out_dir, variant, root_dir, container, dry_run)
    archive_patches(out_dir, variant, root_dir, dry_run)

    if not dry_run:
        print(f"  Built: {out_dir}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(default_config: Path) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build NebulaGraph binary variants with patches and custom cmake flags.")
    parser.add_argument("variants", nargs="*", help="Variant name(s) to build")
    parser.add_argument("--config", default=str(default_config), help="Config file (default: nebula-binaries.conf)")
    parser.add_argument("--all", action="store_true", help="Build all variants")
    parser.add_argument("--list", action="store_true", help="List available variants")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--no-cleanup", action="store_true", help="Skip source tree reset after each build")
    parser.add_argument("--skip-existing", action="store_true", help="Skip variants whose binary is up-to-date with config")
    parser.add_argument("--force", action="store_true", help="Proceed even if source trees have uncommitted changes")
    return parser.parse_args()


def main() -> int:
    root_dir = Path(os.environ.get("MY_NEBULA_ROOTDIR", os.getcwd())).absolute()
    default_config = root_dir / "nebula-binaries.conf"
    args = parse_args(default_config)

    config_file = Path(args.config).expanduser().absolute()
    variants = load_variants(config_file)

    if args.list:
        list_variants(variants)
        return 0

    # Collect unique variants to build
    if args.all:
        selected = list(variants.keys())
    else:
        selected = args.variants
    selected = list(dict.fromkeys(selected))  # Remove duplicates, preserve order

    if not selected:
        print("No variants specified. Use --list, --all, or pass variant names.", file=sys.stderr)
        return 1

    unknown = [name for name in selected if name not in variants]
    if unknown:
        print(f"Unknown variants: {', '.join(unknown)}", file=sys.stderr)
        print(f"Use --list to see available variants in {config_file}", file=sys.stderr)
        return 1

    nebula_src = Path(os.environ.get("MY_NEBULA_SRCDIR", root_dir / "nebula")).absolute()
    rocks_src = Path(os.environ.get("MY_ROCKS_SRCDIR", root_dir / "rocksdb")).absolute()
    binary_dir = Path(os.environ.get("NEBULA_BINARYDIR", root_dir / "bin")).absolute()
    container = os.environ.get("MY_NEBULA_CONTAINER_NAME", "nebula-dev")

    # --- Dirty tree guard ---
    dirty = check_source_trees_clean(nebula_src, rocks_src)
    if dirty:
        print("Source trees have uncommitted changes:", file=sys.stderr)
        for warning in dirty:
            print(warning, file=sys.stderr)
        if not args.force:
            print("Aborting. Use --force to proceed anyway (changes will be lost).", file=sys.stderr)
            return 1
        print("Proceeding despite dirty source trees (--force).", file=sys.stderr)

    # --- Build loop ---
    built: list[str] = []
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []

    for name in selected:
        variant = variants[name]

        skip, reason = should_skip_variant(variant, binary_dir, args.skip_existing)
        if skip:
            print(f"\n--- Skipping: {name} ({reason})")
            skipped.append((name, reason))
            continue

        reset_sources(nebula_src, rocks_src, root_dir, args.dry_run)
        try:
            build_variant(variant, root_dir, nebula_src, rocks_src, binary_dir, container, args.dry_run)
            built.append(name)
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
            print(f"  FAILED: {name}: {exc}", file=sys.stderr)
            failed.append((name, str(exc)))

        if not args.no_cleanup:
            try:
                reset_sources(nebula_src, rocks_src, root_dir, args.dry_run)
                # Delete patched librocksdb.a so next variant doesn't inherit it
                if variant.rocksdb_patches:
                    lib_path = rocks_src / "librocksdb.a"
                    if lib_path.exists() and not args.dry_run:
                        lib_path.unlink()
                    elif args.dry_run:
                        print(f"[dry-run] rm {lib_path}")
            except subprocess.CalledProcessError:
                print("  Warning: post-build source cleanup failed", file=sys.stderr)

    # --- Summary ---
    print()
    if built:
        print(f"Built {len(built)} variant(s): {', '.join(built)}")
    if skipped:
        print(f"Skipped {len(skipped)} variant(s): {', '.join(f'{n} ({r})' for n, r in skipped)}")
    if failed:
        print(f"FAILED {len(failed)} variant(s):")
        for name, err in failed:
            print(f"  {name}: {err}")
    if not built and not skipped and not failed:
        print("Nothing to do.")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
