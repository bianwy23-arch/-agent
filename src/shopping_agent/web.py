"""Local-only chat entry point; all business decisions use the same runtime."""
import argparse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from .config import Settings
from .contracts import DecisionAction
from .runtime import ShoppingRuntime, RuntimeBusy
from .state import InvalidChange


PAGE = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#f8f9fb"><title>日常导购 · 找到适合你的好物</title>
<style>
:root{--ink:#222b3c;--muted:#7a8292;--line:#e8ebf1;--blue:#5369db;--soft:#eef1ff}*{box-sizing:border-box}body{margin:0;color:var(--ink);background:#fff;font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}button,textarea{font:inherit}button{cursor:pointer}button:disabled{opacity:.45;cursor:wait}button:focus-visible,summary:focus-visible,a:focus-visible{outline:3px solid #a9b6ff;outline-offset:3px}button{transition:background .15s,transform .15s}button:hover:not(:disabled){filter:brightness(.97)}.layout{display:flex;min-height:100dvh}.sidebar{width:244px;background:#f8f9fc;border-right:1px solid var(--line);padding:32px 22px;position:fixed;inset:0 auto 0 0;display:flex;flex-direction:column}.brand{display:flex;align-items:center;gap:10px;font-size:19px;font-weight:700;letter-spacing:.5px}.mark{display:inline-grid;place-items:center;width:36px;height:36px;border-radius:12px;background:var(--blue);color:white;font-size:22px}.brand-sub{font-size:11px;letter-spacing:2px;color:var(--muted);margin:9px 0 30px 46px}.new{display:flex;align-items:center;justify-content:center;gap:12px;background:#fff;border:1px solid #dce1ef;border-radius:10px;padding:11px;color:#465bc5;width:100%}.nav-label{font-size:11px;color:#949bab;letter-spacing:1.5px;margin:32px 12px 12px}.nav-item{padding:11px 13px;border-radius:9px;background:#ebeffc;color:#495cc1;display:flex;gap:12px;align-items:center}.nav-item span:last-child{margin-left:auto;font-size:11px}.sidebar-bottom{margin-top:auto;padding:25px 10px 0}.sidebar-bottom strong{font-size:12px;font-weight:500}.sidebar-bottom p{color:var(--muted);font-size:12px;margin:6px 0}.main{margin-left:244px;width:calc(100% - 244px);min-height:100dvh;display:flex;flex-direction:column}.topbar{height:78px;flex-shrink:0;border-bottom:1px solid var(--line);padding:0 42px;display:flex;align-items:center;justify-content:space-between}.topbar-title{font-weight:600}.topbar-title small{color:#969dab;font-weight:400;margin-left:12px}.source-note{font-size:12px;color:var(--muted)}.source-note span{display:inline-block;width:6px;height:6px;background:#9ba7ce;border-radius:50%;margin-right:7px}.workspace{width:min(850px,100%);margin:0 auto;padding:0 36px;flex:1;display:flex;flex-direction:column}.welcome{padding:76px 0 30px}.eyebrow{font-size:11px;letter-spacing:2.5px;color:#8290bb;margin:0 0 18px}.welcome h1{font-size:36px;letter-spacing:-1.3px;line-height:1.4;margin:0 0 12px;font-weight:650}.welcome h1 span{color:var(--blue)}.lede{color:var(--muted);margin:0;font-size:14px}.suggestions{display:grid;grid-template-columns:repeat(3,1fr);gap:13px;margin-top:34px}.suggestion{text-align:left;padding:19px 17px;background:#fff;border:1px solid var(--line);border-radius:13px;color:var(--ink);min-height:137px}.suggestion:hover{border-color:#bbc5f4;background:#fafbff}.suggestion .icon{display:grid;place-items:center;width:31px;height:31px;background:#f0f2fb;border-radius:9px;color:#6b79b2;margin-bottom:13px;font-size:18px}.suggestion strong{display:block;font-size:13px;font-weight:550}.suggestion small{display:block;font-size:11px;color:var(--muted);margin-top:5px}.hint{font-size:12px;color:#9199a7;margin:22px 0 0}.chat{padding:22px 0}.chat:empty{display:none}.message{margin:20px 0 30px;overflow-wrap:anywhere}.message-label{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600;margin-bottom:11px;color:#788094}.avatar{width:23px;height:23px;border-radius:7px;background:var(--soft);color:var(--blue);text-align:center;line-height:23px}.message-content{white-space:pre-wrap;line-height:1.9}.message.user{max-width:85%;margin-left:auto;border-radius:16px 16px 4px 16px;background:#f1f3fa;padding:13px 18px}.user .message-label{display:none}.message small{color:var(--muted);display:block;font-size:11px;margin-top:12px}.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px;margin-top:19px}.card{border:1px solid var(--line);border-radius:13px;padding:18px;overflow:hidden}.card h3{font-size:14px;font-weight:600;line-height:1.55;margin:13px 0 10px}.badge{font-size:10px;padding:3px 8px;border-radius:5px;color:#8a6b34;background:#faf4e7}.badge.satisfied{background:#eaf5ef;color:#3d7c60}.badge.violated{background:#f8eeee;color:#a16363}.price{font-size:22px;font-weight:650;margin:9px 0 13px}.price span{font-size:10px;color:var(--muted);font-weight:400;margin-left:7px}.attribute{font-size:12px;color:#667083;padding:5px 0;border-bottom:1px solid #f1f2f5}.card details{font-size:11px;color:var(--muted);margin:13px 0}.card details p{overflow-wrap:anywhere}summary{cursor:pointer}.actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:15px}.actions button{padding:6px 10px;border:1px solid var(--line);background:white;border-radius:6px;font-size:11px;color:#697386}.actions button:first-child{background:var(--soft);border-color:var(--soft);color:#5366cc}.table-wrap{overflow:auto;margin-top:20px;border:1px solid var(--line);border-radius:10px}table{border-collapse:collapse;width:100%;font-size:12px;min-width:420px}td,th{padding:12px;border-bottom:1px solid var(--line);text-align:left;min-width:110px}th{background:#f8f9fc;font-weight:500}.composer-area{position:sticky;bottom:0;background:linear-gradient(transparent,#fff 16px);padding:24px 0 17px;margin-top:auto}.composer{border:1px solid #dce1ed;border-radius:16px;background:#fff;box-shadow:0 5px 24px #25375e07;padding:15px 17px 12px}.composer:focus-within{border-color:#a2afe8;box-shadow:0 0 0 3px #5369db08}textarea{display:block;width:100%;min-height:52px;max-height:180px;resize:vertical;border:0;outline:0;color:var(--ink);background:transparent;line-height:1.7}textarea::placeholder{color:#a0a6b3}.composer-bottom{display:flex;align-items:center;justify-content:space-between;margin-top:8px}.composer-bottom span{font-size:11px;color:#a0a7b3}.send{width:35px;height:35px;border:0;border-radius:10px;background:var(--blue);color:#fff;font-size:22px;line-height:1}.footnote{text-align:center;color:#9ba2af;font-size:10px;margin:12px 0 0}.status{font-size:12px;color:var(--blue);min-height:22px;padding-bottom:6px}.status.error{color:#ad5b49}.status.busy:before{content:'';display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--blue);margin-right:8px;animation:pulse 1.2s infinite}.hidden{display:none!important}@keyframes pulse{50%{opacity:.25}}@media(prefers-reduced-motion:reduce){*{animation:none!important;scroll-behavior:auto!important;transition:none!important}}@media(min-height:850px){.welcome{padding-top:110px}}@media(max-width:1000px){.sidebar{width:205px;padding:28px 17px}.main{margin-left:205px;width:calc(100% - 205px)}.topbar{padding:0 28px}.workspace{padding:0 28px}.welcome h1{font-size:31px}.suggestions{gap:9px}.suggestion{padding:16px 12px}}@media(max-width:700px){.sidebar{position:static;width:100%;height:auto;padding:15px 19px;flex-direction:row;align-items:center;justify-content:space-between;border-right:0;border-bottom:1px solid var(--line)}.brand{font-size:16px}.mark{width:29px;height:29px;font-size:19px}.brand-sub,.nav-label,.nav-item,.sidebar-bottom{display:none}.new{width:auto;padding:7px 11px;font-size:12px}.layout{display:block}.main{margin:0;width:100%;min-height:calc(100dvh - 66px)}.topbar{height:54px;padding:0 21px}.topbar-title{font-size:12px}.topbar-title small{display:none}.source-note{font-size:10px}.workspace{padding:0 21px}.welcome{padding:45px 0 18px}.welcome h1{font-size:29px}.lede{font-size:13px}.suggestions{grid-template-columns:1fr;margin-top:26px}.suggestion{min-height:0;padding:12px 14px;display:grid;grid-template-columns:36px 1fr;column-gap:10px}.suggestion .icon{grid-row:span 2;margin:0}.suggestion small{margin-top:1px}.hint{font-size:11px}.cards{grid-template-columns:1fr}.composer-bottom span{font-size:10px}.footnote{font-size:9px}.composer-area{padding-top:18px}}
</style></head>
<body><div class="layout"><aside class="sidebar"><div><div class="brand"><span class="mark" aria-hidden="true">✦</span>日常导购</div><div class="brand-sub">YOUR SHOPPING COMPANION</div></div><button id="new" class="new" type="button"><span aria-hidden="true">＋</span> 新对话</button><div class="nav-label">我的空间</div><div class="nav-item"><span aria-hidden="true">◇</span> 购物助手 <span>↗</span></div><div class="sidebar-bottom"><strong>把选择，交给更充分的了解。</strong><p>聊需求，看差异，慢慢选。</p></div></aside>
<main class="main"><header class="topbar"><div class="topbar-title">购物助手 <small>从你的需求出发</small></div><div class="source-note"><span></span>基于商品资料提供建议</div></header><div class="workspace"><section id="welcome" class="welcome"><p class="eyebrow">LESS SEARCHING, BETTER CHOICES</p><h1>想买点什么？<br><span>一起找到适合你的。</span></h1><p class="lede">告诉我预算和使用场景，帮你筛选、比较，再做决定。</p><div class="suggestions"><button class="suggestion" data-prompt="想找一副 100 美元以内的耳机，平时通勤听音乐。"><span class="icon" aria-hidden="true">♫</span><strong>通勤路上的好声音</strong><small>找一副 100 美元以内的耳机 ↗</small></button><button class="suggestion" data-prompt="想买一双跑鞋，日常慢跑穿，预算 80 美元以内。"><span class="icon" aria-hidden="true">↗</span><strong>让每一步更轻松</strong><small>挑一双日常慢跑的跑鞋 ↗</small></button><button class="suggestion" data-prompt="想找一个日常用的背包，需要放下 15 英寸笔记本，预算 60 美元以内。"><span class="icon" aria-hidden="true">▱</span><strong>装下日常的小世界</strong><small>选一个能装电脑的背包 ↗</small></button></div><p class="hint">还没想好也没关系，从一个大概的想法开始。</p></section><section id="chat" class="chat" aria-label="对话记录" aria-live="polite"></section><div class="composer-area"><div id="status" class="status" role="status" aria-live="polite"></div><form id="composer" class="composer"><textarea id="input" aria-label="购买需求" placeholder="说说你想买什么，有什么特别在意的……" maxlength="8000" required></textarea><div class="composer-bottom"><span>Enter 发送 · Shift + Enter 换行</span><button id="send" class="send" aria-label="发送消息" title="发送消息" type="submit">↑</button></div></form><p class="footnote">价格为历史美元价格，仅供参考 · 可查看商品资料核对细节 · 不支持直接购买</p></div></div></main></div>
<script>
let cid=null;try{cid=localStorage.getItem('shopping-v1-conversation');if(typeof location!=='undefined'){const linked=new URLSearchParams(location.search).get('conversation');if(linked){cid=linked;localStorage.setItem('shopping-v1-conversation',cid)}}}catch{}
const chat=document.querySelector('#chat'),status=document.querySelector('#status'),send=document.querySelector('#send'),fresh=document.querySelector('#new'),input=document.querySelector('#input'),welcome=document.querySelector('#welcome'),form=document.querySelector('#composer');
let busy=false;
function saveSession(){try{if(cid)localStorage.setItem('shopping-v1-conversation',cid);else localStorage.removeItem('shopping-v1-conversation')}catch{}}
function setStatus(text,error=false){status.textContent=text;status.className='status'+(error?' error':busy?' busy':'')}
function setBusy(value){busy=value;document.querySelectorAll('button').forEach(b=>{b.disabled=value});input.readOnly=value;form.setAttribute('aria-busy',String(value));status.classList.toggle('busy',value)}
function el(tag,text){const n=document.createElement(tag);if(text!=null)n.textContent=text;return n}
// Support emphasis without HTML parsing; source text always stays inert.
function appendText(node,text){String(text).split(/([*][*][^*\\n]+[*][*])/g).forEach(part=>{if(part.startsWith('**')&&part.endsWith('**'))node.append(el('strong',part.slice(2,-2)));else node.append(el('span',part))})}
const labels={satisfied:'符合要求',violated:'不符合要求',unknown:'信息待确认',conflict:'资料存在分歧',matched:'符合偏好',not_matched:'不符合偏好'};
function render(message,scroll=true){
 welcome.classList.add('hidden');const a=el('article');a.className='message '+(message.role==='user'?'user':'assistant');const label=el('div');label.className='message-label';const avatar=el('span','✦');avatar.className='avatar';label.append(avatar,el('span','日常导购'));a.append(label);const content=el('div');appendText(content,message.content||'');content.className='message-content';a.append(content);
 if(message.price_notice&&(message.product_cards||[]).length&&!/历史/.test(message.content||''))a.append(el('small',message.price_notice));
 const cards=message.product_cards||[];
 if(cards.length){const primaryCount=cards.filter(c=>c.is_recommended).length;const hasPrimary=primaryCount>0;a.append(el('h3','可选商品 · '+(primaryCount||cards.length)+' 款'));const wrap=el('div');wrap.className='cards';const extra=el('details');extra.append(el('summary','查看其他候选 · '+(cards.length-primaryCount)+' 款'));const extraWrap=el('div');extraWrap.className='cards';extra.append(extraWrap);cards.forEach(card=>{const box=el('div');box.className='card';if(card.is_recommended){const tag=el('span','主推荐');tag.className='badge satisfied';box.append(tag)}else{box.append(el('small',card.qualification==='satisfied'?'其他可选商品':'信息待确认'))}const badge=el('span',labels[card.qualification]||'信息待确认');badge.className='badge '+(card.qualification==='satisfied'?'satisfied':card.qualification==='violated'?'violated':'');box.append(badge);if(card.scope==='hypothetical')box.append(el('small','调整条件后的备选'));box.append(el('h3',card.display_name||card.title||'候选商品'));const price=el('div',card.price&&card.price.amount!=null?card.price.amount+' 美元':'暂无价格');price.className='price';price.append(el('span','历史参考价'));box.append(price);(card.attributes||[]).forEach(attr=>{const row=el('div',(attr.label||attr.field)+'：'+(attr.text||'资料暂未说明'));row.className='attribute';box.append(row)});const evidence=el('details');evidence.append(el('summary','查看商品资料'));if(card.title)evidence.append(el('p',card.title));let evidenceCount=0;(card.attributes||[]).forEach(attr=>{if(attr.evidence&&attr.evidence.text){evidence.append(el('p',(attr.label||attr.field)+'：'+attr.evidence.text));evidenceCount++}});if(!evidenceCount)evidence.append(el('p','这项商品暂未提供更多原文细节。'));box.append(evidence);const actions=el('div');actions.className='actions';['shortlist_add','exclude','compare','select_confirmed'].forEach(action=>{const b=el('button',{shortlist_add:'加入备选',exclude:'不考虑',compare:'比较',select_confirmed:'选这件'}[action]);b.type='button';b.disabled=busy;b.dataset.requestId=crypto.randomUUID();b.onclick=async()=>{if(busy)return;setBusy(true);setStatus('正在更新你的选择…');try{const body={action,product_ids:[card.product_id],task_id:message.task_id,display_id:card.display_id,request_id:b.dataset.requestId};const result=await request('/api/conversations/'+cid+'/decision_actions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});b.dataset.requestId=crypto.randomUUID();add({role:'assistant',...result,content:result.message||(result.ok?'已更新你的选择。':'这次操作未完成，请重试。')});setStatus(result.ok?'已更新你的选择。':(result.message||'操作未完成，请重试。'),!result.ok)}catch(err){setStatus(err.message,true)}finally{setBusy(false)}};actions.append(b)});box.append(actions);if(hasPrimary&&!card.is_recommended)extraWrap.append(box);else wrap.append(box)});a.append(wrap);if(hasPrimary&&primaryCount<cards.length)a.append(extra)}
 if(message.comparison_view&&message.comparison_view.rows&&((message.comparison_view.products||message.comparison_view.product_ids||[]).length>1)){const view=message.comparison_view,table=el('table'),head=el('tr');head.append(el('th','比较维度'));const products=view.products||view.product_ids||((view.rows[0]||{}).cells||[]).map(c=>c.product_id);products.forEach((p,i)=>{const id=typeof p==='string'?p:p.product_id;const card=cards.find(c=>c.product_id===id);head.append(el('th',card?(card.display_name||card.title):((typeof p==='object'&&(p.display_name||p.title))||'商品 '+(i+1))))});table.append(head);view.rows.forEach(row=>{const tr=el('tr');tr.append(el('th',row.label));row.cells.forEach(c=>tr.append(el('td',labels[c.text]||c.text)));table.append(tr)});const tw=el('div');tw.className='table-wrap';tw.append(table);a.append(tw)}

 chat.append(a);if(scroll)a.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});
}
function add(message,scroll=true){render(typeof message==='string'?{role:'assistant',content:message}:message,scroll)}
async function request(url,options={}){let r;try{r=await fetch(url,options)}catch{throw new Error('暂时连接不上，请检查网络后重试。')}if(!r.ok){if(r.status===409)throw new Error('上一条请求还在处理中，请稍后再试。');throw new Error('暂时无法完成请求，请稍后重试。')}return r.json()}
if(cid){setBusy(true);setStatus('正在恢复对话…');request('/api/conversations/'+cid).then(data=>{chat.replaceChildren();for(const m of data.messages.filter(m=>!(m.role==='user'&&m.content.startsWith('decision_action:'))))add(m,false);setStatus('')}).catch(()=>{cid=null;saveSession();setStatus('暂时无法恢复上次对话，可以重新开始。',true)}).finally(()=>setBusy(false))}
fresh.onclick=()=>{if(busy)return;cid=null;saveSession();chat.replaceChildren();welcome.classList.remove('hidden');input.value='';setStatus('');input.focus();window.scrollTo({top:0,behavior:'smooth'})};
document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>{input.value=b.dataset.prompt;input.focus()});
input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();if(!busy)form.requestSubmit()}});
form.onsubmit=async e=>{e.preventDefault();if(busy)return;const text=input.value.trim();if(!text)return;add({role:'user',content:text});input.value='';setBusy(true);setStatus('正在了解你的需求，查找合适的商品…');try{if(!cid){cid=(await request('/api/conversations',{method:'POST'})).conversation_id;saveSession()}const result=await request('/api/conversations/'+cid+'/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text})});add({role:'assistant',...result,content:result.message});setStatus(result.kind==='execution_limited'?'这次查询尚未完成，你可以继续补充需求。':'')}catch(error){input.value=text;setStatus(error.message,true)}finally{setBusy(false);input.focus()}};
</script></body></html>
"""


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
        app.state.runtime.store.db.execute("SELECT COUNT(*) FROM conversations").fetchone()
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

    @app.post("/api/conversations/{conversation_id}/decision_actions")
    async def decision_action(conversation_id: str, body: DecisionAction):
        try:
            result = app.state.runtime.apply_decision_action(conversation_id, body.model_dump(mode="json"))
            return result
        except RuntimeBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
