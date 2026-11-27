#!/usr/bin/env python3
"""
Build Dgraph binary variants by applying patches and custom build flags.

Usage:
  python -m kvengines.dgraph.variant_build --list              # List available variants
  python -m kvengines.dgraph.variant_build profile              # Build one variant
  python -m kvengines.dgraph.variant_build --all                # Build all variants
  python -m kvengines.dgraph.variant_build --all --skip-existing # Only build new/changed

Config format (dgraph-binaries.conf):
  name|output_relpath|dgraph_patches|badger_patches|make_env|description

Built binaries go into DGRAPH_BINARYDIR (default: ./bin) as:
  bin/<output_relpath>/dgraph       — the binary
  bin/<output_relpath>/buildinfo    — build metadata
  bin/<output_relpath>/patches/     — archived patch files
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


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Variant:
    name: str
    output_relpath: str
    dgraph_patches: list[str]
    badger_patches: list[str]
    make_env: dict[str, str]
    description: str


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_list(value: str) -> list[str]:
    """Parse comma-separated list, treating '-' as empty."""
    value = value.strip()
    return [] if not value or value == "-" else [x.strip() for x in value.split(",") if x.strip() and x != "-"]


def parse_env(value: str) -> dict[str, str]:
    """Parse semicolon-separated KEY=VALUE pairs, treating '-' as empty."""
    value = value.strip()
    if not value or value == "-":
        return {}
    out = {}
    for item in value.split(";"):
        if not item.strip():
            continue
        if "=" not in item:
            raise ValueError(f"Invalid env '{item}'. Expected KEY=VALUE.")
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
        name, out_rel, dgraph_raw, badger_raw, env_raw, desc = parts[:6]

        if not name or not out_rel:
            raise ValueError(f"{config_file}:{line_no}: name and output_relpath required")
        if name in variants:
            raise ValueError(f"{config_file}:{line_no}: duplicate variant '{name}'")

        variants[name] = Variant(
            name=name,
            output_relpath=out_rel,
            dgraph_patches=parse_list(dgraph_raw),
            badger_patches=parse_list(badger_raw),
            make_env=parse_env(env_raw),
            description=desc,
        )
    return variants


def list_variants(variants: dict[str, Variant]) -> None:
    """Print table of available variants."""
    print(f"{'NAME':<16} {'OUTPUT':<34} {'DGRAPH':<12} {'BADGER':<12} DESCRIPTION")
    for v in variants.values():
        dgraph = ",".join(Path(p).name for p in v.dgraph_patches) or "-"
        badger = ",".join(Path(p).name for p in v.badger_patches) or "-"
        print(f"{v.name:<16} {v.output_relpath:<34} {dgraph:<12} {badger:<12} {v.description}")


# ---------------------------------------------------------------------------
# Shell / git helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None, dry_run: bool = False) -> None:
    """Run command, print if dry_run."""
    if dry_run:
        env_diff = ""
        if env:
            diff_vars = {k: v for k, v in env.items() if os.environ.get(k) != v}
            if diff_vars:
                env_diff = " with " + " ".join(f"{k}={v}" for k, v in sorted(diff_vars.items()))
        print(f"[dry-run] {' '.join(cmd)}" + env_diff + (f" (in {cwd})" if cwd else ""))
    else:
        subprocess.run(cmd, cwd=cwd, env=env, check=True)


def git_rev(path: Path) -> str:
    """Get short git revision, or 'unknown' if not a git repo."""
    try:
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            check=True, capture_output=True, text=True
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return "unknown"


def check_source_trees_clean(dgraph_src: Path, badger_src: Path) -> list[str]:
    """Check if source trees have uncommitted changes.

    Uses 'git status -- <path>' scoped to each source directory, since
    dgraph-25.0.0/ and badger/ share the parent repo's .git.
    Returns list of warning strings (empty if clean).
    """
    warnings = []
    for label, path in [("dgraph", dgraph_src), ("badger", badger_src)]:
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

def reset_sources(dgraph_src: Path, badger_src: Path, dry_run: bool) -> None:
    """Reset dgraph and badger source directories to clean git state."""
    run(["git", "-C", str(dgraph_src), "restore", "--worktree", "--staged", "."], dry_run=dry_run)
    run(["git", "-C", str(dgraph_src), "clean", "-fd"], dry_run=dry_run)
    run(["git", "-C", str(badger_src), "restore", "--worktree", "--staged", "."], dry_run=dry_run)
    run(["git", "-C", str(badger_src), "clean", "-fd"], dry_run=dry_run)


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
        f"dgraph_patches={','.join(sorted(variant.dgraph_patches))}",
        f"badger_patches={','.join(sorted(variant.badger_patches))}",
        f"make_env={';'.join(f'{k}={v}' for k, v in sorted(variant.make_env.items()))}",
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

    out_dir = Path(variant.output_relpath)
    if not out_dir.is_absolute():
        out_dir = binary_dir / out_dir

    if not (out_dir / "dgraph").exists():
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
# Build metadata and artifacts
# ---------------------------------------------------------------------------

def write_buildinfo(out_dir: Path, variant: Variant, dgraph_src: Path, badger_src: Path, dry_run: bool) -> None:
    """Write build metadata to buildinfo file inside the variant directory."""
    info_path = out_dir / "buildinfo"
    if dry_run:
        print(f"[dry-run] write {info_path}")
        return

    info = [
        f"variant={variant.name}",
        f"built_at={datetime.now(timezone.utc).isoformat()}",
        f"binary={out_dir / 'dgraph'}",
        f"dgraph_patches={','.join(variant.dgraph_patches) or '-'}",
        f"badger_patches={','.join(variant.badger_patches) or '-'}",
        f"make_env={';'.join(f'{k}={v}' for k, v in variant.make_env.items()) or '-'}",
        f"commit={git_rev(dgraph_src)}",
        f"config_hash={config_hash(variant)}",
    ]
    info_path.write_text("\n".join(info) + "\n")


def archive_patches(out_dir: Path, variant: Variant, root_dir: Path, dry_run: bool) -> None:
    """Copy patch files into the variant directory for traceability."""
    patch_dir = out_dir / "patches"
    if dry_run:
        print(f"[dry-run] archive patches to {patch_dir}")
        return

    if patch_dir.exists():
        shutil.rmtree(patch_dir)
    patch_dir.mkdir(parents=True)

    for patch in variant.dgraph_patches + variant.badger_patches:
        patch_path = Path(patch) if Path(patch).is_absolute() else root_dir / patch
        if patch_path.exists():
            shutil.copy2(patch_path, patch_dir / patch_path.name)


def unlink_busy_binary(path: Path) -> None:
    """Unlink a binary that is currently being executed, with a warning.

    On Linux, unlinking a running binary is safe — the process keeps its fd.
    This avoids ETXTBSY when overwriting with shutil.copy2.
    """
    if not path.exists():
        return
    try:
        result = subprocess.run(
            ["fuser", str(path)], capture_output=True, text=True
        )
    except FileNotFoundError:
        # fuser not installed; optimistically unlink
        path.unlink()
        return
    if result.returncode == 0:
        pids = result.stdout.strip()
        print(f"  Warning: {path} is currently running (PID {pids}). Unlinking old binary.")
        path.unlink()


# ---------------------------------------------------------------------------
# Build a single variant (pure build mechanics, no skip/cleanup logic)
# ---------------------------------------------------------------------------

def build_variant(variant: Variant, root_dir: Path, dgraph_src: Path, badger_src: Path,
                  binary_dir: Path, dry_run: bool) -> None:
    """Apply patches, compile, and install a single variant binary."""
    out_dir = Path(variant.output_relpath)
    if not out_dir.is_absolute():
        out_dir = binary_dir / out_dir
    out_binary = out_dir / "dgraph"

    print(f"\n=== Building: {variant.name} ===")
    print(f"  Output: {out_dir}/")
    if variant.dgraph_patches or variant.badger_patches:
        print(f"  Patches: dgraph={len(variant.dgraph_patches)}, badger={len(variant.badger_patches)}")
    if variant.make_env:
        print(f"  Env: {', '.join(variant.make_env.keys())}")

    if variant.dgraph_patches:
        apply_patches(root_dir, variant.dgraph_patches, dry_run)
    if variant.badger_patches:
        apply_patches(root_dir, variant.badger_patches, dry_run)

    env = os.environ.copy()
    env.update(variant.make_env)
    print("  Building...")
    run(["make", "dgraph"], cwd=dgraph_src, env=env, dry_run=dry_run)

    built_bin = dgraph_src / "dgraph" / "dgraph"
    if dry_run:
        print(f"[dry-run] copy {built_bin} -> {out_binary}")
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        unlink_busy_binary(out_binary)
        shutil.copy2(built_bin, out_binary)
        out_binary.chmod(out_binary.stat().st_mode | 0o111)
        print(f"  Built: {out_binary}")

    write_buildinfo(out_dir, variant, dgraph_src, badger_src, dry_run)
    archive_patches(out_dir, variant, root_dir, dry_run)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(default_config: Path) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Dgraph binary variants with patches and custom flags.")
    parser.add_argument("variants", nargs="*", help="Variant name(s) to build")
    parser.add_argument("--config", default=str(default_config), help="Config file (default: dgraph-binaries.conf)")
    parser.add_argument("--all", action="store_true", help="Build all variants")
    parser.add_argument("--list", action="store_true", help="List available variants")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--no-cleanup", action="store_true", help="Skip source tree reset after each build")
    parser.add_argument("--skip-existing", action="store_true", help="Skip variants whose binary is up-to-date with config")
    parser.add_argument("--force", action="store_true", help="Proceed even if source trees have uncommitted changes")
    return parser.parse_args()


def main() -> int:
    root_dir = Path(os.environ.get("MY_DGRAPH_ROOTDIR", os.getcwd())).absolute()
    default_config = root_dir / "dgraph-binaries.conf"
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

    dgraph_src = Path(os.environ.get("DGRAPH_SRCDIR", root_dir / "dgraph-25.0.0")).absolute()
    badger_src = Path(os.environ.get("BADGER_SRCDIR", root_dir / "badger")).absolute()
    binary_dir = Path(os.environ.get("DGRAPH_BINARYDIR", root_dir / "bin")).absolute()

    # --- Dirty tree guard ---
    dirty = check_source_trees_clean(dgraph_src, badger_src)
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

        reset_sources(dgraph_src, badger_src, args.dry_run)
        try:
            build_variant(variant, root_dir, dgraph_src, badger_src, binary_dir, args.dry_run)
            built.append(name)
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
            print(f"  FAILED: {name}: {exc}", file=sys.stderr)
            failed.append((name, str(exc)))

        if not args.no_cleanup:
            try:
                reset_sources(dgraph_src, badger_src, args.dry_run)
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
