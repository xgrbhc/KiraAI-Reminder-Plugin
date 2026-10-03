"""Sandboxed preview of real dashboard assets with in-memory, test-only APIs.

Run: python tests/dashboard_server.py --port 0
This server binds only loopback and never calls KiraAI or an LLM.
"""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit


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
   return {status:'ok',msg:'隔离测试操作成功（无真实副作用）'};
  }
 }
};
})();"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlsplit(self.path).path
        if route == "/":
            body, content_type = PARENT, "text/html"
        elif route == "/dashboard/test-bridge.js":
            body, content_type = BRIDGE, "text/javascript"
        elif route in {f"/dashboard/{name}" for name in ("index.html", "app.js", "i18n.js", "style.css")}:
            name = route.rsplit("/", 1)[1]
            body = (WEB / name).read_text(encoding="utf-8")
            content_type = "text/css" if name.endswith(".css") else "text/javascript"
            if name == "index.html":
                content_type = "text/html"
                body = body.replace('<script src="./i18n.js"></script>',
                                    '<script src="./test-bridge.js"></script><script src="./i18n.js"></script>')
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

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
