#!/usr/bin/env python3
"""trim_live_video 的行为测试：截图时刻解析（跨午夜）、动作窗口计算与合并、
时长打印的 N/A 守卫与循环继续（#52）。"""

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "trim_live_video", Path(__file__).resolve().parent / "trim_live_video.py"
)
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class _FakeEntry:
    def __init__(self, name):
        self.name = name


def test_parse_shot_times_and_midnight(tmp_path):
    d = tmp_path / "shots"
    d.mkdir()
    for name in ["230105_list.png", "230220_sent-1.png", "000315_sent-2.png", "ignore.txt", "badfile.png"]:
        (d / name).write_bytes(b"x")
    start = datetime(2026, 9, 9, 23, 0, 0)
    times = mod.parse_shot_times(d, start)
    assert len(times) == 3, "非截图文件忽略"
    assert times[0] == datetime(2026, 9, 9, 23, 1, 5)
    assert times[-1] == datetime(2026, 9, 10, 0, 3, 15), "跨午夜截图归到次日"


def test_compute_windows_merge_overlap():
    start = datetime(2026, 9, 9, 23, 0, 0)
    times = [start + timedelta(seconds=s) for s in [60, 65, 200, 500]]
    windows = mod.compute_windows(times, before=20, after=25)
    # 偏移相对首个动作：[0,5,140,440]；0/5 重叠合并 [0,30]；140→[120,165]；440→[420,465]
    assert windows == [(0.0, 30.0), (120.0, 165.0), (420.0, 465.0)]


def test_compute_windows_first_pad():
    start = datetime(2026, 9, 9, 23, 0, 0)
    times = [start + timedelta(seconds=10), start + timedelta(seconds=300)]
    windows = mod.compute_windows(times, before=20, after=25)
    assert windows[0][0] == 0.0, "首窗口从 0 开始（视频起点估算有误差）"


def test_compute_windows_empty():
    assert mod.compute_windows([], 20, 25) == []


def test_format_minutes_normal():
    # #52 验收 3：正常时长分支——秒数 → x.xmin
    assert mod.format_minutes("90") == "1.5min"
    assert mod.format_minutes("300") == "5.0min"


def test_format_minutes_non_numeric_placeholder():
    # #52 验收 1/3：webm 流式录制 ffprobe 常返回 N/A（头部未写 duration），
    # 打印守卫显示可读占位，不抛 ValueError；空串（ffprobe 无输出）同属非数字分支
    assert mod.format_minutes("N/A") == "未知"
    assert mod.format_minutes("") == "未知"


def test_trim_videos_na_does_not_block_rest(tmp_path, capsys, monkeypatch):
    # #52 验收 1/2：任一视频 duration=N/A 只影响本条打印（显示 未知），
    # 循环继续剪完其余视频——旧版在打印行 float('N/A') 崩溃，后面的视频全部漏剪
    cuts = []

    def fake_cut(video_path, windows, out_path):
        cuts.append(Path(out_path).name)

    probe_results = iter(["N/A", "N/A", "90", "300"])  # 视频1 src/out、视频2 src/out

    def fake_probe(_path):
        return next(probe_results)

    monkeypatch.setattr(mod, "cut_video", fake_cut)
    monkeypatch.setattr(mod, "probe_duration", fake_probe)
    videos = [tmp_path / "page@1.webm", tmp_path / "page@2.webm"]
    mod.trim_videos(videos, [(0.0, 30.0)])
    out = capsys.readouterr().out
    assert cuts == ["trimmed_page@1.webm", "trimmed_page@2.webm"], "N/A 后循环继续，第二个视频仍被剪"
    assert "page@1.webm: 未知 → 未知" in out, "N/A 时长以占位呈现"
    assert "page@2.webm: 1.5min → 5.0min" in out, "正常时长照常计算"
