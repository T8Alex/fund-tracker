"""Encrypted local backups for the fund-tracker system.

Design:
- SQLite databases are copied with the sqlite3 `.backup` API (safe during writes).
- Configs, holdings, launchd plists and run scripts join the archive.
- The archive is tar.gz encrypted with `openssl enc -aes-256-cbc -pbkdf2`
  and a passphrase file (chmod 600) under ~/.local/share/fund-tracker.
- Code is NOT in the archive: the git repo covers it. Only git-excluded assets
  (DBs, holdings, secrets, installed job files) belong here.
- Retention: daily 7 / weekly 4 / monthly 6. Weekly/monthly tiers use marker
  files, so the first successful backup of each week/month wins even if runs
  are missed.
- A plaintext manifest lists sources, sizes and SHA-256 of the raw archive.
- The passphrase file itself stays on this machine; the encryption protects
  backups copied to cloud/external drives from casual readers.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import date, datetime
from pathlib import Path

DEFAULT_PASSFILE = Path.home() / ".local/share/fund-tracker/backup-passphrase"
DEFAULT_DEST = Path.home() / ".local/share/fund-tracker/backups"

RELATIVE_FILES = (
    "data/holdings.json",
    "config.json",
    "stock-config.json",
    "model-review-config.json",
    "run_daily.sh",
)
RELATIVE_DIRS = ("scripts",)
DATABASES = ("data/fund_tracker.db", "stock-data/stock_tracker.db")

HOME_SOURCES = (
    "Library/LaunchAgents/com.t8alex.fundtracker.plist",
    "Library/LaunchAgents/com.t8alex.stocktracker.plist",
    "Library/LaunchAgents/com.t8alex.modelreview.plist",
    "Library/LaunchAgents/com.t8alex.fundbackup.plist",
    "Library/LaunchAgents/com.t8alex.runwatch.plist",
    ".local/share/fund-tracker/run_daily.sh",
    ".local/share/fund-tracker/run_backup.sh",
    ".local/share/fund-tracker/run_watchdog.sh",
    ".local/share/stock-tracker/run_daily.sh",
    ".local/share/model-review/run_monthly.sh",
)

TIER_LIMITS = {"daily": 7, "weekly": 4, "monthly": 6}


def ensure_passphrase(path: Path | str = DEFAULT_PASSFILE) -> Path:
    """Create the passphrase file if missing; never print its content."""
    path = Path(path).expanduser()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
        token = "".join(secrets.choice(alphabet) for _ in range(32))
        path.write_text(token + "\n", encoding="utf-8")
        path.chmod(0o600)
    return path


def _openssl_args(decrypt: bool, source: Path, target: Path, passfile: Path) -> list[str]:
    args = [
        "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
        "-salt", "-in", str(source), "-out", str(target),
    ]
    if decrypt:
        args.append("-d")
    args += ["-pass", f"file:{passfile}"]
    return args


def _openssl_run(base_args: list[str]) -> None:
    completed = subprocess.run(
        base_args, capture_output=True, text=True, timeout=180
    )
    if completed.returncode == 0:
        return
    # Some macOS LibreSSL builds reject -iter 200000; retry with plain pbkdf2.
    trimmed = []
    skip_next = False
    for arg in base_args:
        if skip_next:
            skip_next = False
            continue
        if arg == "-iter":
            skip_next = True
            continue
        trimmed.append(arg)
    completed = subprocess.run(trimmed, capture_output=True, text=True, timeout=180)
    if completed.returncode != 0:
        raise RuntimeError(
            f"openssl 失败：{(completed.stderr or '').strip() or completed.returncode}"
        )


def encrypt_file(source: Path, target: Path, passfile: Path) -> None:
    _openssl_run(_openssl_args(False, source, target, Path(passfile).expanduser()))


def decrypt_file(source: Path, target: Path, passfile: Path) -> None:
    _openssl_run(_openssl_args(True, source, target, Path(passfile).expanduser()))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage(base_dir: Path, home: Path) -> tuple[Path, int]:
    staging = Path(tempfile.mkdtemp(prefix="fund-backup-"))
    staged = 0

    def add(source: Path, rel_dir: str, name: str) -> None:
        nonlocal staged
        target = (staging / rel_dir / name) if rel_dir else staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        target.chmod(0o600)
        staged += 1

    for rel in DATABASES:
        source = base_dir / rel
        if not source.exists():
            continue
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        source_conn = sqlite3.connect(source)
        target_conn = sqlite3.connect(target)
        try:
            source_conn.backup(target_conn)
            target_conn.close()
        finally:
            source_conn.close()
        target.chmod(0o600)
        staged += 1

    for rel in RELATIVE_FILES:
        source = base_dir / rel
        if source.exists():
            parts = Path(rel).parts
            rel_dir = str(parts[0]) if len(parts) > 1 else ""
            add(source, rel_dir, parts[-1])

    for directory in RELATIVE_DIRS:
        source = base_dir / directory
        if not source.exists():
            continue
        target_root = staging / directory
        shutil.copytree(
            source,
            target_root,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "*.log"),
        )
        staged += len(list(target_root.rglob("*")))

    for rel in HOME_SOURCES:
        source = home / rel
        if source.exists():
            add(source, "outside/" + rel.rsplit("/", 1)[0], rel.rsplit("/", 1)[1])
    return staging, staged


def _write_manifest(
    staging: Path,
    dest_dir: Path,
    archive_name: str,
    tar_sha: str,
    git_head: str | None = None,
) -> Path:
    entries = []
    for path in sorted(staging.rglob("*")):
        if path.is_file():
            entries.append(
                {
                    "path": str(path.relative_to(staging)),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    manifest = {
        "archive": archive_name,
        "archive_sha256": tar_sha,
        "git_head": git_head,
        "files": entries,
    }
    path = dest_dir / f"{archive_name}.manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return path


def _git_head() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError:
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def prune_tier(dest: Path, tier: str, limit: int) -> int:
    """Keep the newest `limit` archives (and their manifests) per tier."""
    tier_dir = Path(dest) / tier
    if not tier_dir.exists():
        return 0
    archives = sorted(p for p in tier_dir.glob("*.tar.enc") if p.is_file())
    removed = 0
    for stale in archives[: max(0, len(archives) - limit)]:
        stale.unlink(missing_ok=True)
        stale.with_name(stale.name + ".manifest.json").unlink(missing_ok=True)
        removed += 1
    return removed


def _tier_copy(
    archive: Path,
    dest: Path,
    stamp: date,
    marks_path: Path,
) -> list[str]:
    """daily always; weekly = first success of the ISO week; monthly = first of month."""
    marks: dict[str, str] = {}
    if Path(marks_path).exists():
        try:
            marks = json.loads(Path(marks_path).read_text(encoding="utf-8"))
        except ValueError:
            marks = {}

    iso = stamp.isocalendar()
    week_key = f"{iso.year}-W{iso.week:02d}"
    month_key = stamp.isoformat()[:7]
    tiers = ["daily"]
    if marks.get("week_key") != week_key:
        tiers.append("weekly")
        marks["week_key"] = week_key
    if marks.get("month_key") != month_key:
        tiers.append("monthly")
        marks["month_key"] = month_key

    copied: list[str] = []
    for tier in tiers:
        tier_dir = Path(dest) / tier
        tier_dir.mkdir(parents=True, exist_ok=True)
        target = tier_dir / archive.name
        if tier != "daily":
            shutil.copy2(archive, target)
        copied.append(tier)

    Path(marks_path).write_text(
        json.dumps(marks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    Path(marks_path).chmod(0o600)
    return copied


def run_backup(
    base_dir: Path,
    dest: Path | str = DEFAULT_DEST,
    passfile: Path | str = DEFAULT_PASSFILE,
    *,
    home: Path | None = None,
) -> dict:
    base_dir = Path(base_dir)
    dest = Path(dest).expanduser()
    passfile = ensure_passphrase(passfile)
    home = Path(home) if home else Path.home()
    dest.mkdir(parents=True, exist_ok=True)
    dest.chmod(0o700)

    stamp = date.today()
    stamp_text = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_name = f"dbt-{stamp_text}"
    enc_path = dest / "daily" / f"{archive_name}.tar.enc"
    enc_path.parent.mkdir(parents=True, exist_ok=True)
    tar_path = dest / "daily" / f"{archive_name}.tar.gz"

    staging, staged_count = _stage(base_dir, home)
    try:
        with tarfile.open(tar_path, "w:gz") as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(staging))
        raw_sha = sha256_file(tar_path)
        encrypt_file(tar_path, enc_path, passfile)
        _write_manifest(staging, enc_path.parent, enc_path.name, raw_sha, _git_head())
        enc_path.chmod(0o600)
    finally:
        tar_path.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)

    tiers = _tier_copy(enc_path, dest, stamp, dest / "tier-marks.json")
    pruned = {tier: prune_tier(dest, tier, limit) for tier, limit in TIER_LIMITS.items()}
    missing = [rel for rel in DATABASES if not (base_dir / rel).exists()]

    return {
        "archive": str(enc_path),
        "tiers": tiers,
        "staged_files": staged_count,
        "manifest": str(enc_path) + ".manifest.json",
        "missing_sources": missing,
        "pruned": pruned,
        "passfile": str(passfile),
    }
