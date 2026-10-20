#!/usr/bin/env python3
"""webui 的行为测试：总览聚合、历史过滤、草稿审核、招呼语校验、HTTP 端到端。"""

import json
import sys
import urllib.request
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import webui


def _mk_data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "greeting_drafts.json").write_text(
        json.dumps(
            [
                {"encryptJobId": "a", "jobName": "JA", "greeting": "ga", "status": "pending_review"},
                {"encryptJobId": "b", "jobName": "JB", "greeting": "gb", "status": "pending_review"},
                {"encryptJobId": "c", "jobName": "JC", "greeting": "gc", "status": "approved"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (d / "greetings.json").write_text(
        json.dumps(
            [
                {"encryptJobId": "c", "jobName": "JC", "greeting": "gc"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (d / "applied_history.json").write_text(
        json.dumps(
            {
                "applied": [
                    {
                        "encryptJobId": "x1",
                        "jobName": "Solidity 工程师",
                        "brandName": "珀淞区块链",
                        "date": "2026-09-06",
                    },
                    {"encryptJobId": "x2", "jobName": "后端开发", "brandName": "某基金", "date": "2026-09-06"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (d / "followup_report.json").write_text(
        json.dumps({"totalConversations": 10, "hrReplied": 2, "awaiting": 8, "replyRate": "20%"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (d / "matched_jds.json").write_text(
        json.dumps(
            {
                "matched": [
                    {
                        "jobName": "智能合约工程师",
                        "brandName": "某基金",
                        "salaryDesc": "35-65K",
                        "level": "高",
                        "matchReason": "ERC-721 经验匹配",
                        "encryptJobId": "m1",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return d


# --- 纯函数 ---


def test_build_overview_counts(tmp_path):
    d = _mk_data(tmp_path)
    ov = webui.build_overview(d)
    assert ov["drafts"]["pending"] == 2
    assert ov["drafts"]["approved"] == 1
    assert ov["greetings"] == 1
    assert ov["applied"] == 2
    assert ov["followup"]["replyRate"] == "20%"
    assert "error" not in ov


def test_build_overview_missing_files(tmp_path):
    ov = webui.build_overview(tmp_path)
    assert ov["drafts"] == {"pending": 0, "approved": 0}
    assert ov["greetings"] == 0
    assert ov["applied"] == 0


def test_filter_records():
    records = [
        {"jobName": "Solidity 工程师", "brandName": "珀淞区块链"},
        {"jobName": "后端开发", "brandName": "某基金"},
    ]
    assert len(webui.filter_records(records, "区块链")) == 1
    assert len(webui.filter_records(records, "后端")) == 1
    assert len(webui.filter_records(records, "")) == 2
    assert webui.filter_records(records, "RUST") == []


def test_approve_drafts():
    drafts = [
        {"encryptJobId": "a", "greeting": "ga", "status": "pending_review"},
        {"encryptJobId": "b", "greeting": "gb", "status": "pending_review"},
    ]
    new, n = webui.approve_drafts(drafts, ["a"])
    assert n == 1 and new[0]["status"] == "approved" and new[1]["status"] == "pending_review"


def test_validate_greeting_list():
    ok = [{"encryptJobId": "a", "greeting": "g"}]
    assert webui.validate_greeting_list(ok) == ok
    with pytest.raises(ValueError):
        webui.validate_greeting_list({"not": "a list"})
    with pytest.raises(ValueError):
        webui.validate_greeting_list([{"jobName": "no id/greeting"}])


# --- HTTP 端到端 ---


def test_approve_drafts_rejects_bad_items():
    with pytest.raises(ValueError):
        webui.approve_drafts([{"encryptJobId": "a"}], ["a"])  # 缺 greeting


@pytest.fixture()
def server(tmp_path):
    d = _mk_data(tmp_path)
    httpd = webui.make_server("127.0.0.1", 0, data_dir=d)
    port = httpd.server_address[1]
    th = __import__("threading").Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{port}", d
    httpd.shutdown()


def test_http_overview_and_page(server):
    base, d = server
    with urllib.request.urlopen(f"{base}/api/overview") as r:
        ov = json.loads(r.read().decode())
    assert ov["drafts"]["pending"] == 2
    with urllib.request.urlopen(f"{base}/") as r:
        assert "<html" in r.read().decode().lower()


def test_http_approve_persists(server):
    base, d = server
    req = urllib.request.Request(
        f"{base}/api/drafts/approve",
        data=json.dumps({"eids": ["a"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        assert json.loads(r.read().decode())["approved"] == 1
    drafts = json.loads((d / "greeting_drafts.json").read_text(encoding="utf-8"))
    assert [x["status"] for x in drafts] == ["approved", "pending_review", "approved"]


def test_http_history_filter(server):
    base, d = server
    with urllib.request.urlopen(f"{base}/api/history?query=%E5%8C%BA%E5%9D%97%E9%93%BE") as r:
        data = json.loads(r.read().decode())
    assert len(data["records"]) == 1
    assert data["records"][0]["brandName"] == "珀淞区块链"


def test_http_matched_jds(server):
    base, d = server
    with urllib.request.urlopen(f"{base}/api/matched") as r:
        data = json.loads(r.read().decode())
    assert data["matched"][0]["matchReason"] == "ERC-721 经验匹配"
