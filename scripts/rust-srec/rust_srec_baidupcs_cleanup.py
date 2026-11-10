#!/usr/bin/env python3
"""BaiduPCS-Go dated remote backup cleanup for rust-srec 0.6 Execute program/args.

Arguments:
  streamer [remote_dir_template] [max_dirs] [max_delete_per_run]

The remote template supports __STREAMER__ (recommended) and __DATE__ is not
used because cleanup targets the parent directory of date folders.
"""
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

PCS = "/app/config/BaiduPCS-Go/BaiduPCS-Go"
PCS_CONFIG_DIR = "/app/config/BaiduPCS-Go"
DEFAULT_REMOTE_TEMPLATE = "/rust-srec/__STREAMER__/0backup"
DEFAULT_MAX_DIRS = 3
DEFAULT_MAX_DELETE_PER_RUN = 2


def log(message):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [BaiduPCS-Cleanup] {message}", flush=True)


def fail(message):
    log(f"ERROR: {message}")
    sys.exit(1)


def parse_positive(value, label):
    try:
        number = int(value)
    except (ValueError, TypeError):
        fail(f"invalid {label}={value!r}")
    if number < 1:
        fail(f"invalid {label}={number} (must be >= 1)")
    return number


def main():
    if len(sys.argv) < 2 or len(sys.argv) > 5:
        fail("Usage: python3 rust_srec_baidupcs_cleanup.py <streamer> [remote_dir_template] [max_dirs] [max_delete_per_run]")

    streamer = sys.argv[1]
    if not streamer or streamer in (".", "..") or "/" in streamer or "\\" in streamer or "\x00" in streamer:
        fail("invalid streamer name")

    remote_template = (sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else DEFAULT_REMOTE_TEMPLATE)
    max_dirs = parse_positive(sys.argv[3], "MAX_DIRS") if len(sys.argv) > 3 and sys.argv[3] else DEFAULT_MAX_DIRS
    max_delete = parse_positive(sys.argv[4], "MAX_DELETE_PER_RUN") if len(sys.argv) > 4 and sys.argv[4] else DEFAULT_MAX_DELETE_PER_RUN

    remote = remote_template.replace("__STREAMER__", streamer).rstrip("/")
    # Guard against accidental deletion outside the intended parent folder.
    if not remote.startswith("/") or remote == "/" or "\x00" in remote or any(p in (".", "..") for p in remote.split("/")):
        fail(f"unsafe remote directory: {remote!r}")
    if "__" in remote and "__STREAMER__" in remote:
        fail("unresolved __STREAMER__ placeholder")

    if not (Path(PCS).is_file() and os.access(PCS, os.X_OK)):
        fail(f"BaiduPCS-Go not found or not executable: {PCS}")
    if not Path(PCS_CONFIG_DIR).is_dir():
        fail(f"config directory not found: {PCS_CONFIG_DIR}")

    pcs_env = os.environ.copy()
    pcs_env["BAIDUPCS_GO_CONFIG_DIR"] = PCS_CONFIG_DIR

    log("========== Cleanup started ==========")
    log(f"BaiduPCS-Go : {PCS}")
    log(f"Config dir  : {PCS_CONFIG_DIR}")
    log(f"Remote      : {remote}")
    log(f"Keep        : {max_dirs}")
    log(f"Max delete  : {max_delete}")
    log(f"Listing remote directory: {remote}")

    try:
        proc = subprocess.run([PCS, "ls", "-name", remote], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, errors="replace", env=pcs_env)
    except OSError as exc:
        fail(f"ls execution failed: {exc}")
    output = proc.stdout or ""
    if proc.returncode != 0:
        log(f"ERROR: ls failed, exit_code={proc.returncode}")
        if output:
            print(output, end="" if output.endswith("\n") else "\n", flush=True)
        return 1

    ansi_re = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    date_name_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    date_dirs = []
    for raw_line in ansi_re.sub("", output).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        name = line.split()[-1].rstrip("/")
        if not date_name_re.fullmatch(name):
            continue
        try:
            date.fromisoformat(name)
        except ValueError:
            continue
        date_dirs.append(name)
    date_dirs = sorted(set(date_dirs))

    log(f"Found       : {len(date_dirs)} valid date entries")
    if date_dirs:
        log("Date entries, oldest -> newest:")
        for i, name in enumerate(date_dirs, 1):
            log(f"  [{i:02d}] {name}")

    excess = max(len(date_dirs) - max_dirs, 0)
    log(f"Current     : {len(date_dirs)}")
    log(f"Limit       : {max_dirs}")
    log(f"Excess      : {excess}")
    if excess == 0:
        log("No cleanup required")
        log("========== Cleanup finished ==========")
        return 0

    delete_count = min(excess, max_delete)
    targets = date_dirs[:delete_count]
    log(f"This run    : {delete_count} directorie(s)")
    if excess > max_delete:
        log(f"SAFETY LIMIT: requested={excess}, allowed={max_delete}")
        log(f"Remaining excess after successful run: {excess - delete_count}")
    log("Delete candidates:")
    for i, name in enumerate(targets, 1):
        log(f"  DELETE [{i}/{delete_count}] {remote}/{name}")

    removed, failed = [], []
    for i, name in enumerate(targets, 1):
        target = f"{remote}/{name}"
        log("----------------------------------------")
        log(f"DELETE START [{i}/{delete_count}]: {target}")
        started = datetime.now()
        try:
            proc = subprocess.run([PCS, "rm", target], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, errors="replace", env=pcs_env)
            rc, result_output = proc.returncode, proc.stdout or ""
        except OSError as exc:
            rc, result_output = 1, str(exc)
        elapsed = (datetime.now() - started).total_seconds()
        for line in result_output.splitlines():
            print(f"[BaiduPCS-Go] {line}", flush=True)
        if rc == 0:
            removed.append(target)
            log(f"DELETE OK: {target} ({elapsed:.2f}s)")
        else:
            failed.append(target)
            log(f"DELETE FAILED: {target} (exit={rc}, {elapsed:.2f}s)")

    remaining = len(date_dirs) - len(removed)
    still_excess = max(remaining - max_dirs, 0)
    log("----------------------------------------")
    log("========== Cleanup summary ==========")
    log(f"Before       : {len(date_dirs)}")
    log(f"Retention    : {max_dirs}")
    log(f"Max/run      : {max_delete}")
    log(f"Deleted      : {len(removed)}")
    log(f"Failed       : {len(failed)}")
    log(f"Remaining    : {remaining}")
    log(f"Still excess : {still_excess}")
    for target in removed:
        log(f"  DELETED: {target}")
    if still_excess:
        log(f"NOTICE: {still_excess} excess directorie(s) remain; cleaned on later runs")
    for target in failed:
        log(f"  FAILED: {target}")
    log("========== Cleanup finished with errors ==========" if failed else
        "========== Cleanup finished successfully ==========")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
