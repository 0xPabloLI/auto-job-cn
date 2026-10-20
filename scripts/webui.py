#!/usr/bin/env python3
"""
Web UI 管理面板（issue #3）：单文件、零依赖（Python stdlib http.server）。

用法：
  python3 scripts/webui.py [--port 8800]   # 浏览器打开 http://127.0.0.1:8800

功能（对应 issue #3）：
  - 总览：JD/草稿/招呼语/投递/回复率一览（build_overview）
  - 招呼语预览 + 编辑：直接编辑 greetings.json（validate_greeting_list 校验）
  - 草稿审核：approve pending_review 草稿 → gen_greetings.py promote 可用
  - 投递历史 + 过滤：applied_history.json 关键字过滤

投递不在面板内：唯一投递路径是 live_apply（agent 会话驱动的实时浏览投递，
2026-09-10 用户约定）；原「一键投递」入口随 #44 挂机盲投清理一并移除。

安全：只监听 127.0.0.1；数据目录在 private/（不入库）。勿做公网暴露。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "private/data"  # legacy 默认；main() 按解析重算前缀（#28，不做 UI 切换）


def load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def build_overview(data_dir):
    """首页总览聚合：各数据文件的计数与摘要。"""
    d = Path(data_dir)
    drafts = load_json(d / "greeting_drafts.json", [])
    greetings = load_json(d / "greetings.json", [])
    history = load_json(d / "applied_history.json", {"applied": []})
    followup = load_json(d / "followup_report.json", {})
    batch = load_json(d / "batch_apply_result.json", {})
    raw_jds = load_json(d / "raw_jds.json", [])
    return {
        "jds": len(raw_jds),
        "drafts": {
            "pending": sum(1 for x in drafts if x.get("status") == "pending_review"),
            "approved": sum(1 for x in drafts if x.get("status") == "approved"),
        },
        "greetings": len(greetings),
        "applied": len(history.get("applied", [])),
        "followup": {k: followup.get(k) for k in ("totalConversations", "hrReplied", "replyRate")},
        "lastBatch": {"applied": batch.get("applied"), "results": len(batch.get("results", []))},
    }


def filter_records(records, query):
    """关键字过滤：对记录所有字符串值做大小写无关子串匹配。"""
    q = (query or "").strip().lower()
    if not q:
        return list(records)
    return [r for r in records if any(q in str(v).lower() for v in r.values())]


def approve_drafts(drafts, eids):
    """把指定 eid 的草稿置为 approved，返回 (新列表, 晋级数)。
    每条必须是含 encryptJobId + greeting 的对象（approve 可携带客户端编辑后的全文）。"""
    for item in drafts:
        if not isinstance(item, dict) or not item.get("encryptJobId") or "greeting" not in item:
            raise ValueError("草稿每条都需要 encryptJobId 和 greeting 字段")
    out, n = [], 0
    for x in drafts:
        item = dict(x)
        if item.get("encryptJobId") in eids and item.get("status") != "approved":
            item["status"] = "approved"
            n += 1
        out.append(item)
    return out, n


def validate_greeting_list(payload):
    """招呼语编辑保存前的校验：list 且每条有 encryptJobId + greeting。"""
    if not isinstance(payload, list):
        raise ValueError("必须是 JSON 数组")
    for item in payload:
        if not isinstance(item, dict) or not item.get("encryptJobId") or not item.get("greeting"):
            raise ValueError("每条招呼语都需要 encryptJobId 和 greeting 字段")
    return payload


def make_server(host, port, data_dir=DATA):

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 安静模式
            pass

        def _send(self, code, payload, ctype="application/json; charset=utf-8"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        def _write_file(self, name, data):
            with open(Path(data_dir) / name, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        def do_GET(self):
            url = urlparse(self.path)
            route, qs = url.path, parse_qs(url.query)
            if route == "/":
                return self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            if route == "/api/overview":
                return self._send(200, build_overview(data_dir))
            if route == "/api/drafts":
                return self._send(200, {"drafts": load_json(Path(data_dir) / "greeting_drafts.json", [])})
            if route == "/api/greetings":
                return self._send(200, {"greetings": load_json(Path(data_dir) / "greetings.json", [])})
            if route == "/api/jds":
                return self._send(200, {"jds": load_json(Path(data_dir) / "raw_jds.json", [])})
            if route == "/api/matched":
                matched = load_json(Path(data_dir) / "matched_jds.json", {})
                if not isinstance(matched, dict) or "matched" not in matched:
                    matched = {"matched": load_json(Path(data_dir) / "matched_jd.json", [])}
                return self._send(200, matched)
            if route == "/api/history":
                records = load_json(Path(data_dir) / "applied_history.json", {"applied": []}).get("applied", [])
                return self._send(200, {"records": filter_records(records, unquote(qs.get("query", [""])[0]))})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            route = urlparse(self.path).path
            if route == "/api/drafts/approve":
                body = self._body()
                drafts = body.get("drafts")
                if not isinstance(drafts, list):
                    drafts = load_json(Path(data_dir) / "greeting_drafts.json", [])
                new, n = approve_drafts(drafts, body.get("eids", []))
                self._write_file("greeting_drafts.json", new)
                return self._send(200, {"approved": n})
            if route == "/api/greetings":
                try:
                    data = validate_greeting_list(self._body().get("greetings"))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                self._write_file("greetings.json", data)
                return self._send(200, {"saved": len(data)})
            return self._send(404, {"error": "not found"})

    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>auto-job-cn 管理面板</title>
<style>
 body{font-family:-apple-system,sans-serif;margin:0;background:#f5f6f8;color:#222}
 header{background:#1a1f2e;color:#fff;padding:14px 24px;font-size:18px}
 main{max-width:960px;margin:20px auto;padding:0 16px}
 section{background:#fff;border-radius:10px;padding:16px 20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
 h2{margin:0 0 12px;font-size:16px}
 .cards{display:flex;gap:12px;flex-wrap:wrap}
 .card{flex:1;min-width:120px;background:#f0f3f8;border-radius:8px;padding:12px;text-align:center}
 .card b{display:block;font-size:24px}
 .card span{font-size:12px;color:#666}
 button{background:#2f6fed;color:#fff;border:0;border-radius:6px;padding:8px 16px;cursor:pointer}
 button.gray{background:#8a93a3}
 button:disabled{background:#c3cad6;cursor:not-allowed}
 textarea{width:100%;box-sizing:border-box;min-height:64px;font-size:13px;border:1px solid #d5dbe4;border-radius:6px;padding:8px}
 input[type=text]{padding:8px;border:1px solid #d5dbe4;border-radius:6px;width:280px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td,th{padding:6px 8px;border-bottom:1px solid #eef0f4;text-align:left}
 .draft{border:1px solid #e3e7ee;border-radius:8px;padding:10px;margin-bottom:10px}
 .muted{color:#888;font-size:12px}
 .ok{color:#1a9e55}.bad{color:#d84343}
</style>
</head>
<body>
<header>auto-job-cn 管理面板 <span style="float:right;font-size:12px;opacity:.7">127.0.0.1 · 私有数据</span></header>
<main>

<section><h2>总览</h2><div class="cards" id="overview"></div>
 <p class="muted">投递由 agent 会话跑 live_apply（实时浏览投递），本面板只做数据查看与草稿审核。</p></section>

<section><h2>招呼语草稿审核 <span class="muted">approve 后运行 <code>gen_greetings.py promote</code> 并入投递队列</span></h2>
 <div id="drafts"></div></section>

<section><h2>JD 匹配结果 <span class="muted">crawl_and_filter / agent 匹配产出</span></h2>
 <table id="matched"><thead><tr><th>职位</th><th>公司</th><th>薪资</th><th>级别</th><th>匹配理由</th></tr></thead><tbody></tbody></table></section>

<section><h2>投递队列（greetings.json）</h2>
 <div id="greetings"></div>
 <button onclick="saveGreetings()">保存全部修改</button> <span id="saveMsg" class="muted"></span></section>

<section><h2>投递历史 <input type="text" id="q" placeholder="过滤：公司/职位关键字" oninput="loadHistory()"></h2>
 <table id="history"><thead><tr><th>日期</th><th>职位</th><th>公司</th><th>备注</th></tr></thead><tbody></tbody></table></section>

<section><h2>回复率（followup_tracker）</h2><div id="followup" class="muted">-</div></section>

</main>
<script>
const api = (p, opt) => fetch(p, opt).then(r => r.json());
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function card(v, label){ return `<div class="card"><b>${v}</b><span>${label}</span></div>`; }

async function refresh(){
  const ov = await api('/api/overview');
  document.getElementById('overview').innerHTML =
    card(ov.jds,'爬到 JD') + card(ov.drafts.pending,'待审草稿') + card(ov.drafts.approved,'已审草稿') +
    card(ov.greetings,'投递队列') + card(ov.applied,'累计投递') +
    card(ov.lastBatch.applied ?? '-', '上次批量投递');
  const fu = ov.followup || {};
  document.getElementById('followup').textContent =
    `对话 ${fu.totalConversations ?? '-'} · HR 已回复 ${fu.hrReplied ?? '-'} · 回复率 ${fu.replyRate ?? '-'}`;
}

async function loadDrafts(){
  const {drafts} = await api('/api/drafts');
  const box = document.getElementById('drafts');
  const pend = drafts.filter(d => d.status === 'pending_review');
  box.innerHTML = pend.length ? '' : '<p class="muted">无待审草稿。agent 按 apply-runbook「起草规范」写入 greeting_drafts.json。</p>';
  for (const d of pend){
    const div = document.createElement('div');
    div.className = 'draft';
    div.innerHTML = `<b>${esc(d.jobName)}</b> · ${esc(d.brandName)} · ${esc(d.level||'')}
      <span class="muted">${(d.review_issues||[]).map(esc).join('；')}</span>
      <textarea data-eid="${esc(d.encryptJobId)}">${esc(d.greeting)}</textarea>
      <button onclick="approve('${esc(d.encryptJobId)}')">通过</button>`;
    box.appendChild(div);
  }
}

async function approve(eid){
  const ta = document.querySelector(`textarea[data-eid="${eid}"]`);
  const {drafts} = await api('/api/drafts');
  const target = drafts.find(d => d.encryptJobId === eid);
  if (target && ta && ta.value !== target.greeting) target.greeting = ta.value;
  // 编辑后的正文随 approve 一并落盘
  const resp = await fetch('/api/drafts/approve', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({eids:[eid], drafts})});
  await resp.json(); loadDrafts(); refresh();
}

async function loadGreetings(){
  const {greetings} = await api('/api/greetings');
  const box = document.getElementById('greetings');
  box.innerHTML = greetings.length ? '' : '<p class="muted">队列为空。审核草稿后运行 promote。</p>';
  for (const g of greetings){
    const div = document.createElement('div');
    div.className = 'draft';
    div.innerHTML = `<b>${esc(g.jobName)}</b> · ${esc(g.brandName)} · ${esc(g.level||'')} · 称呼 ${esc(g.addressee||'hr')}
      <textarea data-gid="${esc(g.encryptJobId)}">${esc(g.greeting)}</textarea>`;
    box.appendChild(div);
  }
}

async function saveGreetings(){
  const out = [];
  document.querySelectorAll('textarea[data-gid]').forEach(t => out.push({encryptJobId: t.dataset.gid, greeting: t.value}));
  const {greetings} = await api('/api/greetings');
  const merged = greetings.map(g => {
    const t = document.querySelector(`textarea[data-gid="${g.encryptJobId}"]`);
    return t ? {...g, greeting: t.value} : g;
  });
  const r = await fetch('/api/greetings', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({greetings: merged.length ? merged : out})});
  const j = await r.json();
  document.getElementById('saveMsg').textContent = j.saved ? `已保存 ${j.saved} 条` : (j.error || '');
  refresh();
}

async function loadHistory(){
  const q = document.getElementById('q').value;
  const {records} = await api('/api/history?query=' + encodeURIComponent(q));
  const tb = document.querySelector('#history tbody');
  tb.innerHTML = records.map(r =>
    `<tr><td>${esc(r.date)}</td><td>${esc(r.jobName)}</td><td>${esc(r.brandName)}</td><td>${esc(r.note||'')}</td></tr>`).join('');
}

async function loadMatched(){
  const {matched} = await api('/api/matched');
  const tb = document.querySelector('#matched tbody');
  tb.innerHTML = (matched||[]).map(m =>
    `<tr><td>${esc(m.jobName)}</td><td>${esc(m.brandName)}</td><td>${esc(m.salaryDesc||'')}</td><td>${esc(m.level||'')}</td><td class="muted">${esc(m.matchReason||'')}</td></tr>`).join('')
    || '<tr><td colspan="5" class="muted">暂无匹配结果</td></tr>';
}

refresh(); loadDrafts(); loadGreetings(); loadHistory(); loadMatched();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


def main():
    from _profile_paths import private_path as _pp, profile_line as _pl, resolve_active_profile as _rap

    _name, _prefix = _rap()
    print(_pl(_name, _prefix))
    globals()["DATA"] = _pp("data", _prefix) if _prefix else DATA
    parser = argparse.ArgumentParser(description="auto-job-cn Web UI 管理面板（仅 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8800)
    args = parser.parse_args()
    httpd = make_server("127.0.0.1", args.port, data_dir=DATA)
    print(f"[webui] http://127.0.0.1:{args.port}  (Ctrl-C 退出)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[webui] bye")


if __name__ == "__main__":
    main()
