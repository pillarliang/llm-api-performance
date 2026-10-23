/* Shared offline and workspace report. Data is rendered through textContent. */
"use strict";
window.renderReport = function renderReport(root, data) {
  const el = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const fmt = value => value == null || !Number.isFinite(value) ? "—" : value.toLocaleString("zh-CN", {maximumFractionDigits:2});
  const sourceName = group => {
    if(group.provider_name)return group.provider_name;
    try{return new URL(group.base_url).host;}catch{return group.endpoint_id||"历史来源";}
  };
  const groups = (data.summary || []).map(group=>({...group,modality:group.modality||"text",provider_id:group.provider_id||group.base_url||group.endpoint_id||"legacy",provider_name:sourceName(group)}));
  const textTokenLabel = groups.some(g=>g.modality!=="text") ? "提示词文本 Token" : "输入 Token";
  const modelLabel = group => `${group.provider_name} · ${group.model||group.endpoint_id}`;
  const settings = data.environment || data.settings || {};
  const modelKey = group => JSON.stringify([group.provider_id, group.endpoint_id ?? null, group.model ?? null]);
  const models = new Map();
  const order = ["off", "minimal", "low", "medium", "high", "xhigh", "max", "adaptive"];
  [...groups].sort((a,b) => modelKey(a).localeCompare(modelKey(b))).forEach(group => {
    const key = modelKey(group);
    if (!models.has(key)) models.set(key, {group, enabled:true, profiles:new Map()});
    models.get(key).profiles.set(String(group.profile ?? "default"), true);
  });
  const hues = [215,155,275,25,335,185,65,245];
  [...models.values()].forEach((model,index) => {model.hue = hues[index] ?? (index*137.508)%360;});
  function color(node, model) {
    node.style.setProperty("--model-color", `hsl(${model.hue}, 58%, 32%)`);
    node.style.setProperty("--model-bg", `hsl(${model.hue}, 55%, 97%)`);
    node.style.setProperty("--model-hover", `hsl(${model.hue}, 55%, 93%)`);
  }
  root.replaceChildren();
  root.classList.add("performance-report");
  const header = el("header", undefined, "report-header");
  const heading = el("div");
  heading.append(el("div", "PERFORMANCE / V2", "report-eyebrow"), el("h1", "LLM Performance Bench"));
  const runLabel = [settings.name,settings.version].filter(Boolean).join(" / ");
  heading.append(el("div", runLabel || "LLM API 性能评测报告", "report-muted"));
  const badge = el("span", data.status === "complete" ? "全部测试项已成功" : "仍有待补跑项", "report-badge");
  header.append(heading,badge);
  root.append(header);
  if (data.data_source === "synthetic_fixture") root.append(el("div", "模拟示例 · 本报告使用测试流数据，不代表真实模型性能。", "report-synthetic"));
  const stats = el("div", undefined, "stats");
  const panel = el("section", undefined, "report-panel");
  panel.append(el("h2", "模型性能对比"));
  const filters = el("div", undefined, "report-filters");
  const selector = {};
  const pickers = [];
  function picker(title, initial) {
    const field = el("div", undefined, "report-filter");
    field.append(el("span",title,"report-filter-label"));
    const details = el("details",undefined,"report-picker");
    const summary = el("summary",initial);
    const menu = el("div",undefined,"report-menu");
    details.append(summary,menu); field.append(details); filters.append(field);
    details.addEventListener("toggle",()=>{if(details.open) pickers.forEach(p=>{if(p!==details)p.open=false;});});
    pickers.push(details);
    return {details,summary,menu};
  }
  const modelPicker = picker("比较模型", "全部模型");
  const modelActions = el("div",undefined,"report-menu-actions");
  const allModels = el("button","全选模型"), noModels = el("button","清空模型");
  modelActions.append(allModels,noModels);modelPicker.menu.append(modelActions);
  const modelOptions = el("div",undefined,"report-options");
  modelOptions.setAttribute("role","group");modelOptions.setAttribute("aria-label","选择比较模型");
  modelPicker.menu.append(modelOptions);
  const profilePicker = picker("推理强度", "各模型全部配置");
  profilePicker.menu.classList.add("report-profile-menu");
  profilePicker.menu.append(el("p","为每个模型独立选择，可多选。","report-menu-note"));
  const profileOptions = el("div",undefined,"report-options");
  profilePicker.menu.append(profileOptions);
  models.forEach(model => {
    const name = modelLabel(model.group);
    const identity = `${name}（${model.group.endpoint_id || "默认端点"}）`;
    const label = el("label",undefined,"report-option");
    const checkbox = el("input");checkbox.type="checkbox";checkbox.checked=true;
    checkbox.setAttribute("aria-label",`显示模型 ${identity}`);
    const swatch = el("span",undefined,"report-swatch");swatch.setAttribute("aria-hidden","true");
    label.append(checkbox,swatch,el("span",name));label.title=identity;color(label,model);
    checkbox.addEventListener("change",()=>{model.enabled=checkbox.checked;update();});
    modelOptions.append(label);model.check=checkbox;
    const field = el("fieldset",undefined,"report-profile-group");
    field.append(el("legend",name));field.title=identity;
    const choices=el("div",undefined,"report-profile-choices");model.inputs=[];model.field=field;
    [...model.profiles.keys()].sort((a,b)=>{
      const ai=order.indexOf(a),bi=order.indexOf(b);
      return (ai<0?99:ai)-(bi<0?99:bi)||a.localeCompare(b);
    }).forEach(profile=>{
      const choice=el("label",undefined,"report-profile");
      const input=el("input");input.type="checkbox";input.checked=true;
      input.setAttribute("aria-label",`${identity}推理配置 ${profile}`);
      input.addEventListener("change",()=>{model.profiles.set(profile,input.checked);update();});
      choice.append(input,el("span",profile));choices.append(choice);model.inputs.push([input,profile]);
    });
    field.append(choices);profileOptions.append(field);
  });
  allModels.addEventListener("click",()=>{models.forEach(m=>{m.enabled=true;});update();});
  noModels.addEventListener("click",()=>{models.forEach(m=>{m.enabled=false;});update();});
  for(const [key,title] of [["provider_id","来源"],["modality","输入类型"],["model","模型"],["task","任务"],["target_input_tokens","输入 Token"],["concurrency","并发"]]) {
    const label=el("label",title,"report-filter");const select=el("select");
    select.setAttribute("aria-label",`筛选${title}`);select.append(new Option("全部",""));
    [...new Set(groups.map(g=>String(g[key])))].sort((a,b)=>a.localeCompare(b,undefined,{numeric:true})).forEach(value=>select.append(new Option(key==="provider_id"?groups.find(g=>g.provider_id===value).provider_name:value==="null"?"未分档":value,value)));
    select.addEventListener("change",update);selector[key]=select;label.append(select);filters.append(label);
  }
  const statLabel=el("label","统计值","report-filter");const statistic=el("select");
  statistic.setAttribute("aria-label","统计分位数");
  [["p50","P50"],["mean","均值"],["tail","尾部 · P95 / P5"]].forEach(([value,label])=>statistic.append(new Option(label,value)));
  statistic.addEventListener("change",update);statLabel.append(statistic);filters.append(statLabel);
  const reset=el("button","重置筛选"), csv=el("button","导出 CSV");filters.append(reset,csv);
  const legend=el("div",undefined,"report-model-legend");legend.setAttribute("aria-label","模型颜色图例");
  const scroll=el("div",undefined,"table-scroll");scroll.tabIndex=0;
  scroll.setAttribute("role","region");scroll.setAttribute("aria-label","性能指标表，可横向滚动");
  const table=el("table"),thead=el("thead"),head=el("tr"),body=el("tbody");
  const metrics=[["ttft_ms","TTFT / ms"],["ttfa_seconds","TTFA / s"],["output_speed_tps","答案速度 / tok/s"],["e2e_seconds","E2E / s"],["max_answer_gap_seconds","最大间隔 / s"]];
  const columns=[["provider_name","来源"],["model","模型 / 推理强度"],["task","任务"],["benchmark_input_tokens",textTokenLabel],["concurrency","并发"],["success_rate","成功率"],...metrics];
  let sortKey="model",direction=1,visible=[];
  const sortHeaders=[];
  columns.forEach(([key,name])=>{
    const th=el("th"),button=el("button",name+" ↕");
    th.scope="col";
    button.addEventListener("click",()=>{direction=sortKey===key?-direction:key==="output_speed_tps"?-1:1;sortKey=key;update();});
    th.append(button);head.append(th);sortHeaders.push([th,button,key,name]);
  });
  thead.append(head);table.append(thead,body);scroll.append(table);
  const empty=el("div","没有匹配的测试组，请调整模型、推理强度或其他筛选条件。","empty");scroll.append(empty);
  const selection=el("p",undefined,"report-note report-selection");selection.setAttribute("role","status");
  panel.append(filters,legend,scroll,selection,el("p","延迟越低越好，输出速度越高越好。— 表示无有效样本；不同测试组不合并计算分位数。","report-note"));
  const diagnostics=el("section",undefined,"report-panel");diagnostics.append(el("h2","Token 用量与运行详情"));
  function detail(title){const d=el("details",undefined,"report-detail");d.append(el("summary",title));diagnostics.append(d);return d;}
  const usage=detail("查看 Token 用量与历史失败");
  const usageScroll=el("div",undefined,"table-scroll"),usageTable=el("table"),usageHead=el("thead"),usageRow=el("tr"),usageBody=el("tbody");
  ["来源","模型 / 推理强度","任务","输入 Token / 并发","答案 Token · 均值","服务端输入 · 均值","思考 Token · 均值","历史失败"].forEach(name=>usageRow.append(el("th",name)));
  usageHead.append(usageRow);usageTable.append(usageHead,usageBody);usageScroll.append(usageTable);usage.append(usageScroll);
  const raw=detail("查看所选组的完整汇总 JSON"),pre=el("pre");raw.append(pre);
  const environment=detail("运行环境与会话");
  environment.append(el("pre",JSON.stringify(data.environment||{name:settings.name,version:settings.version,session_id:settings.session_id,tokenizer:settings.tokenizer,environment:settings.environment},null,2)));
  const definitions=el("section",undefined,"report-panel");definitions.append(el("h2","指标说明"),
    el("p","TTFT：首个非空思考或答案片段。TTFA：首个答案片段。E2E：成功请求的最后生成内容到达时间，不包含末尾 usage 等待。"),
    el("p","答案速度 =（答案总 Token − 首片段 Token）/ 首末答案片段时间差。统一使用 o200k_base 计数；单片段答案无法计算速度。"),
    el("p","尾部统计使用延迟 P95、速度 P5。成功率按最终测试项计算；样本数、重试和失败尝试保留在汇总 JSON 中。","report-note"));
  const source=data.data_source==="synthetic_fixture"?"模拟示例":data.data_source==="live_api"?"真实 API 观测":"性能评测数据";
  const footer=el("footer",`${source} · ${data.started_at||"—"} → ${data.finished_at||"—"} · 筛选同步更新统计卡片与表格。`,"report-footer");
  root.append(stats,panel,diagnostics,definitions,footer);
  // Bind to this render root so reopening a run does not accumulate document listeners.
  root.onclick=event=>pickers.forEach(p=>{if(!p.contains(event.target))p.open=false;});
  root.onkeydown=event=>{if(event.key==="Escape")pickers.forEach(p=>{if(p.open){p.open=false;p.querySelector("summary").focus();}});};
  reset.addEventListener("click",()=>{
    models.forEach(m=>{m.enabled=true;m.profiles.forEach((_,key)=>m.profiles.set(key,true));});
    Object.values(selector).forEach(s=>{s.value="";});statistic.value="p50";sortKey="model";direction=1;
    pickers.forEach(p=>{p.open=false;});update();
  });
  function metric(group,key){
    const quantile=statistic.value==="tail"?(key==="output_speed_tps"?"p5":"p95"):statistic.value;
    const value=group.metrics?.[key]?.[quantile];return Number.isFinite(value)?value:null;
  }
  function value(group,key){
    if(key==="model")return group.model||group.endpoint_id||"";
    if(key==="success_rate")return group.planned_cases?group.successful_cases/group.planned_cases:null;
    if(metrics.some(([k])=>k===key))return metric(group,key);
    return group[key]??null;
  }
  function modelCell(group){
    const td=el("td"),name=el("span",group.model||group.endpoint_id,"report-model-name");
    td.title=`${group.provider_name} · ${group.base_url||group.endpoint_id||"默认"}`;td.append(name,el("span",group.profile??"default","cell-sub"));return td;
  }
  function update(){
    let enabled=0,profileCount=0,profileTotal=0;
    models.forEach(m=>{
      m.check.checked=m.enabled;m.field.disabled=!m.enabled;
      m.inputs.forEach(([input,profile])=>{input.checked=m.profiles.get(profile);input.disabled=!m.enabled;});
      if(m.enabled){enabled++;profileTotal+=m.profiles.size;profileCount+=[...m.profiles.values()].filter(Boolean).length;}
    });
    modelPicker.summary.textContent=enabled===models.size?`全部模型（${enabled}）`:enabled?`已选 ${enabled} 个模型`:"请选择模型";
    profilePicker.summary.textContent=enabled===0?"请先选择模型":profileCount===profileTotal?"各模型全部配置":`已选 ${profileCount} / ${profileTotal} 项`;
    visible=groups.filter(g=>{const m=models.get(modelKey(g));return m.enabled&&m.profiles.get(String(g.profile??"default"))&&Object.entries(selector).every(([key,s])=>!s.value||String(g[key])===s.value);});
    visible.sort((a,b)=>{const x=value(a,sortKey),y=value(b,sortKey);if(x==null)return y==null?0:1;if(y==null)return -1;return (typeof x==="number"?x-y:String(x).localeCompare(String(y)))*direction;});
    sortHeaders.forEach(([th,button,key,name])=>{
      const active=key===sortKey;th.setAttribute("aria-sort",active?(direction===1?"ascending":"descending"):"none");button.textContent=name+(active?(direction===1?" ↑":" ↓"):" ↕");
    });
    const sum=key=>visible.reduce((n,g)=>n+(g[key]||0),0);
    const planned=sum("planned_cases"),successful=sum("successful_cases");
    stats.replaceChildren();
    [["计划测试项",fmt(planned),`${visible.length} 个测试组`],["测试项成功率",planned?fmt(100*successful/planned)+"%":"—",`${successful} 项成功`],["待补跑项",fmt(planned-successful),"包含失败、取消与缺失项"],["统计请求次数",fmt(sum("requests")),"不含预热和主动取消"]].forEach(([label,n,note])=>{
      const card=el("div",undefined,"stat");card.append(el("div",label,"stat-label"),el("div",n,"stat-value"),el("div",note,"stat-note"));stats.append(card);
    });
    legend.replaceChildren();
    const visibleModels=new Set(visible.map(modelKey));
    models.forEach((m,key)=>{if(!visibleModels.has(key))return;const item=el("span",undefined,"report-legend-item");color(item,m);const dot=el("span",undefined,"report-swatch");dot.setAttribute("aria-hidden","true");item.append(dot,el("span",modelLabel(m.group)));item.title=`端点：${m.group.endpoint_id}`;legend.append(item);});
    body.replaceChildren();usageBody.replaceChildren();
    const maxSpeed=Math.max(1,...visible.map(g=>metric(g,"output_speed_tps")||0));
    visible.forEach(group=>{
      const tr=el("tr",undefined,"report-model-row");color(tr,models.get(modelKey(group)));
      tr.append(el("td",group.provider_name),modelCell(group),el("td",group.task),el("td",fmt(group.benchmark_input_tokens),"metric-cell"),el("td",fmt(group.concurrency),"metric-cell"));
      const rate=value(group,"success_rate");tr.append(el("td",rate==null?"—":fmt(rate*100)+"%",rate===1?"report-ok":"report-bad"));
      metrics.forEach(([key])=>{const n=metric(group,key),td=el("td",undefined,"metric-cell");
        if(key==="output_speed_tps"&&n!=null){const bar=el("span",undefined,"report-inline-bar");bar.style.width=`${48*Math.max(0,n)/maxSpeed}px`;bar.setAttribute("aria-hidden","true");td.append(bar);}
        td.append(el("span",fmt(n)));tr.append(td);
      });body.append(tr);
      const u=el("tr",undefined,"report-model-row");color(u,models.get(modelKey(group)));
      u.append(el("td",group.provider_name),modelCell(group),el("td",group.task),el("td",`${fmt(group.benchmark_input_tokens)} / ${group.concurrency}`,"metric-cell"));
      ["benchmark_answer_tokens","prompt_tokens","reasoning_tokens"].forEach(key=>u.append(el("td",fmt(group.metrics?.[key]?.mean),"metric-cell")));
      u.append(el("td",Object.entries(group.errors||{}).map(([k,n])=>`${k}: ${n}`).join(" · ")||"—"));usageBody.append(u);
    });
    empty.hidden=visible.length>0;
    const conditions=new Set(visible.map(g=>JSON.stringify([g.task,g.target_input_tokens,g.concurrency])));
    selection.textContent=`展示 ${visible.length} / ${groups.length} 组 · ${statistic.selectedOptions[0].textContent}${conditions.size>1?" · 当前包含不同测试条件，建议筛选相同任务、输入和并发再比较。":""}`;
    pre.textContent=JSON.stringify(visible,null,2);csv.disabled=!visible.length;
  }
  csv.addEventListener("click",()=>{
    const exportColumns=[["provider_name","Provider"],["provider_id","Provider ID"],["model","Model"],["profile","Profile"],["endpoint_id","Endpoint"],["base_url","Base URL"],...columns.slice(2)];
    const escape=v=>'"'+String(v??"").replace(/^[=+@-]/,"'$&").replaceAll('"','""')+'"';
    const rows=[exportColumns.map(([,title])=>title),...visible.map(g=>exportColumns.map(([key])=>value(g,key)))];
    const url=URL.createObjectURL(new Blob(["\ufeff"+rows.map(row=>row.map(escape).join(",")).join("\r\n")],{type:"text/csv;charset=utf-8"}));
    const a=el("a");a.href=url;a.download=`performance-${statistic.value}.csv`;root.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  update();
};
