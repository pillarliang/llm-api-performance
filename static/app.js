"use strict";
const $ = id => document.getElementById(id);
let config = null, busy = false, running = false, selectedRun = null, pollTimer = null, lastStatus = "idle";
let catalog = null, defaultConnection = null, activeProviderId = null, catalogRevision = 0;
const providerKeys = new Map(), providerCatalogs = new Map();
const keyStore = window.providerKeyStore;
const parameterErrors = new Set(), parameterDrafts = new Map();
let inputTokenError = "", workspaceReady = false;
const workspaceSlot = "llm-performance.workspace.v1";
function saveWorkspace(){
  if(!workspaceReady||!config)return false;
  try{
    localStorage.setItem(workspaceSlot,JSON.stringify({config:{...config,models:[]},mode:$("mode").value,activeProviderId}));
    $("local-save-status").textContent="已自动保存到本机";return true;
  }catch{$("local-save-status").textContent="本机存储不可用，刷新将丢失修改";return false;}
}
const node = (tag,text,cls) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
async function api(path, options={}) {
  const response=await fetch(path,{...options,headers:{"Content-Type":"application/json",...options.headers}});
  if(!response.ok){let message=`HTTP ${response.status}`;try{const data=await response.json();message=typeof data.detail==="string"?data.detail:JSON.stringify(data.detail);}catch{}throw new Error(message);}
  return response;
}
function notify(message){$("toast").textContent=message;$("toast").hidden=false;clearTimeout(notify.timer);notify.timer=setTimeout(()=>$("toast").hidden=true,4500);}
function statusBadge(id,text,tone=""){const badge=$(id);badge.textContent=text;badge.className=`badge ${tone}`.trim();}
function error(message){$("notice").textContent=message;$("notice").hidden=false;}
async function guarded(button,work){if(busy)return;busy=true;$("notice").hidden=true;syncButtons();try{await work();}catch(e){error(e.message);}finally{busy=false;syncButtons();}}
function showTab(tab){if(!["experiment","history","results"].includes(tab))tab="experiment";document.querySelectorAll(".tab-panel").forEach(n=>n.hidden=n.id!==tab);document.querySelectorAll("[data-tab]").forEach(n=>{n.classList.toggle("active",n.dataset.tab===tab);n.setAttribute("aria-current",n.dataset.tab===tab?"page":"false");});history.replaceState(null,"",`#${tab}`);}
document.querySelectorAll("[data-tab]").forEach(n=>n.addEventListener("click",()=>showTab(n.dataset.tab)));
function activeProvider(){return config?.providers.find(p=>p.id===activeProviderId);}
function keyStorageHint(){
  const provider=activeProvider(),custom=provider?.mode==="custom";
  const suggestion=provider&&keyStore.suggestBase(provider);
  $("base-url-hint").hidden=!suggestion;
  $("base-url-suggestion").textContent=suggestion||"";
  $("connection-help").hidden=!custom;$("clear-local-key").hidden=!custom;
  $("connection-help").textContent=custom&&!keyStore.endpoint(provider)?"先填写有效的 Base URL，再填写 Key。":keyStore.available?"Key 仅保存在当前浏览器，不写入共享配置。":"浏览器无法保存 Key，当前仅在本次页面中有效。";
}
function syncButtons(){
  const locked=busy;
  if(locked)document.querySelectorAll(".effort-picker,.token-picker").forEach(p=>p.open=false);
  document.querySelectorAll(".setup-column input,.setup-column select,.setup-column button,.setup-column textarea").forEach(el=>el.disabled=locked);
  $("start-run").disabled=locked||!config?.models.some(m=>m.enabled!==false)||parameterErrors.size>0||!!inputTokenError;
  $("start-run").textContent=busy?"正在提交…":"开始评测 →";
  $("remove-provider").disabled=locked||config?.providers.length<=1;
}
function changed(){updateCounts();syncButtons();saveWorkspace();}
function updateCounts(){
  if(!config)return;
  const enabled=config.models.filter(m=>m.enabled!==false),profiles=enabled.reduce((n,m)=>n+m.profiles.length,0);
  const groups=config.tasks.length*config.input_tokens.length*config.concurrencies.length*profiles;
  const sources=new Set(enabled.map(m=>m.provider_id)).size;
  statusBadge("selected-count",`${enabled.length} 个模型 · ${sources} 个来源`,enabled.length?"blue":"");
  $("selection-empty").hidden=enabled.length>0;
  $("plan-summary").textContent=enabled.length?`${sources} 个来源 · ${enabled.length} 个模型 · ${groups} 组组合`:"选择模型后开始评测";
  {
    statusBadge("plan-badge","预计");$("pending-calls").textContent=groups*config.repetitions||"—";
    $("retry-budget").textContent=config.max_retries?`最多 ${config.max_retries} 次 / 项`:"关闭";
    $("warmup-calls").textContent=groups*config.warmup;$("skipped-row").hidden=true;
  }
}
function connectionPayload(provider=activeProvider()){
  return provider.mode==="default"?{mode:"default",base_url:provider.base_url}:{mode:"custom",base_url:provider.base_url,api_key:providerKeys.get(provider.id)||""};
}
function configPayload(){
  rebindModels();
  return {config:structuredClone(config),connections:config.providers.map(p=>({id:p.id,api_key:providerKeys.get(p.id)||""})),mode:$("mode").value};
}
function rebindModels(){
  for(const model of config.models){const provider=config.providers.find(p=>p.id===model.provider_id);
    model.base_url=provider.base_url;model.provider_name=provider.name;
    model.api_key_env=provider.mode==="default"?"DASHSCOPE_API_KEY":(model.api_key_env.startsWith("CUSTOM_API_KEY")?model.api_key_env:"CUSTOM_API_KEY");delete model.base_url_env;
  }
}
function providerStatus(provider){
  const state=providerCatalogs.get(provider.id);
  if(state?.error)return ["读取失败","red"];
  if(state?.data&&!state.data.stale)return ["已连接","green"];
  if(state?.data?.stale)return ["列表已过期","amber"];
  return provider.mode==="default"?(defaultConnection.configured?["已配置","green"]:["待配置","amber"]):providerKeys.get(provider.id)?["待验证","amber"]:["待填写 Key","amber"];
}
function renderProviderList(){
  $("provider-list").replaceChildren();$("picker-provider").replaceChildren();
  for(const provider of config.providers){
    const button=node("button",undefined,"provider-item");button.dataset.providerId=provider.id;
    button.classList.toggle("active",provider.id===activeProviderId);button.setAttribute("aria-pressed",String(provider.id===activeProviderId));
    const count=config.models.filter(m=>m.provider_id===provider.id).length;
    const [text,tone]=providerStatus(provider),status=node("span",text,`badge ${tone}`);
    button.append(node("strong",provider.name||"未命名来源"),node("span",`${count} 个已选模型`,"muted"),status);
    button.addEventListener("click",()=>selectProvider(provider.id));$("provider-list").append(button);
    $("picker-provider").append(new Option(provider.name||"未命名来源",provider.id));
  }
  $("picker-provider").value=activeProviderId;
  const [text,tone]=providerStatus(activeProvider());statusBadge("key-status",text,tone);syncButtons();
}
function selectProvider(id){
  activeProviderId=id;catalogRevision++;saveWorkspace();
  const provider=activeProvider(),custom=provider.mode==="custom";
  $("provider-name").value=provider.name;$("connection-mode").value=provider.mode;
  $("base-url").value=provider.base_url;$("base-url").readOnly=!custom;
  $("api-key").value=providerKeys.get(id)||"";$("api-key").readOnly=!custom;
  $("api-key").placeholder=custom?"填写后自动保存到本机":"使用服务端密钥";keyStorageHint();
  $("model-search").value="";catalog=providerCatalogs.get(id)?.data||null;renderPicker();renderProviderList();
  $("picker-status").textContent=catalog?`${catalog.count} 个可选模型`:"填写连接配置后刷新列表，或手动添加模型。";
  if(!catalog&&(custom?provider.base_url&&providerKeys.get(id):defaultConnection.configured))loadCatalog();
}
function resetCatalog(){
  catalogRevision++;providerCatalogs.delete(activeProviderId);catalog=null;renderPicker();
  $("picker-status").textContent="连接已变更，刷新列表或手动添加模型。";rebindModels();renderProviderList();changed();
}
$("add-provider").addEventListener("click",()=>{
  const provider={id:`source-${crypto.randomUUID?.()||Date.now().toString(36)+Math.random().toString(36).slice(2)}`,name:`连接源 ${config.providers.length+1}`,mode:"custom",base_url:""};
  config.providers.push(provider);selectProvider(provider.id);changed();$("provider-name").focus();$("provider-name").select();
});
$("remove-provider").addEventListener("click",()=>{
  if(config.providers.length<=1)return;
  const id=activeProviderId;
  if(!keyStore.clear(id))notify("浏览器无法清除已保存的 Key，请在浏览器设置中清除此站点数据。");
  for(const model of config.models.filter(m=>m.provider_id===id)){parameterErrors.delete(model.id);parameterDrafts.delete(model.id);}
  config.models=config.models.filter(m=>m.provider_id!==id);config.providers=config.providers.filter(p=>p.id!==id);
  providerKeys.delete(id);providerCatalogs.delete(id);selectProvider(config.providers[0].id);renderModels();changed();
});
$("picker-provider").addEventListener("change",()=>selectProvider($("picker-provider").value));
$("provider-name").addEventListener("input",()=>{activeProvider().name=$("provider-name").value;rebindModels();renderProviderList();document.querySelectorAll(".model-source").forEach(el=>{el.textContent=config.providers.find(p=>p.id===el.dataset.providerId)?.name||"未命名来源";});changed();});
$("connection-mode").addEventListener("change",()=>{
  const provider=activeProvider();provider.mode=$("connection-mode").value;providerKeys.delete(provider.id);
  if(provider.mode==="default")provider.base_url=defaultConnection.base_url;
  else providerKeys.set(provider.id,keyStore.read(provider));
  providerCatalogs.delete(provider.id);rebindModels();selectProvider(provider.id);changed();
});
$("base-url").addEventListener("input",()=>{
  const provider=activeProvider(),previous=keyStore.endpoint(provider);provider.base_url=$("base-url").value.trim();
  if(previous!==keyStore.endpoint(provider)){providerKeys.set(provider.id,keyStore.read(provider));$("api-key").value=providerKeys.get(provider.id)||"";}
  keyStorageHint();resetCatalog();
});
$("use-base-url").addEventListener("click",()=>{
  const suggestion=keyStore.suggestBase(activeProvider());if(!suggestion)return;
  $("base-url").value=suggestion;$("base-url").dispatchEvent(new Event("input"));
  if(!providerKeys.get(activeProviderId)){$("api-key").focus();notify("地址已更新，请填写此地址的 Key 后刷新列表");}
});
$("api-key").addEventListener("input",()=>{
  const value=$("api-key").value.trim();providerKeys.set(activeProviderId,value);keyStore.write(activeProvider(),value);keyStorageHint();resetCatalog();
});
$("clear-local-key").addEventListener("click",()=>{
  const cleared=keyStore.clear(activeProviderId);providerKeys.delete(activeProviderId);$("api-key").value="";keyStorageHint();resetCatalog();
  notify(cleared?"已清除此连接源的本机 Key":"当前页面 Key 已清除；请在浏览器设置中清除此站点数据。");
});
window.addEventListener("storage",event=>{
  if(!config)return;
  let affected=false;
  for(const provider of config.providers){const slot=keyStore.slot(provider);if(!slot||(event.key!==null&&event.key!==slot))continue;
    providerKeys.set(provider.id,keyStore.read(provider));providerCatalogs.delete(provider.id);affected=true;
    if(provider.id===activeProviderId){catalogRevision++;catalog=null;$("api-key").value=providerKeys.get(provider.id)||"";renderPicker();$("picker-status").textContent="本机 Key 已更新，可刷新模型列表。";}
  }
  if(affected){keyStorageHint();renderProviderList();syncButtons();}
});
const fields={"experiment-name":"name","experiment-version":"version",repetitions:"repetitions",warmup:"warmup",timeout:"timeout_seconds","max-retries":"max_retries","cache-state":"cache_state"};
for(const [id,key] of Object.entries(fields))$(id).addEventListener("input",()=>{if(!config)return;config[key]=$(id).type==="number"?Number($(id).value):$(id).value;changed();});
const parseNumbers=value=>value.split(/[,，]/).map(v=>v.trim()==="null"?null:Number(v.trim()));
$("concurrencies").addEventListener("input",()=>{if(config){config.concurrencies=parseNumbers($("concurrencies").value);changed();}});
function updateInputTokens(){
  if(!config)return;
  const selected=[...$("input-tokens").querySelectorAll("input:checked")].map(input=>input.value);
  const custom=selected.includes("custom");$("custom-tokens-field").hidden=!custom;
  const values=selected.filter(value=>value!=="custom").map(Number);
  inputTokenError="";
  if(custom){
    const parts=$("custom-tokens").value.split(/[,，]/).map(value=>value.trim());
    const valid=parts.every(value=>value==="null"||(/^\d+$/.test(value)&&Number.isSafeInteger(Number(value))&&Number(value)>0));
    if(valid)values.push(...parts.map(value=>value==="null"?null:Number(value)));
    else inputTokenError="自定义档位请填写正整数，多个值用逗号分隔";
  }
  config.input_tokens=[...new Set(values)];
  if(!inputTokenError&&!values.length)inputTokenError="至少选择一个输入 Token 档位";
  $("input-token-error").textContent=inputTokenError;$("input-token-error").hidden=!inputTokenError;
  $("custom-tokens").setAttribute("aria-invalid",String(custom&&!!inputTokenError));
  const labels=config.input_tokens.map(value=>value===null?"原始输入":String(value));
  if(custom&&inputTokenError)labels.push("自定义（待填写）");
  $("token-selection").textContent=labels.join("、")||"请选择档位";
  changed();
}
$("input-tokens").addEventListener("change",updateInputTokens);
$("custom-tokens").addEventListener("input",updateInputTokens);
$("mode").addEventListener("change",changed);
$("token-picker").addEventListener("click",event=>{if(busy)event.preventDefault();});
document.addEventListener("click",event=>{if(!$("token-picker").contains(event.target))$("token-picker").open=false;});
$("token-picker").addEventListener("keydown",event=>{if(event.key==="Escape"){$("token-picker").open=false;$("token-picker").querySelector("summary").focus();}});
function setConfig(value,restoreOrphans=false){
  config=value;parameterErrors.clear();parameterDrafts.clear();providerKeys.clear();providerCatalogs.clear();
  if(![2,3].includes(config.ui_version)){config.models=[];config.input_tokens=[512];config.concurrencies=[1];config.warmup=0;config.repetitions=3;config.parameters={};}
  if(!config.providers?.length){
    config.providers=[];
    const legacy=new Map();
    for(const model of config.models){
      const custom=model.api_key_env.startsWith("CUSTOM_API_KEY"),key=JSON.stringify([model.base_url,model.api_key_env]);
      if(!legacy.has(key)){const provider={id:`source-${legacy.size+1}`,name:custom?`自定义来源 ${legacy.size+1}`:"阿里云 SG",mode:custom?"custom":"default",base_url:custom?model.base_url:defaultConnection.base_url};legacy.set(key,provider);config.providers.push(provider);}
      model.provider_id=legacy.get(key).id;
    }
    if(!config.providers.length)config.providers=[{id:"default",name:"阿里云 SG",mode:"default",base_url:defaultConnection.base_url}];
  }
  if(restoreOrphans)for(const provider of keyStore.savedProviders())if(!config.providers.some(p=>p.id===provider.id))config.providers.push(provider);
  for(const provider of config.providers)if(provider.mode==="default")provider.base_url=defaultConnection.base_url;
  for(const provider of config.providers)providerKeys.set(provider.id,keyStore.read(provider));
  // Model selection belongs to this page session, including legacy saved selections.
  config.models=[];
  config.ui_version=3;config.max_retries??=2;
  for(const [id,key] of Object.entries(fields))$(id).value=config[key];
  const customTokens=config.input_tokens.filter(value=>![512,1024,2048].includes(value));
  $("input-tokens").querySelectorAll("input").forEach(input=>input.checked=input.value==="custom"?customTokens.length>0:config.input_tokens.includes(Number(input.value)));
  $("custom-tokens").value=customTokens.map(value=>value===null?"null":value).join(", ");
  updateInputTokens();$("concurrencies").value=config.concurrencies.join(", ");
  rebindModels();renderModels();selectProvider(config.providers[0].id);changed();
}
const efforts=[["default","服务默认"],["none","none · 不推理"],["minimal","minimal · 最小"],["low","low · 低"],["medium","medium · 中"],["high","high · 高"],["xhigh","xhigh · 更高"],["max","max · 最大"]];
function addModel(name){
  name=name.trim();if(!name||!config||busy)return;
  if(config.models.some(m=>m.model===name&&m.provider_id===activeProviderId)){notify("此来源的模型已添加");return;}
  const provider=activeProvider();
  const id=JSON.stringify([provider.id,name]);
  config.models.push({id,provider_id:provider.id,provider_name:provider.name,model:name,base_url:provider.base_url,api_key_env:provider.mode==="default"?"DASHSCOPE_API_KEY":"CUSTOM_API_KEY",enabled:true,include_usage:true,profiles:[{name:"default",thinking_enabled:null,parameters:{}}]});
  renderModels();renderPicker();renderProviderList();changed();
}
function removeModel(model){config.models=config.models.filter(m=>m!==model);parameterErrors.delete(model.id);parameterDrafts.delete(model.id);renderModels();renderPicker();renderProviderList();changed();}
function renderModels(){
  $("model-list").replaceChildren();
  for(const [index,model] of config.models.entries()){
    const row=node("div",undefined,"model-row"),title=node("div",model.model,"model-title"),controls=node("div",undefined,"model-controls");
    row.dataset.providerId=model.provider_id;row.dataset.modelId=model.id;
    const source=node("span",model.provider_name,"model-source");source.dataset.providerId=model.provider_id;title.append(source);
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
    summary.addEventListener("click",event=>{if(running||busy)event.preventDefault();});
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
  for(const model of models){const label=node("label",undefined,"model-option"),check=node("input");check.type="checkbox";check.checked=config.models.some(m=>m.model===model.id&&m.provider_id===activeProviderId);check.disabled=running||busy;check.setAttribute("aria-label",`选择 ${model.id}`);
    check.addEventListener("change",()=>{if(check.checked)addModel(model.id);else removeModel(config.models.find(m=>m.model===model.id&&m.provider_id===activeProviderId));});
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
  let saved=null;
  try{saved=JSON.parse(localStorage.getItem(workspaceSlot));}catch{}
  if(saved?.config?.ui_version===3&&Array.isArray(saved.config.providers)&&saved.config.providers.length){
    setConfig(saved.config);$("mode").value=saved.mode==="incremental"?"incremental":"full";
    if(config.providers.some(p=>p.id===saved.activeProviderId))selectProvider(saved.activeProviderId);
  }else setConfig(data.config,true);
  workspaceReady=true;saveWorkspace();
  $("connection-dot").classList.add("ready");$("connection-label").textContent="服务已连接";showTab(location.hash.slice(1));
  await Promise.all([refreshHistory(),poll()]);
}

$("start-run").addEventListener("click",()=>guarded($("start-run"),async()=>{
  const state=await(await api("/api/runs",{method:"POST",body:JSON.stringify(configPayload())})).json();
  running=true;showTab("history");await poll();notify("后台任务已创建，可继续新建评测");
}));
function renderJobs(jobs){
  $("job-list").replaceChildren();$("jobs-empty").hidden=jobs.length>0;
  const active=jobs.filter(job=>["starting","running"].includes(job.status)).length;
  $("active-count").textContent=active?`${active} 个运行中`:"";
  for(const job of jobs){
    const live=["starting","running"].includes(job.status),p=job.progress||{};
    const total=job.plan?.pending_calls||0,completed=p.completed||0,percent=total?Math.min(100,Math.round(completed/total*100)):live?0:100;
    const card=node("article",undefined,"job-card");card.dataset.jobId=job.job_id;
    const head=node("div",undefined,"job-heading"),title=node("div"),actions=node("div",undefined,"actions");
    title.append(node("h3",`${job.name} · ${job.version}`),node("span",new Date(job.started_at).toLocaleString("zh-CN"),"muted"));
    const labels={starting:"准备中",running:"运行中",finished:p.failed?"已结束 · 有失败项":"已完成",cancelled:"已停止",error:"执行异常",interrupted:"已中断"};
    actions.append(node("span",labels[job.status]||job.status,`badge ${live?"blue":job.status==="finished"&&!p.failed?"green":"amber"}`));
    if(live){
      const stop=node("button","停止任务","text-button danger");
      stop.addEventListener("click",()=>guarded(stop,async()=>{await api(`/api/jobs/${encodeURIComponent(job.job_id)}/stop`,{method:"POST"});await poll();}));actions.append(stop);
    }else if(job.has_results){const view=node("button","查看结果");view.addEventListener("click",()=>guarded(view,()=>viewRun({...job,run_id:job.path.split("/").pop()})));actions.append(view);}
    head.append(title,actions);card.append(head);
    if(job.targets?.length)card.append(node("p",job.targets.map(t=>`${t.provider} / ${t.model}`).join(" · "),"job-targets"));
    const progress=node("div",undefined,"progress");progress.setAttribute("role","progressbar");progress.setAttribute("aria-label",`${job.name} 完成进度`);progress.setAttribute("aria-valuenow",String(completed));progress.setAttribute("aria-valuemin","0");progress.setAttribute("aria-valuemax",String(total||1));
    const fill=node("div");fill.style.width=`${percent}%`;progress.append(fill);
    const counts=node("div",undefined,"job-counts");counts.append(node("strong",`已完成 ${completed} / ${total} 项`),node("span",`${percent}%`,"mono"));
    card.append(counts,progress,node("p",`成功 ${p.succeeded||0} · 失败 ${p.failed||0} · 请求中 ${p.active||0} · 等待重试 ${p.retrying||0} · 已重试 ${p.retries||0} 次`,"job-detail"));
    if(job.plan?.warmup_calls)card.append(node("p",`预热 ${p.warmups||0} / ${job.plan.warmup_calls} 次`,"muted"));
    if(job.plan?.planned_cases>total)card.append(node("p",`已有 ${job.plan.planned_cases-total} 项成功，本轮跳过`,"muted"));
    if(job.error)card.append(node("p",job.error,"field-error"));
    $("job-list").append(card);
  }
}
async function poll(){
  clearTimeout(pollTimer);
  try{
    const jobs=await(await api("/api/jobs")).json();
    running=jobs.some(job=>["starting","running"].includes(job.status));
    $("connection-label").textContent="服务已连接";$("connection-dot").classList.add("ready");
    renderJobs(jobs);
    const signature=jobs.map(job=>`${job.job_id}:${job.status}`).join("|");
    if(signature!==lastStatus){await refreshHistory();lastStatus=signature;}
    syncButtons();
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
$("refresh-history").addEventListener("click",()=>guarded($("refresh-history"),async()=>{await refreshHistory();await poll();}));
function artifactPath(path,file){return `/api/artifact/${path.split("/").map(encodeURIComponent).join("/")}/${file}`;}
async function viewRun(run){const data=await(await api(artifactPath(run.path,"results.json"))).json();selectedRun=run;window.renderReport($("report"),data);$("download-json").disabled=false;$("download-html").disabled=false;showTab("results");}
async function download(file){if(!selectedRun)return;const blob=await(await api(artifactPath(selectedRun.path,file))).blob();const url=URL.createObjectURL(blob),a=node("a");a.href=url;a.download=`${selectedRun.name}-${selectedRun.run_id}-${file}`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
$("download-json").addEventListener("click",()=>guarded($("download-json"),()=>download("results.json")));
$("download-html").addEventListener("click",()=>guarded($("download-html"),()=>download("report.html")));
async function loadCatalog(refresh=false){
  const revision=++catalogRevision,provider=activeProvider(),id=provider.id,connection=connectionPayload(provider);
  $("reload-picker").disabled=true;$("picker-status").textContent="正在读取接口模型…";
  try{
    const data=await(await api("/api/models",{method:"POST",body:JSON.stringify({connection,refresh})})).json();
    if(revision!==catalogRevision||id!==activeProviderId)return;
    catalog=data;providerCatalogs.set(id,{data});renderProviderList();renderPicker();
    $("picker-status").textContent=data.stale?`${data.error||"刷新失败"} 显示上次列表，可重试或手动添加模型。`:`${data.count} 个可选模型`;
  }catch(e){if(revision!==catalogRevision||id!==activeProviderId)return;providerCatalogs.set(id,{error:true});renderProviderList();$("picker-status").textContent=`${e.message}。可重试，或手动添加模型。`;}
  finally{if(revision===catalogRevision)syncButtons();}
}
connect().catch(e=>error(e.message));
