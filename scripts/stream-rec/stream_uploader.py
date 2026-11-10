#!/usr/bin/env python3
"""
通用直播录像上传脚本 (合并自 kelie_upload.py + kelie_rclone.py)

多主播共用,在各自 systemd service 的 ExecStart 里传入不同参数即可。

四种运行模式 (--mode):
  upload       上传 B 站后按日期移到 backup/{date}/;backup 分区可用空间不足时
               自动删除最老一天备份(阈值由 --cleanup-threshold 控制,默认 20%)
  upload-only  仅上传 B 站,完成后删除本地文件
  rclone       上传 B 站完成后再 rclone 备份到云端(rclone move,本地随之消失)
  rclone-only  跳过 B 站,仅把 upload/ 内文件 rclone 备份到云端
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime

import psutil


# 上传失败最大重试次数,超过后删除文件跳过
MAX_RETRIES = 3
# 每次重试之间的等待秒数
RETRY_WAIT = 10


# ---------------- 参数 ----------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="通用直播录像上传/同步脚本")

    # 主播身份
    p.add_argument("--up", required=True, help="主播英文标识(日志用)")
    p.add_argument("--up-name", required=True, help="主播中文名(标题用)")
    p.add_argument("--base-dir", required=True,
                   help="主播工作目录,内含 upload/ 及各类 log")

    # 运行模式
    p.add_argument("--mode",
                   choices=["upload", "upload-only", "rclone", "rclone-only"],
                   default="upload",
                   help="upload: 上传后按日期备份到 backup/(空间不足自动删最老备份); "
                        "upload-only: 仅上传,完成后删除本地; "
                        "rclone: 上传后 rclone 备份到云端; "
                        "rclone-only: 跳过 B 站,仅 rclone 备份")

    # B 站投稿 (upload / upload-only / rclone 模式必填)
    p.add_argument("--cookie", default="", help="biliup-rs cookie 文件路径")
    p.add_argument("--tag", default="", help="稿件标签,逗号分隔")
    p.add_argument("--desc", default="", help="稿件简介")
    p.add_argument("--source", default="", help="转载来源(copyright=2 生效)")
    p.add_argument("--tid", type=int, default=171, help="分区 tid,默认 171")
    p.add_argument("--copyright", type=int, default=1, choices=[1, 2],
                   help="1 自制 2 转载,默认 1")
    p.add_argument("--line", default="txa", help="biliup-rs 上传线路")
    p.add_argument("--limit", type=int, default=8,
                   help="biliup-rs --limit 并发(仅 flv 使用)")
    p.add_argument("--biliup-bin", default="biliup-rs",
                   help="biliup-rs 可执行文件路径")
    p.add_argument("--title-suffix", default="",
                   help="标题后缀,常用于游戏名(如 '永劫无间')")

    # rclone (rclone / rclone-only 模式必填)
    p.add_argument("--rclone-zone", default="",
                   help="rclone 远端空间名")
    p.add_argument("--rclone-chunk-size", default="100M",
                   help="rclone --onedrive-chunk-size,默认 100M")
    p.add_argument("--rclone-extra", default="",
                   help="追加到 rclone 命令末尾的额外参数")

    # backup 磁盘清理 (mode=upload 时生效)
    p.add_argument("--cleanup-threshold", type=float, default=20.0,
                   help="backup 分区可用空间低于此百分比时删最老备份;0 关闭(默认 20)")

    # 循环
    p.add_argument("--interval", type=int, default=30, help="主循环间隔秒数")
    p.add_argument("--reset-time", default="08:00",
                   help="每天清空 upload 日志的时间 HH:MM(空串关闭)")

    cfg = p.parse_args()

    needs_bili = cfg.mode in ("upload", "upload-only", "rclone")
    needs_rclone = cfg.mode in ("rclone", "rclone-only")

    if needs_bili:
        missing = [k for k in ("cookie", "tag") if not getattr(cfg, k)]
        if missing:
            p.error(f"mode={cfg.mode} 需要参数: {', '.join('--' + m for m in missing)}")
    if needs_rclone and not cfg.rclone_zone:
        p.error(f"mode={cfg.mode} 需要 --rclone-zone")

    return cfg


def build_paths(cfg) -> dict:
    b = cfg.base_dir
    return {
        "upload":     os.path.join(b, "upload"),
        "backup":     os.path.join(b, "backup"),
        "main_log":   os.path.join(b, "main.log"),
        "upload_log": os.path.join(b, "upload.log"),
        "danmu_log":  os.path.join(b, "upload_danmu.log"),
        "rclone_log": os.path.join(b, "rclone.log"),
    }


# ---------------- 工具 ----------------

def find_first_video(directory: str, ext: str):
    for name in sorted(os.listdir(directory)):
        if name.endswith(ext):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
            if m:
                return name, m.group(1)
    return None, None


def find_oldest_subdir(base: str):
    if not os.path.isdir(base):
        return None
    subs = [os.path.join(base, s) for s in os.listdir(base)
            if os.path.isdir(os.path.join(base, s))]
    return min(subs, key=os.path.getctime) if subs else None


def read_bvid(log_path: str):
    pat = re.compile(r'"bvid":\s*String\("([^"]+)"\)')
    try:
        with open(log_path, "r") as f:
            for line in f:
                m = pat.search(line)
                if m:
                    return m.group(1)
    except FileNotFoundError:
        pass
    return None


def run(cmd: str, log_path: str) -> int:
    logging.info(f"执行 {cmd}")
    with open(log_path, "a") as f:
        return subprocess.run(cmd, shell=True, stdout=f,
                              stderr=subprocess.STDOUT).returncode


def fmt_size(path: str) -> str:
    """把文件体积格式化成人类可读字符串,文件不存在返回 ?"""
    try:
        n = os.path.getsize(path)
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}PB"


def remove_files(files: list, reason: str = ""):
    """删除一组文件并在日志里打印各自体积。"""
    for f in files:
        if not os.path.exists(f):
            continue
        size = fmt_size(f)
        try:
            os.remove(f)
            logging.info(f"删除{('(' + reason + ')') if reason else ''}: {f} ({size})")
        except OSError as e:
            logging.error(f"删除失败 {f}: {e}")


# ---------------- B 站命令 ----------------

def build_upload_cmd(cfg, media: str, title: str) -> str:
    limit = f" --limit {cfg.limit}" if media.endswith(".flv") else ""
    src = f' --source "{cfg.source}"' if cfg.copyright == 2 and cfg.source else ""
    return (
        f'{cfg.biliup_bin} --user-cookie {cfg.cookie} upload '
        f'--title "{title}" --copyright {cfg.copyright} '
        f'--desc "{cfg.desc}" --tag "{cfg.tag}" --tid {cfg.tid} '
        f'--line {cfg.line}{limit}{src} "{media}"'
    )


def build_append_cmd(cfg, media: str, bvid: str) -> str:
    limit = f" --limit {cfg.limit}" if media.endswith(".flv") else ""
    return (
        f'{cfg.biliup_bin} -u {cfg.cookie} append --vid {bvid} '
        f'--line {cfg.line}{limit} "{media}"'
    )


# ---------------- rclone 模块 ----------------

def rclone_remote_path(zone: str, sub: str) -> str:
    """组合 rclone 远端路径,兼容两种写法:
       - 'remote'                       -> 'remote:/sub'
       - 'remote:/already/some/path'    -> 'remote:/already/some/path/sub'
    """
    if ":" in zone:
        return zone.rstrip("/") + "/" + sub
    return f"{zone}:/{sub}"


def rclone_sync(cfg, live_date: str, files: list, rclone_log: str) -> bool:
    """把若干本地文件 move 到 {rclone_zone}/{live_date}"""
    if not cfg.rclone_zone:
        logging.error("rclone 未启用(--rclone-zone 为空),跳过")
        return False

    remote = rclone_remote_path(cfg.rclone_zone, live_date)

    # 首次同步本场直播时创建远端目录并建立 rclone.log 锚点
    if not os.path.exists(rclone_log):
        open(rclone_log, "w").close()
        run(f'rclone mkdir "{remote}"', rclone_log)

    extra = f" {cfg.rclone_extra}" if cfg.rclone_extra else ""
    ok = True
    for f in files:
        if not os.path.exists(f):
            continue
        cmd = (f'rclone move "{f}" "{remote}" '
               f'--onedrive-chunk-size {cfg.rclone_chunk_size}{extra}')
        if run(cmd, rclone_log) != 0:
            logging.error(f"rclone move 失败: {f}")
            ok = False
    return ok


# ---------------- 上传后动作 ----------------

def post_upload(cfg, paths, live_date: str, files: list):
    """根据 cfg.mode 处理上传完成后的本地文件"""
    if cfg.mode == "rclone":
        rclone_sync(cfg, live_date, files, paths["rclone_log"])
    elif cfg.mode == "upload-only":
        remove_files(files, reason="上传完成")
    else:  # upload -> backup/{date}/
        dest = os.path.join(paths["backup"], live_date)
        os.makedirs(dest, exist_ok=True)
        for f in files:
            if not os.path.exists(f):
                continue
            logging.info(f"移到 backup: {f} ({fmt_size(f)}) -> {dest}")
            try:
                shutil.move(f, dest)
            except OSError as e:
                logging.error(f"backup 移动失败 {f}: {e}")


# ---------------- 主流程 ----------------

def process_rclone_only(cfg, paths):
    """rclone-only 模式: 不投稿 B 站,直接把 upload/ 内文件 rclone 到 {zone}:/{date}"""
    upload_dir = paths["upload"]
    if not os.path.isdir(upload_dir):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    for name in sorted(os.listdir(upload_dir)):
        full = os.path.join(upload_dir, name)
        if not os.path.isfile(full):
            continue
        m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
        live_date = m.group(1) if m else today
        logging.info(
            f"⤴ rclone 同步 {full} ({fmt_size(full)}) -> "
            f"{rclone_remote_path(cfg.rclone_zone, live_date)}"
        )
        rclone_sync(cfg, live_date, [full], paths["rclone_log"])


def process_media(cfg, paths, ext: str, log_path: str, title_fmt: str):
    upload_dir = paths["upload"]
    if not os.path.isdir(upload_dir):
        return
    if not any(n.endswith(ext) for n in os.listdir(upload_dir)):
        return

    first, live_date = find_first_video(upload_dir, ext)
    if not first:
        return

    base = os.path.splitext(first)[0]
    media = os.path.join(upload_dir, base + ext)
    sidecar_exts = [".ass"] if ext == ".mp4" else [".xml"]
    sidecars = [os.path.join(upload_dir, base + e) for e in sidecar_exts
                if os.path.exists(os.path.join(upload_dir, base + e))]

    suffix = f" {cfg.title_suffix}" if cfg.title_suffix else ""
    title = title_fmt.format(up=cfg.up_name, date=live_date) + suffix

    all_files = [media] + sidecars
    log_exists = os.path.exists(log_path)
    bvid = read_bvid(log_path) if log_exists else None
    # 没有 bvid 就走全新投稿(无论 log 是否存在),有 bvid 才追加
    is_new = bvid is None

    if not log_exists:
        logging.info(f"★ 全新投稿 [{ext}] {media} ({fmt_size(media)})")
        cmd = build_upload_cmd(cfg, media, title)
    elif is_new:
        logging.warning(
            f"{log_path} 已存在但未找到 bvid,改为全新投稿 [{ext}] {media} ({fmt_size(media)})"
        )
        # 清掉残缺日志,避免重试时混入旧输出影响 bvid 解析
        os.remove(log_path)
        cmd = build_upload_cmd(cfg, media, title)
    else:
        logging.info(f"☆ 追加分 P {bvid} [{ext}] {media} ({fmt_size(media)})")
        cmd = build_append_cmd(cfg, media, bvid)

    success = False
    for attempt in range(1, MAX_RETRIES + 1):
        rc = run(cmd, log_path)
        # 全新投稿还需确认 log 里出现 bvid,否则视为失败
        if rc == 0 and (not is_new or read_bvid(log_path)):
            success = True
            logging.info(f"✓ 第 {attempt}/{MAX_RETRIES} 次上传成功: {media}")
            break
        logging.warning(f"✗ 第 {attempt}/{MAX_RETRIES} 次上传失败 rc={rc}: {media}")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_WAIT)

    if not success:
        logging.error(
            f"!! 上传失败 {MAX_RETRIES} 次,删除并跳过: {media} ({fmt_size(media)})"
        )
        remove_files(all_files, reason=f"重试{MAX_RETRIES}次失败")
        # 新投稿若彻底失败,清掉残缺的 upload log 让下个文件重新发起新投稿
        if is_new and os.path.exists(log_path):
            os.remove(log_path)
            logging.info(f"清除残缺投稿日志: {log_path}")
        return

    post_upload(cfg, paths, live_date, all_files)


def reset_logs_daily(cfg, paths):
    if not cfg.reset_time:
        return
    if datetime.now().strftime("%H:%M") != cfg.reset_time:
        return
    for key in ("upload_log", "danmu_log", "rclone_log"):
        p = paths[key]
        if os.path.exists(p):
            os.remove(p)
            logging.info(f"定时清理: 已删除 {p}")


def check_and_cleanup(backup_dir: str, threshold: float):
    if not os.path.isdir(backup_dir):
        return
    usage = psutil.disk_usage(backup_dir)
    free_pct = usage.free / usage.total * 100
    if free_pct >= threshold:
        return
    logging.warning(f"{backup_dir} 可用空间 {free_pct:.2f}% < {threshold}%")
    oldest = find_oldest_subdir(backup_dir)
    if oldest:
        logging.warning(f"删除最老备份: {oldest}")
        shutil.rmtree(oldest, ignore_errors=True)


def main_loop(cfg, paths):
    try:
        if cfg.mode == "rclone-only":
            process_rclone_only(cfg, paths)
        else:
            process_media(cfg, paths, ".mp4", paths["danmu_log"],
                          "【{up}丨弹幕版】{date} 录播")
            process_media(cfg, paths, ".flv", paths["upload_log"],
                          "【{up}】{date} 录播")
    except Exception as e:
        logging.exception(f"处理错误: {e}")

    try:
        reset_logs_daily(cfg, paths)
    except Exception as e:
        logging.exception(f"日志重置错误: {e}")

    if cfg.mode == "upload" and cfg.cleanup_threshold > 0:
        try:
            check_and_cleanup(paths["backup"], cfg.cleanup_threshold)
        except Exception as e:
            logging.exception(f"磁盘清理错误: {e}")


def main():
    cfg = parse_args()
    paths = build_paths(cfg)
    os.makedirs(paths["upload"], exist_ok=True)
    if cfg.mode == "upload":
        os.makedirs(paths["backup"], exist_ok=True)

    logging.basicConfig(
        filename=paths["main_log"],
        level=logging.INFO,
        format="%(asctime)s:%(levelname)s:%(message)s",
    )
    if cfg.mode in ("rclone", "rclone-only"):
        extra = f" zone={cfg.rclone_zone}"
    elif cfg.mode == "upload":
        extra = f" cleanup-threshold={cfg.cleanup_threshold}%"
    else:
        extra = ""
    logging.info(f"=== 启动 {cfg.up}({cfg.up_name}) mode={cfg.mode}{extra} ===")

    while True:
        time.sleep(cfg.interval)
        main_loop(cfg, paths)


if __name__ == "__main__":
    main()
