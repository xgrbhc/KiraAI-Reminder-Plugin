"""Sandboxed preview of real dashboard assets with in-memory, test-only APIs.

Run: python tests/dashboard_server.py --port 0
This server binds only loopback and never calls KiraAI or an LLM.
"""

import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
import mimetypes
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


WEB = Path(__file__).resolve().parents[1] / "web"
PARENT = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Reminder dashboard isolated test</title></head><body style="margin:0;background:#181824;color:white">
<div style="padding:10px">隔离测试：内存测试数据，不连接 Kira 或 LLM。
<span id="request-count">Mock POST requests: 0</span></div>
<iframe title="Reminder dashboard test" src="/dashboard/index.html" style="width:100%;height:calc(100vh - 45px);border:0"
sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-downloads"></iframe>
<script>let calls=0;window.addEventListener('message',e=>{
if(e.origin===location.origin && e.data?.kind==='mock-post'){
document.getElementById('request-count').textContent='Mock POST requests: '+(++calls);
}});</script></body></html>"""
BRIDGE = """(() => {
const state = {
 'Test:dm:alice': {
  reminders: [
   {job_id:'job1',content:'隔离测试：喝水提醒',time:'2030-01-01 10:00',repeat:'none',creator_id:'alice',owner_id:'alice',creator_name:'Alice'},
   {job_id:'important',content:'隔离测试：重要提醒',time:'2030-01-01 11:00',repeat:'none',important:true,creator_id:'alice',owner_id:'alice',creator_name:'Alice'}
  ], deliveries: []
 },
 'Test:gm:group': {
  reminders:[{job_id:'group1',content:'隔离测试：群聊提醒',time:'2030-01-01 12:00',repeat:'none',creator_id:'bob',owner_id:'bob',creator_name:'Bob'}],
  deliveries:[]
 }
};
const preview = new URLSearchParams(location.search);
if (preview.has('stress')) {
 const r = state['Test:dm:alice'].reminders[0];
 Object.assign(r, {
  content:'https://example.invalid/'+'long_reminder_segment_'.repeat(16),
  creator_id:'long-name-test',creator_name:'long_display_name_'.repeat(8),
  category:'long_category_'.repeat(8),action:'test_only_action_name_'.repeat(8),
  important:true,paused:true,repeat:'interval',interval_minutes:1440
 });
 const sid = 'Test:dm:'+'session_identifier_'.repeat(8);
 state[sid] = {reminders:[{...r,job_id:'long-session-job'}],deliveries:[]};
}
const listSize = Math.min(100, Math.max(0, Number(preview.get('list_size')) || 0));
const readDelay = Math.min(2000, Math.max(0, Number(preview.get('read_delay')) || 0));
for(let i=2;i<listSize;i++) state['Test:dm:alice'].reminders.push({
 job_id:'scroll-test-'+i,content:'隔离滚动测试 '+i,time:'2030-01-01 12:00',repeat:'none',creator_id:'alice',owner_id:'alice',creator_name:'Alice'
});
state['Test:dm:alice'].deliveries.push({delivery_id:'delivery1',job_id:'job1',status:'unconfirmed',reminder:{...state['Test:dm:alice'].reminders[0]}});
const copy = value => JSON.parse(JSON.stringify(value));
const ok = data => ({status:'ok',data});
const error = msg => ({status:'error',msg});
const tokens = new Map();
window.PluginPageContext = {
 ready: async () => ({pluginId:'reminder_plugin',locale:'zh'}),
 onContext: () => () => {},
 api: {
  async get(endpoint) {
   if(readDelay) await new Promise(resolve=>setTimeout(resolve,readDelay));
   if(endpoint==='sessions') return ok(Object.entries(state).map(([id,s])=>({id,count:s.reminders.length,users:[...new Map(s.reminders.map(r=>[r.creator_id,{id:r.creator_id,name:r.creator_name}])).values()]})));
   const [kind,...parts]=endpoint.split('/'); const sid=decodeURIComponent(parts.join('/'));
   return state[sid] && ['reminders','deliveries'].includes(kind) ? ok(copy(state[sid][kind])) : error('Test session not found');
  },
  async post(endpoint,payload) {
   window.parent.postMessage({kind:'mock-post'},location.origin);
   console.info('Mock POST',endpoint);
   await new Promise(resolve=>setTimeout(resolve,200));
   const s=state[payload.session_id]; if(!s) return error('Test session not found');
   const action=endpoint.split('/')[1];
   if(endpoint.startsWith('deliveries/')) {
    const d=s.deliveries.find(d=>d.delivery_id===payload.delivery_id);
    if(!d) return error('Test delivery not found');
    if(action==='retry') d.status='awaiting_llm';
    else if(action==='dismiss') {s.deliveries=s.deliveries.filter(item=>item!==d);s.reminders=s.reminders.filter(r=>r.job_id!==d.job_id);}
    else return error('Invalid test decision');
   } else if(action==='confirm-delete') {
    const key=payload.session_id+':'+payload.confirm_token;
    const job=tokens.get(key);if(!job) return error('令牌无效或已过期');
    s.reminders=s.reminders.filter(r=>r.job_id!==job);tokens.delete(key);
   } else {
    const r=s.reminders.find(r=>r.job_id===payload.job_id);if(!r) return error('Test reminder not found');
    if(action==='delete') {
     if(r.important){tokens.set(payload.session_id+':testtoken',r.job_id);return error('注意：这是重要提醒\\n请确认删除令牌: testtoken');}
     s.reminders=s.reminders.filter(item=>item!==r);
    } else if(action==='pause') r.paused=true;
    else if(action==='resume') r.paused=false;
    else return error('Invalid test action');
   }
   const detail = preview.has('stress') ? 'long_notification_segment_'.repeat(8) : '';
   return {status:'ok',msg:'隔离测试操作成功（无真实副作用）：'+endpoint+' / '+(payload.job_id||payload.delivery_id||'')+detail};
  }
 }
};
})();"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlsplit(self.path).path
        if route == "/":
            body, content_type = PARENT, "text/html"
            query = urlsplit(self.path).query
            if query:
                body = body.replace('src="/dashboard/index.html"',
                                    f'src="/dashboard/index.html?{escape(query, quote=True)}"')
        elif route == "/dashboard/test-bridge.js":
            body, content_type = BRIDGE, "text/javascript"
        elif route == "/style-probe":
            # Reuse the production stylesheet order instead of maintaining a copy.
            links = re.findall(r'<link rel="stylesheet" href="[^"]+">', (WEB / "index.html").read_text(encoding="utf-8"))
            body = (Path(__file__).parent / "style_probe.html").read_text(encoding="utf-8")
            body = body.replace("<!-- DASHBOARD_STYLES -->", "\n".join(links))
            content_type = "text/html"
        elif route.startswith("/dashboard/"):
            name = unquote(route.removeprefix("/dashboard/"))
            target = (WEB / name).resolve()
            core_file = name in {"index.html", "app.js", "i18n.js", "style.css"}
            vendor_file = (
                target.is_relative_to((WEB / "vendor").resolve())
                and target.suffix in {".js", ".css", ".woff", ".woff2", ".ttf"}
            )
            if not target.is_file() or not target.is_relative_to(WEB.resolve()) or not (core_file or vendor_file):
                self.send_error(404)
                return
            body = target.read_bytes()
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if name == "index.html":
                content_type = "text/html"
                body = body.decode("utf-8").replace('<script src="./i18n.js"></script>',
                                                  '<script src="./test-bridge.js"></script><script src="./i18n.js"></script>')
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        # Only this isolated preview blocks external resources; host policy is unchanged.
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; font-src 'self'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body.encode("utf-8") if isinstance(body, str) else body)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"ISOLATED_TEST_URL=http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
