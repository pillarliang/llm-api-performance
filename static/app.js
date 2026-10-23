"use strict";
const $ = id => document.getElementById(id);
let config = null, planReady = false, running = false, selectedRun = null, pollTimer = null, lastStatus = "idle", configRevision = 0;
let catalog = null, defaultConnection = null, activeTaskType = "text", catalogRevision = 0;
const parameterErrors = new Set(), parameterDrafts = new Map();
const node = (tag,text,cls) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
async function api(path, options={}) {
  const response=await fetch(path,{...options,headers:{"Content-Type":"application/json",...options.headers}});
  if(!response.ok){let message=`HTTP ${response.status}`;try{const data=await response.json();message=typeof data.detail==="string"?data.detail:JSON.stringify(data.detail);}catch{}throw new Error(message);}
  return response;
}
function notify(message){$("toast").textContent=message;$("toast").hidden=false;clearTimeout(notify.timer);notify.timer=setTimeout(()=>$("toast").hidden=true,4500);}
function statusBadge(id,text,tone=""){const badge=$(id);badge.textContent=text;badge.className=`badge ${tone}`.trim();}
function error(message){$("notice").textContent=message;$("notice").hidden=false;}
async function guarded(button,work){button.disabled=true;$("notice").hidden=true;try{await work();}catch(e){error(e.message);}finally{button.disabled=false;syncButtons();}}
function showTab(tab){if(!["experiment","history","results"].includes(tab))tab="experiment";document.querySelectorAll(".tab-panel").forEach(n=>n.hidden=n.id!==tab);document.querySelectorAll("[data-tab]").forEach(n=>{n.classList.toggle("active",n.dataset.tab===tab);n.setAttribute("aria-current",n.dataset.tab===tab?"page":"false");});history.replaceState(null,"",`#${tab}`);}
document.querySelectorAll("[data-tab]").forEach(n=>n.addEventListener("click",()=>showTab(n.dataset.tab)));
function syncButtons(){
  if(running)document.querySelectorAll(".effort-picker").forEach(p=>p.open=false);
  const noModels=!config?.models.some(m=>m.enabled!==false), unavailable=activeTaskType!=="text";
  $("start-run").disabled=!planReady||running||unavailable||noModels||parameterErrors.size>0;
  $("stop-run").hidden=!running;$("save-config").disabled=running||noModels||parameterErrors.size>0;
  $("check-plan").disabled=running||unavailable||noModels||parameterErrors.size>0;
  document.querySelectorAll(".setup-column input,.setup-column select,.setup-column button,.setup-column textarea").forEach(el=>el.disabled=running);
}
function changed(){configRevision++;planReady=false;statusBadge("plan-badge","待检查");["pending-calls","warmup-calls","skipped-calls"].forEach(id=>$(id).textContent="—");syncButtons();updateCounts();}
function updateCounts(){
  if(!config)return;
  const enabled=config.models.filter(m=>m.enabled!==false),profiles=enabled.reduce((n,m)=>n+m.profiles.length,0);
  const groups=config.tasks.length*config.input_tokens.length*config.concurrencies.length*profiles;
  statusBadge("selected-count",`已选 ${enabled.length} 个`,enabled.length?"blue":"");
  $("plan-summary").textContent=activeTaskType!=="text"?"视觉理解尚未开放":enabled.length?`${enabled.length} 个模型 · ${groups} 组组合 · ${groups*config.repetitions} 次正式请求`:"选择模型后检查运行计划";
}
function connectionPayload(){return $("connection-mode").value==="default"?{mode:"default"}:{mode:"custom",base_url:$("base-url").value.trim(),api_key:$("api-key").value.trim()};}
function configPayload(){return {config,connection:connectionPayload(),mode:$("mode").value};}
function rebindModels(){
  for(const model of config.models){model.base_url=$("base-url").value.trim();model.api_key_env=$("connection-mode").value==="default"?"DASHSCOPE_API_KEY":"CUSTOM_API_KEY";delete model.base_url_env;}
}
function resetCatalog(){
  $("model-search").value="";catalogRevision++;$("reload-picker").disabled=running;catalog=null;
  $("model-picker").replaceChildren();$("picker-status").textContent="连接已变更，点击刷新列表；也可以手动添加模型。";rebindModels();changed();
}
$("connection-mode").addEventListener("change",()=>{
  const custom=$("connection-mode").value==="custom";$("base-url").readOnly=!custom;$("api-key").readOnly=!custom;$("api-key").value="";
  $("api-key").placeholder=custom?"填写自己的 API Key":"使用服务端 SG 密钥";
  $("connection-help").textContent=custom?"填写 OpenAI 兼容接口地址和 Key。Key 仅供当前页面使用，不保存到配置或报告，刷新页面后需重新填写。":"默认连接使用服务端 SG 配置，可直接选择模型。";
  statusBadge("key-status",custom?"待填写":defaultConnection.configured?"SG 已配置":"SG 未配置",custom||!defaultConnection.configured?"amber":"green");
  if(!custom)$("base-url").value=defaultConnection.base_url;
  resetCatalog();if(!custom)loadCatalog();
});
for(const id of ["base-url","api-key"])$(id).addEventListener("input",()=>{resetCatalog();statusBadge("key-status","自定义 · 待验证","amber");});
for(const button of document.querySelectorAll("[data-task]")){
  button.addEventListener("click",()=>{
    activeTaskType=button.dataset.task;
    for(const tab of document.querySelectorAll("[data-task]")){const selected=tab===button;tab.classList.toggle("active",selected);tab.setAttribute("aria-selected",String(selected));tab.tabIndex=selected?0:-1;}
    $("text-task").hidden=activeTaskType!=="text";$("vision-task").hidden=activeTaskType!=="vision";changed();
  });
  button.addEventListener("keydown",event=>{if(["ArrowLeft","ArrowRight","Home","End"].includes(event.key)){event.preventDefault();const target=event.key==="Home"?$("text-tab"):event.key==="End"?$("vision-tab"):button===$("text-tab")?$("vision-tab"):$("text-tab");target.focus();target.click();}});
}
const fields={"experiment-name":"name","experiment-version":"version",repetitions:"repetitions",warmup:"warmup",timeout:"timeout_seconds","cache-state":"cache_state"};
for(const [id,key] of Object.entries(fields))$(id).addEventListener("input",()=>{if(!config)return;config[key]=$(id).type==="number"?Number($(id).value):$(id).value;changed();});
const parseNumbers=value=>value.split(/[,，]/).map(v=>v.trim()==="null"?null:Number(v.trim()));
$("concurrencies").addEventListener("input",()=>{if(config){config.concurrencies=parseNumbers($("concurrencies").value);changed();}});
$("input-tokens").addEventListener("change",()=>{const custom=$("input-tokens").value==="custom";$("custom-tokens-field").hidden=!custom;config.input_tokens=custom?parseNumbers($("custom-tokens").value):[Number($("input-tokens").value)];changed();});
$("custom-tokens").addEventListener("input",()=>{config.input_tokens=parseNumbers($("custom-tokens").value);changed();});
$("mode").addEventListener("change",changed);
function setConfig(value){
  config=value;parameterErrors.clear();parameterDrafts.clear();
  if(config.ui_version!==2){config.models=[];config.input_tokens=[512];config.concurrencies=[1];config.warmup=0;config.repetitions=3;config.parameters={};}
  // A saved custom connection needs a new Key after reload. Keep its endpoint and model choices.
  const savedCustom=config.ui_version===2&&config.models.some(m=>m.api_key_env.startsWith("CUSTOM_API_KEY"));
  if(savedCustom){$("connection-mode").value="custom";$("base-url").value=config.models[0].base_url;$("base-url").readOnly=false;$("api-key").readOnly=false;$("api-key").placeholder="重新填写自己的 API Key";$("connection-help").textContent="已恢复配置；请重新填写 API Key，再刷新列表或检查计划。";statusBadge("key-status","待填写 Key","amber");}
  config.ui_version=2;
  for(const [id,key] of Object.entries(fields))$(id).value=config[key];
  const preset=config.input_tokens.length===1&&[512,1024,2048].includes(config.input_tokens[0]);
  $("input-tokens").value=preset?String(config.input_tokens[0]):"custom";$("custom-tokens-field").hidden=preset;
  $("custom-tokens").value=config.input_tokens.map(v=>v===null?"null":v).join(", ");$("concurrencies").value=config.concurrencies.join(", ");
  rebindModels();renderModels();changed();
}
const efforts=[["default","服务默认"],["none","none · 不推理"],["minimal","minimal · 最小"],["low","low · 低"],["medium","medium · 中"],["high","high · 高"],["xhigh","xhigh · 更高"],["max","max · 最大"]];
function addModel(name){
  name=name.trim();if(!name||!config||running)return;
  if(config.models.some(m=>m.model===name)){notify("这个模型已添加");return;}
  let id=name;while(config.models.some(m=>m.id===id))id+="-custom";
  config.models.push({id,model:name,base_url:$("base-url").value.trim(),api_key_env:$("connection-mode").value==="default"?"DASHSCOPE_API_KEY":"CUSTOM_API_KEY",enabled:true,include_usage:true,profiles:[{name:"default",thinking_enabled:null,parameters:{}}]});
  renderModels();renderPicker();changed();
}
function removeModel(model){config.models=config.models.filter(m=>m!==model);parameterErrors.delete(model.id);parameterDrafts.delete(model.id);renderModels();renderPicker();changed();}
function renderModels(){
  $("model-list").replaceChildren();
  for(const [index,model] of config.models.entries()){
    const row=node("div",undefined,"model-row"),title=node("div",model.model,"model-title"),controls=node("div",undefined,"model-controls");
    const picker=node("details",undefined,"effort-picker"),summary=node("summary"),choices=node("div",undefined,"effort-options");
    choices.setAttribute("role","group");choices.setAttribute("aria-label",`${model.model} 推理强度（可多选）`);
    // Keep saved per-profile parameters when toggling a strength off and on.
    const available=new Map(efforts.map(([name,label])=>[name,{label,profile:{name,thinking_enabled:null,parameters:name==="default"?{}:{reasoning_effort:name}}}]));
    for(const profile of model.profiles){const label=available.get(profile.name)?.label||profile.label||profile.name;available.set(profile.name,{label,profile:structuredClone(profile)});}
    if(!model.profiles.length)model.profiles=[structuredClone(available.get("default").profile)];
    const custom=node("button","自定义请求参数","text-button parameter-toggle");custom.setAttribute("aria-expanded","false");custom.setAttribute("aria-controls",`parameters-${index}`);
    const panel=node("div",undefined,"parameter-panel");panel.id=`parameters-${index}`;panel.hidden=true;
    const field=node("label","配置档位","field"),target=node("select");target.setAttribute("aria-label",`${model.model} 参数所属档位`);field.append(target);
    const editor=node("textarea");editor.setAttribute("aria-label",`${model.model} 请求参数 JSON`);editor.spellcheck=false;
    const hint=node("p",undefined,"footnote");
    const drafts=parameterDrafts.get(model.id)||new Map();parameterDrafts.set(model.id,drafts);
    const refreshErrors=()=>{const invalid=model.profiles.some(p=>drafts.get(p.name)?.invalid);if(invalid)parameterErrors.add(model.id);else parameterErrors.delete(model.id);};
    const loadEditor=()=>{
      const profile=model.profiles.find(p=>p.name===target.value);if(!profile)return;
      const draft=drafts.get(profile.name);editor.value=draft?.text??JSON.stringify(profile.parameters||{},null,2);
      editor.setCustomValidity(draft?.invalid?"请填写有效的 JSON 对象":"");
      hint.textContent=draft?.invalid?"JSON 格式错误，请修正此档位的参数。":"仅修改当前档位；其他已选档位的请求参数保持独立。";
    };
    const refreshSelection=()=>{
      const names=model.profiles.map(p=>p.name==="default"?"服务默认":p.name);
      summary.textContent=`推理强度 · ${names.length>2?`${names.slice(0,2).join(" + ")} 等 ${names.length} 档`:names.join(" + ")}`;
      summary.setAttribute("aria-label",`${model.model} 推理强度，已选 ${names.length} 档`);
      summary.title=names.join(" + ");
      const previous=target.value;target.replaceChildren();for(const p of model.profiles)target.append(new Option(available.get(p.name)?.label||p.name,p.name));
      if(model.profiles.some(p=>p.name===previous))target.value=previous;
      else{const invalid=model.profiles.find(p=>drafts.get(p.name)?.invalid);if(invalid)target.value=invalid.name;}
      loadEditor();refreshErrors();
    };
    for(const [name,option] of available){
      const label=node("label",undefined,"effort-option"),check=node("input");check.type="checkbox";check.checked=model.profiles.some(p=>p.name===name);check.setAttribute("aria-label",`${model.model} ${option.label}`);
      check.addEventListener("change",()=>{
        if(!check.checked&&model.profiles.length===1){check.checked=true;notify("至少保留一个推理档位");return;}
        if(check.checked)model.profiles.push(structuredClone(option.profile));
        else{const profile=model.profiles.find(p=>p.name===name);option.profile=structuredClone(profile);model.profiles=model.profiles.filter(p=>p.name!==name);}
        refreshSelection();changed();
      });
      label.append(check,node("span",option.label));choices.append(label);
    }
    choices.append(node("p","可同时选择多个档位，分别生成评测组合。","footnote"));
    picker.append(summary,choices);
    summary.addEventListener("click",event=>{if(running)event.preventDefault();});
    picker.addEventListener("toggle",()=>{if(picker.open){for(const other of document.querySelectorAll(".effort-picker"))if(other!==picker)other.open=false;}});
    picker.addEventListener("keydown",event=>{if(event.key==="Escape"){picker.open=false;summary.focus();}});
    custom.addEventListener("click",()=>{panel.hidden=!panel.hidden;custom.setAttribute("aria-expanded",String(!panel.hidden));picker.open=false;});
    target.addEventListener("change",loadEditor);
    editor.addEventListener("input",()=>{
      const profile=model.profiles.find(p=>p.name===target.value);
      try{const parameters=JSON.parse(editor.value);if(!parameters||Array.isArray(parameters)||typeof parameters!=="object")throw Error();
        profile.parameters=parameters;profile.thinking_enabled=null;available.get(profile.name).profile=structuredClone(profile);
        drafts.set(profile.name,{text:editor.value,invalid:false});editor.setCustomValidity("");hint.textContent="仅修改当前档位；其他已选档位的请求参数保持独立。";
      }catch{drafts.set(profile.name,{text:editor.value,invalid:true});editor.setCustomValidity("请填写有效的 JSON 对象");hint.textContent="JSON 格式错误，请修正此档位的参数。";}
      refreshErrors();changed();
    });
    panel.append(field,hint,editor);refreshSelection();
    if(parameterErrors.has(model.id)){panel.hidden=false;custom.setAttribute("aria-expanded","true");}
    const remove=node("button","移除","text-button");remove.setAttribute("aria-label",`移除 ${model.model}`);remove.addEventListener("click",()=>removeModel(model));
    controls.append(picker,custom,remove);row.append(title,controls,panel);$("model-list").append(row);
  }
}
document.addEventListener("click",event=>{for(const picker of document.querySelectorAll(".effort-picker[open]"))if(!picker.contains(event.target))picker.open=false;});
function renderPicker(){
  $("model-picker").replaceChildren();if(!catalog)return;
  const search=$("model-search").value.trim().toLowerCase();const models=catalog.models.filter(m=>`${m.id} ${m.owned_by||""}`.toLowerCase().includes(search));
  for(const model of models){const label=node("label",undefined,"model-option"),check=node("input");check.type="checkbox";check.checked=config.models.some(m=>m.model===model.id);check.disabled=running;check.setAttribute("aria-label",`选择 ${model.id}`);
    check.addEventListener("change",()=>{if(check.checked)addModel(model.id);else removeModel(config.models.find(m=>m.model===model.id));});
    label.append(check,node("span",model.id,"model-name"),node("small",model.owned_by||""));$("model-picker").append(label);
  }
  if(!models.length)$("model-picker").append(node("div",search?"没有匹配模型，可在下方手动添加":"接口未返回模型，可在下方手动添加","empty"));
}
$("model-search").addEventListener("input",renderPicker);
$("reload-picker").addEventListener("click",()=>loadCatalog(true));
$("add-custom-model").addEventListener("click",()=>{addModel($("custom-model-name").value);$("custom-model-name").value="";});
$("custom-model-name").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();$("add-custom-model").click();}});
async function connect(){
  const data=await(await api("/api/config")).json();defaultConnection=data.connection;
  $("base-url").value=defaultConnection.base_url;statusBadge("key-status",defaultConnection.configured?"SG 已配置":"SG 未配置",defaultConnection.configured?"green":"amber");setConfig(data.config);
  $("connection-dot").classList.add("ready");$("connection-label").textContent="服务已连接";
  showTab(location.hash.slice(1));
  await Promise.all([refreshHistory(),poll(),$("connection-mode").value==="default"?loadCatalog():Promise.resolve()]);
  if($("connection-mode").value==="custom")$("picker-status").textContent="填写 Key 后刷新模型列表，或直接使用已选模型。";
}
$("save-config").addEventListener("click",()=>guarded($("save-config"),async()=>{await api("/api/config",{method:"PUT",body:JSON.stringify(configPayload())});notify("配置已保存");}));
$("check-plan").addEventListener("click",()=>guarded($("check-plan"),async()=>{
  const revision=configRevision;
  const plan=await(await api("/api/plan",{method:"POST",body:JSON.stringify(configPayload())})).json();
  if(revision!==configRevision){notify("配置已变化，请重新检查计划");return;}
  $("pending-calls").textContent=plan.pending_calls;$("warmup-calls").textContent=plan.warmup_calls;$("skipped-calls").textContent=plan.planned_cases-plan.pending_calls;statusBadge("plan-badge",plan.effective_update_mode==="full"?"全量计划":"增量计划","blue");planReady=true;notify("计划检查完成，尚未发起模型请求");
}));
$("start-run").addEventListener("click",()=>guarded($("start-run"),async()=>{
  await api("/api/runs",{method:"POST",body:JSON.stringify(configPayload())});running=true;planReady=false;await poll();notify("评测已开始");
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
      const labels={starting:"准备中",running:"运行中",finished:"本轮结束",cancelled:"已停止",error:"执行异常"};statusBadge("run-status",labels[state.status]||state.status,({starting:"blue",running:"blue",finished:"green",cancelled:"amber",error:"red"})[state.status]||"");
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
  const revision=++catalogRevision;
  $("reload-picker").disabled=true;$("picker-status").textContent="正在读取接口模型…";
  try{
    const data=await(await api("/api/models",{method:"POST",body:JSON.stringify({connection:connectionPayload(),refresh})})).json();
    if(revision!==catalogRevision)return;
    catalog=data;
    $("picker-status").textContent=catalog.stale?"刷新失败，显示上次列表；可重试或手动添加模型。":`接口返回 ${catalog.count} 个模型，可搜索、多选或手动添加。`;
    const custom=$("connection-mode").value==="custom";
    statusBadge("key-status",catalog.stale?"列表刷新失败":custom?"自定义 · 已连接":"SG 已配置",catalog.stale?"amber":"green");
    renderPicker();
  }catch(e){if(revision!==catalogRevision)return;$("picker-status").textContent=`${e.message}。可重试，或手动添加模型。`;statusBadge("key-status","列表读取失败","red");}
  finally{if(revision===catalogRevision)$("reload-picker").disabled=running;}
}
connect().catch(e=>error(e.message));
