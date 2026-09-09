"""Local-only chat entry point; all business decisions use the same runtime."""
import argparse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from .config import Settings
from .runtime import ShoppingRuntime
from .state import InvalidChange


PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>日常导购</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f4ef;color:#202c29;font:16px/1.7 system-ui,sans-serif}
main{max-width:840px;margin:48px auto;padding:0 24px}h1{font-size:32px;margin:0}header p{color:#65736c;margin:8px 0 24px}
#chat{min-height:260px}article{background:white;border:1px solid #dfe3dc;border-radius:14px;padding:18px 22px;margin:16px 0;white-space:pre-wrap;overflow-wrap:anywhere}
article.user{background:#e6efe7;margin-left:48px}small{color:#69786e;display:block;margin-top:12px;font-size:12px}
form{display:flex;gap:12px;align-items:flex-end;margin:24px 0}textarea{font:inherit;flex:1;min-height:88px;padding:14px;border:1px solid #bdc8bc;border-radius:12px;resize:vertical}
button{background:#28543e;color:white;border:0;border-radius:10px;padding:12px 18px;cursor:pointer;font:inherit}button:disabled{opacity:.5;cursor:wait}
#new{background:transparent;color:#28543e;border:1px solid #bdc8bc;padding:6px 12px}#status{color:#69786e;font-size:14px}footer{color:#69786e;font-size:13px}
</style><main><header><h1>日常导购</h1><p>12 个日常品类 · 600 条商品资料 · 历史美元价格</p><button id="new">开始新会话</button></header>
<section id="chat"><article>告诉我你想找什么，以及已有的要求。例如：找一个 100 美元以内的耳机。</article></section>
<div id="status"></div><form><textarea id="input" placeholder="输入需求，或继续修改预算、比较商品……" maxlength="8000" required></textarea><button id="send">发送</button></form>
<footer>价格和规格来自历史商品资料；缺失的信息会明确说明。本工具不下单、不提供实时库存。</footer></main>
<script>
let cid=localStorage.getItem('shopping-v1-conversation');
const chat=document.querySelector('#chat'), status=document.querySelector('#status'), send=document.querySelector('#send'), fresh=document.querySelector('#new');
function add(text,role,note){const a=document.createElement('article');a.className=role;a.textContent=text;if(note){const s=document.createElement('small');s.textContent=note;a.append(s)}chat.append(a);a.scrollIntoView({behavior:'smooth',block:'end'})}
async function request(url,options={}){const r=await fetch(url,options);if(!r.ok)throw new Error('请求失败（'+r.status+'），请检查服务或重新开始会话。');return r.json()}
if(cid){send.disabled=true;request('/api/conversations/'+cid).then(data=>{chat.replaceChildren();for(const m of data.messages)add(m.content,m.role)}).catch(()=>{cid=null;localStorage.removeItem('shopping-v1-conversation');status.textContent='原会话不可用，请开始新会话。'}).finally(()=>{send.disabled=false})}
fresh.onclick=()=>{cid=null;localStorage.removeItem('shopping-v1-conversation');chat.replaceChildren();add('新的会话已开始。你想找什么？','assistant');status.textContent=''};
document.querySelector('form').onsubmit=async e=>{e.preventDefault();const input=document.querySelector('#input'),text=input.value.trim();if(!text)return;add(text,'user');input.value='';send.disabled=true;fresh.disabled=true;status.textContent='正在处理需求和查询商品资料……';try{if(!cid){cid=(await request('/api/conversations',{method:'POST'})).conversation_id;localStorage.setItem('shopping-v1-conversation',cid)}const result=await request('/api/conversations/'+cid+'/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text})});add(result.message,'assistant',result.price_notice||'');status.textContent=result.kind==='execution_limited'?'本轮未完成，已生效的修改仍保留。':''}catch(error){status.textContent=error.message}finally{send.disabled=false;fresh.disabled=false;input.focus()}};
</script></html>"""


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=8000)


def create_app(settings, *, runtime_dir=None, catalog_dir=None, model=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.runtime = ShoppingRuntime(settings, runtime_dir=runtime_dir, catalog_dir=catalog_dir, model=model)
        try:
            yield
        finally:
            await app.state.runtime.close()

    app = FastAPI(title="Shopping Agent", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return PAGE

    @app.get("/health")
    async def health():
        return {"status": "ready", "model": settings.model, "catalog_count": sum(app.state.runtime.catalog.categories.values()), "provider_connectivity": "not_checked_by_health"}

    @app.post("/api/conversations")
    async def create_conversation():
        return {"conversation_id": app.state.runtime.create_conversation()}

    @app.get("/api/conversations/{conversation_id}")
    async def history(conversation_id: str):
        try:
            conversation = app.state.runtime.conversation(conversation_id)
            return {"conversation_id": conversation_id, "messages": conversation["messages"]}
        except InvalidChange as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/conversations/{conversation_id}/messages")
    async def message(conversation_id: str, body: Message):
        try:
            return await app.state.runtime.run(conversation_id, body.message)
        except InvalidChange as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path.cwd())
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    uvicorn.run(create_app(Settings.load(args.root)), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
