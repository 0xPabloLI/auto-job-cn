#!/usr/bin/env python3
"""
scripts 共享落盘底座：tmp + os.replace 原子写（写一半中断不损坏已有文件），
父目录不存在则创建。当前消费方：repo_survey / build_kb / industry_research。

导入方式（scripts/ 目录内平铺，参照 _llm.py 的处理）：
  sys.path.insert(0, str(Path(__file__).resolve().parent))
  from _atomic_io import write_json_atomic, write_text_atomic

命名说明：不用 _io.py——_io 是 CPython 内建模块（sys.builtin_module_names），
同名文件永远无法被 import 遮蔽。
"""

import json
import os
from pathlib import Path


def write_json_atomic(path, data):
    """JSON 原子写：tmp 文件 + os.replace，父目录不存在则创建（ensure_ascii=False, indent=2）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def write_text_atomic(path, text):
    """文本原子写：tmp 文件 + os.replace，父目录不存在则创建。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)
