#!/usr/bin/env python3
"""
录屏后处理（用户约定 2026-09-10）：把会话视频里的长停留段剪掉，只保留动作窗口。
依据：shots/ 的截图文件名（HHMMSS_action.png）即动作时刻——在每个动作时刻前后各保留
一小段，其余剪除，ffmpeg 拼接输出 trimmed video。

用法：python3 scripts/trim_live_video.py <session_dir> [--before 20] [--after 25]
输出：<videos_dir>/<ts>/trimmed.webm
"""

import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIDEO_BASE = ROOT / "private/data/videos"  # legacy 默认；main() 按解析重算

from _profile_paths import (  # noqa: E402  #28：私有路径唯一出口
    private_path,
    resolve_active_profile,
)


def parse_shot_times(shots_dir, session_start):
    """shots/ 文件名 → 动作时刻列表（datetime，跨午夜已归一到 session_start 之后）。
    文件名格式 HHMMSS_action.png；非匹配文件忽略。"""
    times = []
    for f in sorted(Path(shots_dir).iterdir()):
        m = re.match(r"^(\d{2})(\d{2})(\d{2})_.+\.png$", f.name)
        if not m:
            continue
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        t = session_start.replace(hour=h, minute=mi, second=s)
        if t < session_start:  # 跨午夜（00-07 点的截图属于次日）
            t += timedelta(days=1)
        times.append(t)
    return sorted(times)


def compute_windows(times, before, after, first_pad=45):
    """动作时刻 → 保留窗口 [(start_offset, end_offset)]（秒，相对视频起点）。
    相邻窗口重叠则合并；首窗口额外向前放宽（视频起点估算有误差）。
    times 必须已排序；返回按起点排序的合并窗口。"""
    if not times:
        return []
    t0 = times[0]
    offsets = [(t - t0).total_seconds() for t in times]
    windows = []
    for i, off in enumerate(offsets):
        start = max(0.0, off - (first_pad if i == 0 else before))
        end = off + after
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    return [(s, e) for s, e in windows]


def cut_video(video_path, windows, out_path):
    """按窗口切割 + concat 拼接。返回 out_path。"""
    video_path, out_path = Path(video_path), Path(out_path)
    tmp_dir = out_path.parent / f"trim_tmp_{out_path.stem}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    seg_files = []
    for i, (start, end) in enumerate(windows):
        seg = tmp_dir / f"seg_{i:03d}.webm"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-ss",
                f"{start:.1f}",
                "-to",
                f"{end:.1f}",
                "-i",
                str(video_path),
                "-c",
                "copy",
                str(seg),
            ],
            check=True,
        )
        seg_files.append(seg)
    concat_list = tmp_dir / "list.txt"
    concat_list.write_text("".join(f"file '{f}'\n" for f in seg_files), encoding="utf-8")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            str(out_path),
        ],
        check=True,
    )
    subprocess.run(["rm", "-rf", str(tmp_dir)], check=True)
    return out_path


def probe_duration(video_path):
    """ffprobe format=duration → strip 后字符串；webm 流式录制头部未写 duration 时为 'N/A'（#52）。"""
    return subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(video_path)],
        capture_output=True,
        text=True,
    ).stdout.strip()


def format_minutes(raw):
    """ffprobe duration → 'x.xmin'；'N/A'/空等非数字显示 '未知'（打印守卫，不参与计算——#52）。"""
    try:
        return f"{float(raw) / 60:.1f}min"
    except ValueError:
        return "未知"


def trim_videos(videos, windows):
    """逐视频剪辑并打印时长变化；单视频时长 N/A 只显示未知，不阻断其余视频（#52 验收 2）。"""
    for v in videos:
        out = v.parent / f"trimmed_{v.name}"
        cut_video(v, windows, out)
        src = format_minutes(probe_duration(v))
        dst = format_minutes(probe_duration(out))
        print(f"  {v.name}: {src} → {dst} → {out}")


def main():
    _n, _prefix = resolve_active_profile()
    video_base = private_path("data/videos", _prefix) or VIDEO_BASE
    ap = argparse.ArgumentParser(description="剪掉会话视频中的长停留段")
    ap.add_argument("session_dir", help="private/data/live_session/<ts> 目录")
    ap.add_argument("--before", type=float, default=20, help="动作前保留秒数")
    ap.add_argument("--after", type=float, default=25, help="动作后保留秒数")
    args = ap.parse_args()

    session_dir = Path(args.session_dir)
    ts = session_dir.name  # YYYYMMDD-HHMMSS = 会话目录创建时刻（≈视频起点前）
    date_str, clock = ts.split("-")
    session_start = datetime.strptime(f"{date_str} {clock}", "%Y%m%d %H%M%S")

    times = parse_shot_times(session_dir / "shots", session_start)
    if not times:
        print("[!] shots/ 无截图，无法定位动作时刻")
        return 1
    videos = sorted(video_base.glob(f"{ts}/*.webm"))
    if not videos:
        print(f"[!] {video_base / ts} 无视频文件")
        return 1

    windows = compute_windows(times, args.before, args.after)
    total_keep = sum(e - s for s, e in windows)
    print(f"[剪辑] {len(times)} 个动作点 → {len(windows)} 个窗口，保留 {total_keep / 60:.1f} 分钟")

    trim_videos(videos, windows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
