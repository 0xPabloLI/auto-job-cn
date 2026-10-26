#!/usr/bin/env python3
"""
Camoufox 带登录态爬站内 JD。
- 持久化 user_data_dir 保存登录 cookie
- 首次运行：打开登录页，等用户手动登录（小号），保存状态
- 之后运行：带已保存的登录态读取 JD 页面数据；命中 code=38 即登录态失效，重跑 --login
用法:
  首次: python3 test_camoufox_login.py --login
  爬取: python3 test_camoufox_login.py
"""

import json
import os
import sys
import time
import random
from camoufox.sync_api import Camoufox


def rnd_sleep(lo=5, hi=9):
    t = random.uniform(lo, hi)
    print(f"    [限速] 等 {t:.1f}s")
    time.sleep(t)


from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from _profile_paths import (  # noqa: E402  #28：私有路径唯一出口
    browser_profile_dir,
    private_path,
    profile_line,
    resolve_active_profile,
)

PROFILE_DIR = str(ROOT / "private/.camoufox_profile")  # legacy 默认；main 按解析重算
QUERY = "Python"
CITY = "101010100"
OUT = str(ROOT / "private/data/jd_result.json")


def make_joblist_hook(result):
    def on_resp(resp):
        try:
            if "/wapi/zpgeek/search/joblist.json" in resp.url:
                j = resp.json()
                print(f"    [hook] joblist code={j.get('code')} msg={j.get('message')}")
                result["last"] = j
                if j.get("code") == 0:
                    result["data"] = j
        except Exception:
            pass

    return on_resp


def make_detail_hook(result):
    def on_resp(resp):
        try:
            if "/wapi/zpgeek/job/detail.json" in resp.url:
                j = resp.json()
                print(f"    [hook] detail code={j.get('code')} msg={j.get('message')}")
                result["last"] = j
                if j.get("code") == 0:
                    result["data"] = j
        except Exception:
            pass

    return on_resp


def wait_for(result, page, timeout=60):
    t0 = time.time()
    while result["data"] is None and time.time() - t0 < timeout:
        page.wait_for_timeout(300)
    return result["data"]


def do_login():
    _name, _prefix = resolve_active_profile()
    print(profile_line(_name, _prefix))
    profile_dir = str(browser_profile_dir(_name))
    os.makedirs(profile_dir, exist_ok=True)
    with Camoufox(
        humanize=True, geoip=True, os="macos", headless=False, persistent_context=True, user_data_dir=profile_dir
    ) as context:
        page = context.new_page()
        page.goto("https://www.zhipin.com/web/geek/job?query=Python&city=101010100", wait_until="domcontentloaded")
        print("[登录] 浏览器已打开目标站点。请在页面右上角点「登录」用小号登录。")
        print("[登录] 登录成功后回这里按回车继续...")
        input()
        page.screenshot(path=str(ROOT / "private/screenshots/after_login.png"))
        print("[登录] 已截屏 scripts/after_login.png，profile 已保存。")


def do_crawl():
    _name, _prefix = resolve_active_profile()
    print(profile_line(_name, _prefix))
    profile_dir = str(browser_profile_dir(_name))
    out_path = private_path("data/jd_result.json", _prefix)
    with Camoufox(
        humanize=True, geoip=True, os="macos", headless=False, persistent_context=True, user_data_dir=profile_dir
    ) as context:
        page = context.new_page()
        jl_result = {"data": None, "last": None}
        page.on("response", make_joblist_hook(jl_result))
        url = f"https://www.zhipin.com/web/geek/job?query={QUERY}&city={CITY}&page=1"
        print(f"[1] 导航搜索页: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
        print(f"[1] 当前 URL: {page.url}")

        print("[2] 等 joblist code=0 ...")
        data = wait_for(jl_result, page)
        if not data:
            print("[!] joblist 失败。last:", jl_result.get("last"))
            page.screenshot(path=str(ROOT / "private/screenshots/crawl_fail.png"))
            return

        zp = data.get("zpData", {})
        jobs = zp.get("jobList", [])
        print(f"[OK] joblist 成功！共 {len(jobs)} 条。")
        for j in jobs[:5]:
            print(f"    - {j.get('jobName')} | {j.get('brandName')} | {j.get('salaryDesc')}")

        pick = jobs[:3]
        out_jobs = []
        for j in pick:
            eid = j.get("encryptJobId")
            detail_url = f"https://www.zhipin.com/job_detail/{eid}.html"
            print(f"\n[3] 导航详情页: {detail_url}")
            d_result = {"data": None, "last": None}
            page.on("response", make_detail_hook(d_result))
            page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
            d = wait_for(d_result, page)
            if d:
                info = d.get("zpData", {}).get("jobInfo", {})
                desc = info.get("postDescription", "")
                print(f"    [OK] detail 成功。JD 长度 {len(desc)}")
                print(f"    JD 前 200 字: {desc[:200]}")
                out_jobs.append(
                    {
                        "jobName": j.get("jobName"),
                        "brandName": j.get("brandName"),
                        "salaryDesc": j.get("salaryDesc"),
                        "encryptJobId": eid,
                        "securityId": j.get("securityId"),
                        "postDescription": desc,
                        "source": "xhr",
                    }
                )
            else:
                print("    [!] detail XHR 失败，尝试 DOM...")
                desc_dom = page.evaluate("""() => {
                    const el = document.querySelector('.job-sec-text') || document.querySelector('.job-detail .text');
                    return el ? el.innerText : '';
                }""")
                if desc_dom:
                    print(f"    DOM JD 长度 {len(desc_dom)}: {desc_dom[:200]}")
                    out_jobs.append(
                        {
                            "jobName": j.get("jobName"),
                            "brandName": j.get("brandName"),
                            "encryptJobId": eid,
                            "postDescription": desc_dom,
                            "source": "dom",
                        }
                    )
            rnd_sleep(6, 10)

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(
                {"query": QUERY, "city": CITY, "total": len(jobs), "samples": out_jobs}, f, ensure_ascii=False, indent=2
            )
        print(f"\n[done] 存 {out_path}，详情 {len(out_jobs)} 条。")


if __name__ == "__main__":
    if "--login" in sys.argv:
        do_login()
    else:
        do_crawl()
