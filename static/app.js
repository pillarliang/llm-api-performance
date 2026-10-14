"use strict";
const $ = id => document.getElementById(id);
let config = null, profileOptions = new Map(), planReady = false, running = false, selectedRun = null, pollTimer = null, lastStatus = "idle", configRevision = 0;
let catalog = null;
const node = (tag,text,cls) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
async function api(path, options={}) {
  const response=await fetch(path,{...options,headers:{"Content-Type":"application/json",...options.headers}});
  if(!response.ok){let message=`HTTP ${response.status}`;try{const data=await response.json();message=typeof data.detail==="string"?data.detail:JSON.stringify(data.detail);}catch{}throw new Error(message);}
  return response;
}
function notify(message){$("toast").textContent=message;$("toast").hidden=false;clearTimeout(notify.timer);notify.timer=setTimeout(()=>$("toast").hidden=true,4500);}
function error(message){$("notice").textContent=message;$("notice").hidden=false;}
async function guarded(button,work){button.disabled=true;$("notice").hidden=true;try{await work();}catch(e){error(e.message);}finally{button.disabled=false;syncButtons();}}
function showTab(tab){document.querySelectorAll(".tab-panel").forEach(n=>n.hidden=n.id!==tab);document.querySelectorAll("[data-tab]").forEach(n=>{n.classList.toggle("active",n.dataset.tab===tab);n.setAttribute("aria-current",n.dataset.tab===tab?"page":"false");});history.replaceState(null,"",`#${tab}`);}
document.querySelectorAll("[data-tab]").forEach(n=>n.addEventListener("click",()=>showTab(n.dataset.tab)));
function syncButtons(){ $("start-run").disabled=!planReady||running;$("stop-run").hidden=!running;$("save-config").disabled=running;$("check-plan").disabled=running; }
function changed(){configRevision++;planReady=false;$("plan-badge").textContent="待检查";["pending-calls","warmup-calls","skipped-calls"].forEach(id=>$(id).textContent="—");syncButtons();updateCounts();}
function updateCounts(){
  if(!config)return;
  const enabled=config.models.filter(m=>m.enabled!==false),profiles=enabled.reduce((n,m)=>n+m.profiles.length,0);
  const groups=config.tasks.length*config.input_tokens.length*config.concurrencies.length*profiles;
  $("stat-models").textContent=enabled.length;$("stat-groups").textContent=groups;$("stat-cases").textContent=groups*config.repetitions;$("stat-warmups").textContent=groups*config.warmup;
}
const fields={"experiment-name":"name","experiment-version":"version",repetitions:"repetitions",warmup:"warmup",timeout:"timeout_seconds","cache-state":"cache_state"};
for(const [id,key] of Object.entries(fields))$(id).addEventListener("input",()=>{if(!config)return;config[key]=$(id).type==="number"?Number($(id).value):$(id).value;changed();});
for(const [id,key] of [["input-tokens","input_tokens"],["concurrencies","concurrencies"]])$(id).addEventListener("input",()=>{if(!config)return;config[key]=$(id).value.split(/[,，]/).map(v=>v.trim()==="null"?null:Number(v.trim()));changed();});
$("mode").addEventListener("change",changed);
function setConfig(value){
  config=value;profileOptions=new Map(config.models.map(m=>[m.id,structuredClone(m.profiles)]));
  for(const [id,key] of Object.entries(fields))$(id).value=config[key];
  $("input-tokens").value=config.input_tokens.map(v=>v===null?"null":v).join(", ");$("concurrencies").value=config.concurrencies.join(", ");
  renderModels();changed();
  if(catalog)renderCatalog();
}
function renderModels(){
  $("model-list").replaceChildren();
  for(const [index,model] of config.models.entries()){
    const row=node("div",undefined,"model-row"),title=node("div",undefined,"model-title"),checkbox=node("input");checkbox.type="checkbox";checkbox.id=`model-${index}`;checkbox.checked=model.enabled!==false;
    const label=node("label",model.id);label.htmlFor=checkbox.id;
    const badge=node("span",model.enabled!==false?"已启用":"未启用",`badge ${model.enabled!==false?"green":""}`);
    title.append(checkbox,label,badge);row.append(title,node("div",model.base_url,"model-meta"));
    const profiles=node("div",undefined,"profiles");
    for(const profile of profileOptions.get(model.id)||[]){
      const pill=node("label",undefined,"profile"),check=node("input");check.type="checkbox";check.checked=model.profiles.some(p=>p.name===profile.name);check.disabled=model.enabled===false;
      check.addEventListener("change",()=>{if(check.checked)model.profiles.push(structuredClone(profile));else model.profiles=model.profiles.filter(p=>p.name!==profile.name);changed();});
      pill.append(check,node("span",profile.label||profile.name));pill.title=[profile.description,JSON.stringify(profile.parameters)].filter(Boolean).join("\n");profiles.append(pill);
    }
    checkbox.addEventListener("change",()=>{model.enabled=checkbox.checked;changed();renderModels();});
    row.append(profiles);$("model-list").append(row);
  }
  $("endpoint-info").replaceChildren();
  const endpoints=[...new Set(config.models.filter(m=>m.enabled!==false).map(m=>m.base_url))];
  for(const endpoint of endpoints)$("endpoint-info").append(node("strong","OpenAI 兼容接口"),node("div",endpoint));
}
async function connect(){
  const data=await(await api("/api/config")).json();setConfig(data.config);$("yaml-editor").value=data.yaml;
  const ready=data.credentials.every(c=>c.configured);$("key-status").textContent=ready?"已配置":"部分未配置";$("key-status").className=`badge ${ready?"green":"amber"}`;
  $("connection-dot").classList.add("ready");$("connection-label").textContent="服务已连接";
  const tab=location.hash.slice(1);if(["experiment","catalog","history","results"].includes(tab))showTab(tab);
  await Promise.all([refreshHistory(),poll(),loadCatalog()]);
}
$("toggle-yaml").addEventListener("click",()=>guarded($("toggle-yaml"),async()=>{
  if(!$("yaml-panel").hidden){$("yaml-panel").hidden=true;return;}
  const data=await(await api("/api/config/validate",{method:"POST",body:JSON.stringify({config})})).json();$("yaml-editor").value=data.yaml;$("yaml-panel").hidden=false;
}));
$("apply-yaml").addEventListener("click",()=>guarded($("apply-yaml"),async()=>{
  const data=await(await api("/api/config/validate",{method:"POST",body:JSON.stringify({yaml:$("yaml-editor").value})})).json();setConfig(data.config);$("yaml-panel").hidden=true;notify("配置已应用，检查计划后即可运行");
}));
$("save-config").addEventListener("click",()=>guarded($("save-config"),async()=>{await api("/api/config",{method:"PUT",body:JSON.stringify({config})});notify("配置已保存");}));
$("check-plan").addEventListener("click",()=>guarded($("check-plan"),async()=>{
  const revision=configRevision;
  const plan=await(await api("/api/plan",{method:"POST",body:JSON.stringify({config,mode:$("mode").value})})).json();
  if(revision!==configRevision){notify("配置已变化，请重新检查计划");return;}
  $("pending-calls").textContent=plan.pending_calls;$("warmup-calls").textContent=plan.warmup_calls;$("skipped-calls").textContent=plan.planned_cases-plan.pending_calls;$("plan-badge").textContent=plan.effective_update_mode==="full"?"全量计划":"增量计划";planReady=true;notify("计划检查完成，尚未发起模型请求");
}));
$("start-run").addEventListener("click",()=>guarded($("start-run"),async()=>{
  await api("/api/runs",{method:"POST",body:JSON.stringify({config,mode:$("mode").value})});running=true;planReady=false;await poll();notify("评测已开始");
}));
$("stop-run").addEventListener("click",()=>guarded($("stop-run"),async()=>{await api("/api/stop",{method:"POST",body:"{}"});await poll();notify("已停止，进度已保存，可通过增量模式续跑");}));
async function poll(){
  clearTimeout(pollTimer);
  try{
    const state=await(await api("/api/status")).json();running=["starting","running"].includes(state.status);$("connection-label").textContent="服务已连接";$("connection-dot").classList.add("ready");
    if(state.path){
      $("events-panel").hidden=false;$("run-progress").hidden=false;
      const completed=state.session_attempts||0,total=state.plan.pending_calls;$("progress-bar").style.width=`${total?Math.min(100,100*completed/total):100}%`;
      $("progress-label").textContent=`本轮 ${completed} / ${total} 次正式尝试 · ${state.session_successes||0} 次成功`;
      const labels={starting:"准备中",running:"运行中",finished:"本轮结束",cancelled:"已停止",error:"执行异常"};$("run-status").textContent=labels[state.status]||state.status;
      $("events").textContent=(state.events||[]).map(e=>`${e.at.slice(11,19)}  ${e.event}  ${e.case_id} ${e.error_type||""}`).join("\n")||"正在等待请求事件…";
      $("events").scrollTop=$("events").scrollHeight;
      if(state.error)error(state.error);
    }
    if(lastStatus!==state.status&&!running&&state.status!=="idle"){planReady=false;await refreshHistory();}
    lastStatus=state.status;syncButtons();
  }catch(e){$("connection-label").textContent="连接中断";$("connection-dot").classList.remove("ready");error(e.message);}
  pollTimer=setTimeout(poll,running?1500:6000);
}
async function refreshHistory(){
  const runs=await(await api("/api/runs")).json();$("history-count").textContent=runs.length;$("history-rows").replaceChildren();$("history-empty").hidden=!!runs.length;
  for(const run of runs){
    const tr=node("tr"),identity=node("td",run.name,"mono");identity.append(node("span",run.version,"cell-sub"));
    const time=node("td",new Date(run.started_at).toLocaleString("zh-CN"));time.append(node("span",run.run_id,"cell-sub"));
    const status=node("td");status.append(node("span",run.status==="complete"?"已完成":"待补跑",`badge ${run.status==="complete"?"green":"amber"}`));
    const action=node("td"),button=node("button","查看结果");button.addEventListener("click",()=>guarded(button,()=>viewRun(run)));action.append(button);
    tr.append(identity,time,status,node("td",`${run.totals.successful_cases} / ${run.totals.planned_cases}`,"mono"),node("td",String(run.totals.requests),"mono"),action);$("history-rows").append(tr);
  }
}
$("refresh-history").addEventListener("click",()=>guarded($("refresh-history"),refreshHistory));
function artifactPath(path,file){return `/api/artifact/${path.split("/").map(encodeURIComponent).join("/")}/${file}`;}
async function viewRun(run){const data=await(await api(artifactPath(run.path,"results.json"))).json();selectedRun=run;window.renderReport($("report"),data);$("download-json").disabled=false;$("download-html").disabled=false;showTab("results");}
async function download(file){if(!selectedRun)return;const blob=await(await api(artifactPath(selectedRun.path,file))).blob();const url=URL.createObjectURL(blob),a=node("a");a.href=url;a.download=`${selectedRun.name}-${selectedRun.run_id}-${file}`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
$("download-json").addEventListener("click",()=>guarded($("download-json"),()=>download("results.json")));
$("download-html").addEventListener("click",()=>guarded($("download-html"),()=>download("report.html")));
async function loadCatalog(refresh=false){
  $("refresh-models").disabled=true;$("catalog-status").textContent="正在读取端点模型列表…";
  try{
    catalog=await(await api(`/api/models${refresh?"?refresh=true":""}`)).json();
    $("catalog-count").textContent=catalog.count;
    $("catalog-endpoint").textContent=catalog.base_url;
    $("catalog-status").textContent=`${catalog.stale?"缓存 · 刷新失败":"已同步"} · ${new Date(catalog.fetched_at).toLocaleString("zh-CN")}`;
    $("catalog-error").hidden=!catalog.stale;$("catalog-error").textContent=catalog.error||"";
    const provider=$("catalog-provider"),selected=provider.value;provider.replaceChildren(new Option("全部归属方",""));
    [...new Set(catalog.models.map(model=>model.owned_by||"未提供"))].sort().forEach(owner=>provider.append(new Option(owner,owner)));
    provider.value=selected;$("download-models").disabled=false;renderCatalog();
  }catch(e){$("catalog-error").textContent=e.message;$("catalog-error").hidden=false;$("catalog-status").textContent="读取失败，可点击刷新重试";}
  finally{$("refresh-models").disabled=false;}
}
function renderCatalog(){
  if(!catalog)return;
  const search=$("catalog-search").value.trim().toLowerCase(),owner=$("catalog-provider").value;
  const focus=$("catalog-focus").value;
  const family=id=>/(?:^|\/)glm-?5\.3(?:-|$)/i.test(id)?"glm53":/(?:^|\/)glm-?5\.2(?:-|$)/i.test(id)?"glm52":/^qwen3\.8(?:-|$)/i.test(id)?"qwen38":"";
  const primary=["glm-5.3","glm-5.2","qwen3.8-max","qwen3.8-flash"];
  const rank=id=>primary.includes(id)?primary.indexOf(id):family(id)?4:5;
  const models=catalog.models.filter(model=>(!search||`${model.id} ${model.owned_by||""}`.toLowerCase().includes(search))&&(!owner||(model.owned_by||"未提供")===owner)&&(focus==="all"||(focus==="focus"?!!family(model.id):family(model.id)===focus))).sort((a,b)=>rank(a.id)-rank(b.id)||a.id.localeCompare(b.id));
  $("catalog-visible-count").textContent=`显示 ${models.length} / ${catalog.count} 个模型`;
  $("catalog-rows").replaceChildren();$("catalog-empty").hidden=models.length>0;
  for(const model of models){
    const tr=node("tr"),identity=node("td",model.id,"mono"),provider=node("td",model.owned_by||"—"),date=node("td",model.created?new Date(model.created*1000).toLocaleDateString("zh-CN"):"—","muted");
    const existing=config?.models.some(item=>item.model===model.id&&item.base_url.replace(/\/$/,"")===catalog.base_url.replace(/\/$/,""));
    const status=node("td");status.append(node("span",existing?"已加入实验":"端点可用",`badge ${existing?"green":"blue"}`));
    const action=node("td"),button=node("button",existing?"已加入":"加入实验");button.disabled=existing||!config;
    button.addEventListener("click",()=>{
      let id=model.id;while(config.models.some(item=>item.id===id))id+="-endpoint";
      const added={id,model:model.id,base_url:catalog.base_url,api_key_env:"DASHSCOPE_API_KEY",enabled:false,include_usage:true,profiles:[{name:"default",thinking_enabled:null,parameters:{}}]};
      config.models.push(added);profileOptions.set(id,structuredClone(added.profiles));renderModels();changed();renderCatalog();notify(`${model.id} 已加入实验；勾选启用后可检查计划`);
    });
    action.append(button);tr.append(identity,provider,date,status,action);$("catalog-rows").append(tr);
  }
}
$("browse-models").addEventListener("click",()=>showTab("catalog"));
$("refresh-models").addEventListener("click",()=>loadCatalog(true));
$("catalog-search").addEventListener("input",renderCatalog);
$("catalog-provider").addEventListener("change",renderCatalog);
$("catalog-focus").addEventListener("change",renderCatalog);
$("download-models").addEventListener("click",()=>{
  if(!catalog)return;
  const url=URL.createObjectURL(new Blob([JSON.stringify(catalog,null,2)],{type:"application/json"})),a=node("a");a.href=url;a.download="endpoint-models.json";document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
connect().catch(e=>error(e.message));
