#!/usr/bin/env python3
"""打开 Camoufox（带登录 profile），导航到目标站点，保持打开。按回车退出。
登录态过期时复用本脚本唤起重登窗口（profile 常量同源，防锁冲突）。"""

from camoufox.sync_api import Camoufox

from _profile_paths import browser_profile_dir, profile_line, resolve_active_profile
from platforms.chat_first import JOB_SEARCH_URL

_name, _prefix = resolve_active_profile()
print(profile_line(_name, _prefix))

with Camoufox(
    humanize=True,
    geoip=True,
    os="macos",
    headless=False,
    persistent_context=True,
    user_data_dir=str(browser_profile_dir(_name)),
) as ctx:
    page = ctx.new_page()
    # 登录态/重登入口不带搜索词（#29 P0：词表只从 profile queries 单一真源来）
    page.goto(JOB_SEARCH_URL, wait_until="domcontentloaded")
    print("浏览器已打开，自己操作。按回车退出...")
    input()
