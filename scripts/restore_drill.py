#!/usr/bin/env python3
"""Restore drill: prove a recent backup can actually be restored.

Steps:
1. Pick the newest daily archive (or pass --archive).
2. Decrypt into a fresh temp dir using the passphrase file.
3. Verify the tar.gz SHA-256 against the manifest, then every staged file's
   SHA-256 against the manifest.
4. Run PRAGMA integrity_check + row counts on both databases.
5. JSON-parse holdings and the three configs.
6. Run doctor against the restore root (launchd checks skipped).
7. Print a verdict; exit 1 on any failure. The temp dir is removed after the
   run unless --keep is given.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from shared.backup import decrypt_file, sha256_file  # noqa: E402
from scripts.doctor import run_and_format  # noqa: E402

HOME = Path.home()
DEST = HOME / ".local/share/fund-tracker/backups"

LEVEL_ICONS = {"OK": "✅", "WARN": "⚠️", "FAIL": "❌", "SKIP": "⏭️"}


def newest_archive() -> Path:
    daily = DEST / "daily"
    archives = sorted(p for p in daily.glob("*.tar.enc") if p.is_file())
    if not archives:
        raise SystemExit("没有任何每日备份归档，先运行 python3 scripts/backup.py")
    return archives[-1]


def verify_db(path: Path) -> tuple[bool, str]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        counts = [
            f"{table}={connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}"
            for table in ("nav", "prices", "runs", "predictions")
            if table in tables
        ]
        detail = f"integrity={integrity}；{'，'.join(counts) or '无已知表'}"
        return integrity == "ok", detail
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="从加密备份演练恢复并体检")
    parser.add_argument("--archive", help="指定 tar.enc；缺省取每日档最新")
    parser.add_argument("--keep", action="store_true", help="保留恢复目录以便人工检查")
    args = parser.parse_args()

    archive = Path(args.archive).expanduser() if args.archive else newest_archive()
    manifest_path = archive.with_name(archive.name + ".manifest.json")
    if not manifest_path.exists():
        print(f"❌ FAIL 清单：{manifest_path.name} 不存在")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    passfile = HOME / ".local/share/fund-tracker/backup-passphrase"
    tmp = Path(tempfile.mkdtemp(prefix="restore-drill-"))
    results: list[tuple[str, str, str]] = []

    try:
        tar_path = tmp / "restored.tar.gz"
        extract_root = tmp / "restore"
        decrypt_file(archive, tar_path, passfile)
        results.append(("解密", "OK", f"{archive.name} → tar.gz"))

        tar_sha = sha256_file(tar_path)
        expected = str(manifest.get("archive_sha256") or "")
        results.append(
            (
                "归档哈希",
                "OK" if tar_sha == expected else "FAIL",
                "sha256 一致" if tar_sha == expected else "sha256 不一致",
            )
        )

        with tarfile.open(tar_path, "r:gz") as bundle:
            bundle.extractall(extract_root, filter="data")
        results.append(("解包", "OK", str(extract_root.relative_to(tmp))))

        files = manifest.get("files") or []
        mismatch = []
        for entry in files:
            restored = extract_root / str(entry["path"])
            if not restored.exists():
                mismatch.append(f"{entry['path']}（缺失）")
            elif sha256_file(restored) != entry["sha256"]:
                mismatch.append(f"{entry['path']}（哈希不符）")
        results.append(
            (
                "逐文件校验",
                "OK" if files and not mismatch else "FAIL",
                f"{len(files)} 个文件全部一致" if not mismatch else "；".join(mismatch[:5]),
            )
        )
        tar_path.unlink(missing_ok=True)

        for rel in ("data/fund_tracker.db", "stock-data/stock_tracker.db"):
            path = extract_root / rel
            if not path.exists():
                continue  # 归档时不存在；doctor 会标记
            ok, detail = verify_db(path)
            results.append((f"数据库 {Path(rel).stem}", "OK" if ok else "FAIL", detail))

        for rel in (
            "data/holdings.json",
            "config.json",
            "stock-config.json",
            "model-review-config.json",
        ):
            path = extract_root / rel
            if not path.exists():
                continue
            try:
                json.loads(path.read_text(encoding="utf-8"))
                results.append((f"JSON {Path(rel).name}", "OK", "解析成功"))
            except ValueError as exc:
                results.append((f"JSON {Path(rel).name}", "FAIL", f"解析失败：{exc}"))

        _, doctor_code = run_and_format(extract_root, skip_launchd=True)
        results.append(("doctor", "OK" if doctor_code == 0 else "FAIL", "恢复根目录体检通过" if doctor_code == 0 else "恢复根目录体检有 FAIL，见 doctor 输出"))
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"# 恢复演练：{archive.name}")
    for name, level, detail in results:
        print(f"{LEVEL_ICONS.get(level, '•')} [{level}] {name}：{detail}")
    failed = any(level == "FAIL" for _, level, _ in results)
    print("\n结论：" + ("未能完全恢复，需要处理。" if failed else "备份可恢复，演练通过。"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
