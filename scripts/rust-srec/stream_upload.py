#!/usr/bin/env python3
"""
stream_upload.py — 直播录像一体化处理脚本

两阶段流程
──────────
阶段 1 · 弹幕压制（--encode-danmaku，可选）
  监听 {base_dir}/ffmpeg/，检测到 .flv + .xml 配对后：
    ① danmu2ass 将 XML → ASS（产物留在 ffmpeg/）
    ② ffmpeg CUDA + h264_nvenc 将 ASS 压制进 FLV → .mp4（产物留在 ffmpeg/）
    ③ 压制完成后四个文件整组原子移到 {base_dir}/upload/

阶段 2 · 上传 / 归档（始终运行）
  监听 {base_dir}/upload/：
    .flv  → 普通版，独立 B 站投稿（同一天多文件续传为多 P）
    .mp4  → 弹幕版，独立 B 站投稿（与 .flv 投稿完全分离）
  两者均可通过 --upload-flv / --upload-mp4 单独开关。

后续行为（--mode）
  upload-delete   上传后删除本地文件（含弹幕文件）
  upload-backup   上传后按日期备份到 backup/（空间不足自动清理）
  upload-rclone   上传后 rclone 备份到云端
  backup          跳过 B 站，直接按日期备份到 backup/

依赖：ffmpeg、ffprobe、biliup-rs、danmu2ass（--encode-danmaku 时）
"""

from __future__ import annotations

import argparse
import functools
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime

import psutil


# ── 常量 ──────────────────────────────────────────────────
MAX_RETRIES = 3
RETRY_WAIT  = 10

_BVID_PAT = re.compile(r'"bvid":\s*String\("([^"]+)"\)')

# danmu2ass 默认参数（可在 service 中逐条覆盖）
D_FONT_SIZE    = 38
D_BOLD         = True
D_ALPHA        = 0.8
D_OUTLINE      = 1
D_DURATION     = 13
D_LANE_SIZE    = 53
D_H_GAP        = 6
D_FLOAT_PCT    = 0.43
D_STABLE_SECS  = 15


# ── 参数 ──────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="直播录像一体化处理脚本",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # 主播身份
    p.add_argument("--up-name",  required=True, help="主播名（标题及日志用）")
    p.add_argument("--base-dir", required=True, help="主播工作目录")

    # 后续行为
    p.add_argument(
        "--mode",
        choices=["upload-delete", "upload-backup", "upload-rclone", "backup"],
        default="upload-backup",
        help=(
            "upload-delete: 上传后删除本地; "
            "upload-backup: 上传后备份到 backup/; "
            "upload-rclone: 上传后 rclone 到云端; "
            "backup: 不上传，直接备份到 backup/"
        ),
    )

    # 上传版本选择
    p.add_argument("--upload-flv", action=argparse.BooleanOptionalAction,
                   default=True,  help="是否上传普通版 .flv 到 B 站")
    p.add_argument("--upload-mp4", action=argparse.BooleanOptionalAction,
                   default=True,  help="是否上传弹幕版 .mp4 到 B 站")

    # B 站投稿参数（mode != backup 且至少一个版本要上传时必填）
    p.add_argument("--cookie",     default="", help="biliup-rs cookie 文件路径")
    p.add_argument("--tag",        default="", help="稿件标签，逗号分隔")
    p.add_argument("--desc",       default="", help="稿件简介")
    p.add_argument("--source",     default="", help="转载来源（copyright=2 时生效）")
    p.add_argument("--tid",        type=int, default=171, help="分区 tid")
    p.add_argument("--copyright",  type=int, default=1, choices=[1, 2],
                   help="1 自制 2 转载")
    p.add_argument("--line",       default="txa",  help="biliup-rs 上传线路")
    p.add_argument("--limit",       type=int, default=8,
                   help="biliup-rs --limit 并发（仅 .flv 使用）")
    p.add_argument("--speed-limit", type=int, default=0,
                   help="trickle 上传限速 KB/s，0 不限速")
    p.add_argument("--title-suffix", default="",
                   help="标题后缀，如游戏名 '永劫无间'")

    # rclone（mode=upload-rclone 时必填）
    p.add_argument("--rclone-zone",       default="",    help="rclone 远端空间名")
    p.add_argument("--rclone-chunk-size", default="100M",
                   help="rclone --onedrive-chunk-size")
    p.add_argument("--rclone-extra",      default="",
                   help="追加到 rclone 命令末尾的额外参数")

    # backup 磁盘清理
    p.add_argument("--cleanup-threshold", type=float, default=20.0,
                   help="backup 分区可用空间低于此百分比时删最老备份；0 关闭")

    # 弹幕压制开关
    p.add_argument("--encode-danmaku", action=argparse.BooleanOptionalAction,
                   default=False, help="是否启用弹幕压制阶段")

    # danmu2ass 参数
    p.add_argument("--danmu2ass-bin", default="/opt/stream-rec/danmu2ass",
                   help="danmu2ass 可执行路径")
    p.add_argument("--font",          default="",
                   help="弹幕字体，留空使用 danmu2ass 默认（黑体）")
    p.add_argument("--font-size",     type=int,   default=D_FONT_SIZE,  help="弹幕字号（像素）")
    p.add_argument("--bold",          action=argparse.BooleanOptionalAction,
                   default=D_BOLD,    help="弹幕加粗")
    p.add_argument("--alpha",         type=float, default=D_ALPHA,      help="弹幕不透明度")
    p.add_argument("--outline",       type=float, default=D_OUTLINE,    help="弹幕描边宽度")
    p.add_argument("--duration",      type=float, default=D_DURATION,   help="滚动弹幕停留秒数")
    p.add_argument("--lane-size",     type=int,   default=D_LANE_SIZE,  help="弹幕行高（像素）")
    p.add_argument("--horizontal-gap",type=float, default=D_H_GAP,      help="弹幕最小水平间距")
    p.add_argument("--float-percentage",type=float,default=D_FLOAT_PCT, help="滚动弹幕最大占屏比")
    p.add_argument("--stable-secs",   type=int,   default=D_STABLE_SECS,
                   help="文件多少秒未修改才处理")
    p.add_argument("--denylist",      default="",
                   help="弹幕屏蔽词文件路径，留空不启用")

    # 运行控制
    p.add_argument("--interval",   type=int, default=30, help="主循环间隔秒数")
    p.add_argument("--reset-time", default="08:00",
                   help="每天重置投稿日志的时间 HH:MM（空串关闭）")

    cfg = p.parse_args()

    needs_bili = cfg.mode != "backup" and (cfg.upload_flv or cfg.upload_mp4)
    if needs_bili:
        missing = [k for k in ("cookie", "tag") if not getattr(cfg, k)]
        if missing:
            p.error(f"mode={cfg.mode} 需要参数: {', '.join('--' + m for m in missing)}")
    if cfg.mode == "upload-rclone" and not cfg.rclone_zone:
        p.error("mode=upload-rclone 需要 --rclone-zone")
    if cfg.encode_danmaku and not os.access(cfg.danmu2ass_bin, os.X_OK):
        p.error(f"danmu2ass 不可执行: {cfg.danmu2ass_bin}")

    return cfg


def build_paths(cfg) -> dict:
    b = cfg.base_dir
    return {
        "ffmpeg_dir": os.path.join(b, "ffmpeg"),
        "upload_dir": os.path.join(b, "upload"),
        "backup_dir": os.path.join(b, "backup"),
        "main_log":   os.path.join(b, "main.log"),
        "encode_log": os.path.join(b, "encode.log"),
        "bvid_flv":   os.path.join(b, "bvid_flv.log"),   # FLV 普通版投稿 bvid
        "bvid_mp4":   os.path.join(b, "bvid_mp4.log"),   # MP4 弹幕版投稿 bvid
        "rclone_log": os.path.join(b, "rclone.log"),
    }


# ── 通用工具 ──────────────────────────────────────────────

def fmt_size(path: str) -> str:
    try:
        n = os.path.getsize(path)
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def find_first_video(directory: str, ext: str):
    """返回 upload/ 中最早（按文件名排序）的该扩展名文件名及其日期字符串。"""
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
    return min(subs, key=os.path.basename) if subs else None


def read_bvid(log_path: str):
    try:
        with open(log_path) as f:
            for line in f:
                m = _BVID_PAT.search(line)
                if m:
                    return m.group(1)
    except FileNotFoundError:
        pass
    return None


def _fmt_cmd(cmd: str | list) -> str:
    if isinstance(cmd, str):
        return cmd
    return " ".join(str(a) for a in cmd)


def run_cmd(cmd: str | list, log_path: str) -> int:
    logging.info("执行: %s", _fmt_cmd(cmd))
    with open(log_path, "a") as f:
        return subprocess.run(
            cmd, shell=isinstance(cmd, str),
            stdout=f, stderr=subprocess.STDOUT,
        ).returncode


def tail_log_since(log_path: str, since: int, max_lines: int = 10) -> str:
    try:
        with open(log_path, "rb") as f:
            f.seek(since)
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = [ln.rstrip() for ln in data.splitlines() if ln.strip()]
    return "\n".join(lines[-max_lines:])


def remove_files(files: list, reason: str = ""):
    tag = f"({reason})" if reason else ""
    for f in files:
        if not os.path.exists(f):
            continue
        size = fmt_size(f)
        try:
            os.remove(f)
            logging.info("删除%s: %s (%s)", tag, f, size)
        except OSError as e:
            logging.error("删除失败 %s: %s", f, e)


def is_stable(path: str, min_age: int) -> bool:
    try:
        st = os.stat(path)
        return st.st_size > 0 and (time.time() - st.st_mtime) >= min_age
    except OSError:
        return False


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _unique_dest(dest_dir: str, filename: str) -> str:
    """返回 dest_dir/filename 的唯一路径，已存在则加 _1、_2… 后缀。"""
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(dest_dir, filename)
    n = 1
    while os.path.exists(candidate):
        candidate = os.path.join(dest_dir, f"{base}_{n}{ext}")
        n += 1
    return candidate


# ── 弹幕压制 ──────────────────────────────────────────────

def get_video_size(path: str) -> tuple[int, int]:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        text=True,
    ).strip()
    w, h = out.split(",")
    return int(w), int(h)


@functools.lru_cache(maxsize=1)
def _system_font() -> str:
    try:
        out = subprocess.check_output(
            ["fc-match", "--format=%{family}", "sans-serif:lang=zh"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        return out.split(",")[0].strip()
    except Exception:
        return ""


def xml_to_ass(xml_path: str, ass_path: str,
               width: int, height: int, cfg) -> int:
    """调用 danmu2ass 将 XML → ASS，返回弹幕数量。ass_path 与 xml_path 同目录。"""
    font = cfg.font or _system_font()
    cmd = [
        cfg.danmu2ass_bin,
        "--alpha",         str(cfg.alpha),
        "--duration",      str(cfg.duration),
        "--font-size",     str(cfg.font_size),
        "--outline",       str(cfg.outline),
        "--force",
        "--horizontal-gap", str(cfg.horizontal_gap),
        "--lane-size",     str(cfg.lane_size),
        "-p",              str(cfg.float_percentage),
        "--width",         str(width),
        "--height",        str(height),
    ]
    if cfg.bold:
        cmd.append("--bold")
    if font:
        cmd += ["-f", font]
    if cfg.denylist:
        cmd += ["--denylist", cfg.denylist]
    cmd += [xml_path, "-o", ass_path]

    logging.info("执行: %s", _fmt_cmd(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"danmu2ass 失败 (rc={res.returncode}): {res.stderr.strip()}"
        )
    m = re.search(r"弹幕数量[:：](\d+)", res.stderr)
    return int(m.group(1)) if m else 0


def encode_video(flv: str, ass_path: str, mp4: str, log: str) -> bool:
    """CUDA 解码 + h264_nvenc 编码，ASS 硬压进视频。"""
    cmd = [
        "ffmpeg", "-y", "-nostats", "-hwaccel", "cuda",
        "-i", flv,
        "-vf", f"subtitles='{ass_path}'",
        "-c:v", "h264_nvenc", "-rc", "cbr",
        "-b:v", "15000k", "-bufsize", "15000k",
        "-preset", "4", "-profile:v", "high",
        "-movflags", "+faststart",
        "-c:a", "copy",
        "-f", "mp4",
        mp4,
    ]
    logging.info("执行: %s", _fmt_cmd(cmd))
    with open(log, "a") as lf:
        ret = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)
    return ret.returncode == 0


def encode_one_pair(flv: str, xml: str, ffmpeg_dir: str, upload_dir: str,
                    encode_log: str, cfg) -> bool:
    """
    处理一对 flv+xml：在 ffmpeg/ 内完成压制，再整组移到 upload/。
    移动顺序保证上传阶段扫描到的状态始终完整：
      ① .ass（mp4 的 sidecar）
      ② .xml（flv 的 sidecar）
      ③ .flv（此时 .xml 已就位）
      ④ .mp4（最后，此时 .ass 已就位）
    """
    base_name = os.path.splitext(os.path.basename(flv))[0]
    in_ffmpeg = lambda ext: os.path.join(ffmpeg_dir, base_name + ext)
    in_upload = lambda ext: os.path.join(upload_dir,  base_name + ext)

    os.makedirs(upload_dir, exist_ok=True)

    try:
        width, height = get_video_size(flv)
    except Exception as e:
        logging.error("ffprobe 失败 %s: %s", flv, e)
        return False

    logging.info("★ 压制 [%dx%d] %s (%s)",
                 width, height, os.path.basename(flv), fmt_size(flv))

    # ASS 直接写到 ffmpeg/（与源文件同目录）
    ass_in_ffmpeg = in_ffmpeg(".ass")
    try:
        count = xml_to_ass(xml, ass_in_ffmpeg, width, height, cfg)
        logging.info("  弹幕转换完成: %d 条", count)
    except Exception as e:
        logging.error("  XML→ASS 失败: %s", e)
        return False

    mp4_part = in_ffmpeg(".mp4") + ".part"
    if not encode_video(flv, ass_in_ffmpeg, mp4_part, encode_log):
        logging.error("  ffmpeg 压制失败: %s", os.path.basename(flv))
        _safe_unlink(mp4_part)
        _safe_unlink(ass_in_ffmpeg)
        return False
    logging.info("  压制完成: %s", fmt_size(mp4_part))

    mp4_in_ffmpeg = in_ffmpeg(".mp4")
    os.rename(mp4_part, mp4_in_ffmpeg)

    # 整组原子移到 upload/（同分区 os.rename）
    try:
        os.rename(ass_in_ffmpeg,  in_upload(".ass"))  # ①
        os.rename(xml,            in_upload(".xml"))  # ②
        os.rename(flv,            in_upload(".flv"))  # ③
        os.rename(mp4_in_ffmpeg,  in_upload(".mp4"))  # ④
    except OSError as e:
        logging.error("  移动到 upload/ 失败: %s", e)
        _safe_unlink(mp4_in_ffmpeg)
        _safe_unlink(ass_in_ffmpeg)
        return False

    logging.info("  → upload/ 完成: %s.*", base_name)
    return True


def encode_phase(paths: dict, processed: set, cfg) -> None:
    """扫描 ffmpeg/，对稳定的 flv+xml 配对执行压制。"""
    ffmpeg_dir = paths["ffmpeg_dir"]
    if not os.path.isdir(ffmpeg_dir):
        return

    for name in sorted(os.listdir(ffmpeg_dir)):
        if not name.endswith(".flv"):
            continue
        base     = os.path.splitext(name)[0]
        flv_path = os.path.join(ffmpeg_dir, name)
        xml_path = os.path.join(ffmpeg_dir, base + ".xml")

        if base in processed or not os.path.exists(xml_path):
            continue
        if not is_stable(flv_path, cfg.stable_secs) or \
           not is_stable(xml_path, cfg.stable_secs):
            continue

        try:
            ok = encode_one_pair(flv_path, xml_path, ffmpeg_dir, paths["upload_dir"],
                                 paths["encode_log"], cfg)
            if ok:
                processed.add(base)
        except Exception as e:
            logging.exception("压制异常 %s: %s", name, e)


# ── B 站上传 ──────────────────────────────────────────────

def _build_upload_cmd(cfg, media: str, title: str) -> list:
    cmd = ["trickle", "-u", str(cfg.speed_limit)] if cfg.speed_limit > 0 else []
    cmd += [
        "biliup-rs", "--user-cookie", cfg.cookie,
        "upload",
        "--title", title,
        "--copyright", str(cfg.copyright),
        "--desc", cfg.desc,
        "--tag", cfg.tag,
        "--tid", str(cfg.tid),
        "--line", cfg.line,
    ]
    if media.endswith(".flv"):
        cmd += ["--limit", str(cfg.limit)]
    if cfg.copyright == 2 and cfg.source:
        cmd += ["--source", cfg.source]
    cmd.append(media)
    return cmd


def _build_append_cmd(cfg, media: str, bvid: str) -> list:
    cmd = ["trickle", "-u", str(cfg.speed_limit)] if cfg.speed_limit > 0 else []
    cmd += ["biliup-rs", "-u", cfg.cookie, "append", "--vid", bvid, "--line", cfg.line]
    if media.endswith(".flv"):
        cmd += ["--limit", str(cfg.limit)]
    cmd.append(media)
    return cmd


# ── 后续行为 ──────────────────────────────────────────────

def post_action(cfg, paths, live_date: str, files: list) -> None:
    """上传成功（或 mode=backup 跳过上传）后对本地文件的处理。"""
    if cfg.mode in ("upload-delete",):
        remove_files(files, reason="上传完成")

    elif cfg.mode in ("upload-backup", "backup"):
        dest = os.path.join(paths["backup_dir"], live_date)
        os.makedirs(dest, exist_ok=True)
        for f in files:
            if not os.path.exists(f):
                continue
            dst = _unique_dest(dest, os.path.basename(f))
            logging.info("备份: %s (%s) → %s", os.path.basename(f), fmt_size(f), dst)
            try:
                shutil.move(f, dst)
            except OSError as e:
                logging.error("备份失败 %s: %s", f, e)

    elif cfg.mode == "upload-rclone":
        rclone_sync(cfg, live_date, files, paths["rclone_log"])


def rclone_remote_path(zone: str, sub: str) -> str:
    if ":" in zone:
        return zone.rstrip("/") + "/" + sub
    return f"{zone}:/{sub}"


def rclone_sync(cfg, live_date: str, files: list, rclone_log: str) -> bool:
    remote = rclone_remote_path(cfg.rclone_zone, live_date)
    if not os.path.exists(rclone_log):
        open(rclone_log, "w").close()
        run_cmd(f'rclone mkdir "{remote}"', rclone_log)
    extra = f" {cfg.rclone_extra}" if cfg.rclone_extra else ""
    ok = True
    for f in files:
        if not os.path.exists(f):
            continue
        cmd = (f'rclone move "{f}" "{remote}" '
               f'--onedrive-chunk-size {cfg.rclone_chunk_size}{extra}')
        if run_cmd(cmd, rclone_log) != 0:
            logging.error("rclone 失败: %s", f)
            ok = False
    return ok


# ── 上传阶段 ──────────────────────────────────────────────

def process_file(cfg, paths, ext: str, sidecar_ext: str,
                 bvid_log: str, title_fmt: str, do_upload: bool) -> None:
    """
    处理 upload/ 中一个文件（.flv 或 .mp4）及其 sidecar。
    FLV 投稿与 MP4 投稿使用各自独立的 bvid log，互不干涉。
    同一天同一类型的多个文件续传为同一投稿的多 P。
    """
    upload_dir = paths["upload_dir"]
    if not os.path.isdir(upload_dir):
        return
    if not any(n.endswith(ext) for n in os.listdir(upload_dir)):
        return

    first, live_date = find_first_video(upload_dir, ext)
    if not first:
        return

    base    = os.path.splitext(first)[0]
    media   = os.path.join(upload_dir, base + ext)
    sidecar = os.path.join(upload_dir, base + sidecar_ext)
    all_files = [media] + ([sidecar] if os.path.exists(sidecar) else [])

    # mode=backup 或此类型被关闭：跳过上传，直接归档
    if not do_upload or cfg.mode == "backup":
        logging.info("⊘ 跳过上传 [%s] %s (%s)", ext, media, fmt_size(media))
        post_action(cfg, paths, live_date, all_files)
        return

    # 决定新投稿 or 续传
    suffix = f" {cfg.title_suffix}" if cfg.title_suffix else ""
    title  = title_fmt.format(up=cfg.up_name, date=live_date) + suffix

    log_exists = os.path.exists(bvid_log)
    bvid       = read_bvid(bvid_log) if log_exists else None
    is_new     = bvid is None

    if not log_exists:
        logging.info("★ 新投稿 [%s] %s (%s)", ext, media, fmt_size(media))
        cmd = _build_upload_cmd(cfg, media, title)
    elif is_new:
        logging.warning("日志存在但无 bvid，重新投稿: %s", media)
        os.remove(bvid_log)
        cmd = _build_upload_cmd(cfg, media, title)
    else:
        logging.info("☆ 续传 P [%s] bvid=%s %s (%s)", ext, bvid, media, fmt_size(media))
        cmd = _build_append_cmd(cfg, media, bvid)

    success       = False
    file_vanished = False

    for attempt in range(1, MAX_RETRIES + 1):
        if not os.path.exists(media):
            logging.error("第 %d 次重试前源文件已消失: %s", attempt, media)
            file_vanished = True
            break

        logging.info("→ 第 %d/%d 次上传 %s (%s)",
                     attempt, MAX_RETRIES, media, fmt_size(media))
        offset = os.path.getsize(bvid_log) if os.path.exists(bvid_log) else 0
        rc = run_cmd(cmd, bvid_log)

        if rc == 0 and (not is_new or read_bvid(bvid_log)):
            success = True
            logging.info("✓ 第 %d/%d 次上传成功: %s", attempt, MAX_RETRIES, media)
            break

        logging.warning("✗ 第 %d/%d 次上传失败 rc=%d: %s",
                        attempt, MAX_RETRIES, rc, media)
        tail = tail_log_since(bvid_log, offset)
        if tail:
            logging.warning("biliup-rs 输出:\n%s", tail)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_WAIT)

    if not success:
        if file_vanished:
            remove_files([sidecar] if os.path.exists(sidecar) else [], "源文件消失")
        else:
            logging.error("!! 上传失败 %d 次，跳过: %s", MAX_RETRIES, media)
            if cfg.mode == "upload-delete":
                remove_files(all_files, f"重试{MAX_RETRIES}次失败")
            else:
                logging.info("上传失败，转入备份: %s", os.path.basename(media))
                post_action(cfg, paths, live_date, all_files)

        if is_new and os.path.exists(bvid_log):
            archive = bvid_log + ".failed"
            try:
                if os.path.exists(archive):
                    os.remove(archive)
                os.rename(bvid_log, archive)
                logging.info("失败日志归档: %s → %s", bvid_log, archive)
            except OSError:
                _safe_unlink(bvid_log)
        return

    post_action(cfg, paths, live_date, all_files)


def upload_phase(cfg, paths) -> None:
    """扫描 upload/，独立处理 .flv（普通版）和 .mp4（弹幕版）。"""
    process_file(
        cfg, paths,
        ext=".flv", sidecar_ext=".xml",
        bvid_log=paths["bvid_flv"],
        title_fmt="【{up}】 {date}",
        do_upload=cfg.upload_flv,
    )
    process_file(
        cfg, paths,
        ext=".mp4", sidecar_ext=".ass",
        bvid_log=paths["bvid_mp4"],
        title_fmt="【{up}丨弹幕版】 {date}",
        do_upload=cfg.upload_mp4,
    )


# ── 维护 ──────────────────────────────────────────────────

def reset_logs_daily(cfg, paths) -> None:
    if not cfg.reset_time:
        return
    if datetime.now().strftime("%H:%M") != cfg.reset_time:
        return
    for key in ("bvid_flv", "bvid_mp4", "rclone_log"):
        p = paths[key]
        if os.path.exists(p):
            os.remove(p)
            logging.info("定时重置: %s", p)


def check_and_cleanup(backup_dir: str, threshold: float) -> None:
    if threshold <= 0 or not os.path.isdir(backup_dir):
        return
    usage    = psutil.disk_usage(backup_dir)
    free_pct = usage.free / usage.total * 100
    if free_pct >= threshold:
        return
    logging.warning("backup 可用 %.1f%% < %.1f%%，清理最老备份", free_pct, threshold)
    oldest = find_oldest_subdir(backup_dir)
    if oldest:
        logging.warning("删除: %s", oldest)
        shutil.rmtree(oldest, ignore_errors=True)


# ── 主循环 ────────────────────────────────────────────────

def _encode_loop(cfg, paths, encode_processed: set) -> None:
    while True:
        try:
            encode_phase(paths, encode_processed, cfg)
        except Exception as e:
            logging.exception("压制阶段异常: %s", e)
        time.sleep(cfg.interval)


def main() -> None:
    cfg   = parse_args()
    paths = build_paths(cfg)

    os.makedirs(paths["upload_dir"], exist_ok=True)
    if cfg.encode_danmaku:
        os.makedirs(paths["ffmpeg_dir"], exist_ok=True)
    if cfg.mode in ("upload-backup", "backup"):
        os.makedirs(paths["backup_dir"], exist_ok=True)

    logging.basicConfig(
        filename=paths["main_log"],
        level=logging.INFO,
        format="%(asctime)s:%(levelname)s:%(message)s",
    )

    versions = "+".join(filter(None, [
        "flv" if cfg.upload_flv else "",
        "mp4" if cfg.upload_mp4 else "",
    ])) or "无"
    logging.info(
        "=== 启动 %s mode=%s upload=%s encode-danmaku=%s ===",
        cfg.up_name, cfg.mode, versions, cfg.encode_danmaku,
    )

    encode_processed: set[str] = set()

    if cfg.encode_danmaku:
        threading.Thread(
            target=_encode_loop, args=(cfg, paths, encode_processed),
            name="encode", daemon=True,
        ).start()

    while True:
        try:
            upload_phase(cfg, paths)
        except Exception as e:
            logging.exception("上传阶段异常: %s", e)

        try:
            reset_logs_daily(cfg, paths)
        except Exception as e:
            logging.exception("日志重置异常: %s", e)

        if cfg.mode in ("upload-backup", "backup") and cfg.cleanup_threshold > 0:
            try:
                check_and_cleanup(paths["backup_dir"], cfg.cleanup_threshold)
            except Exception as e:
                logging.exception("磁盘清理异常: %s", e)

        time.sleep(cfg.interval)


if __name__ == "__main__":
    main()
