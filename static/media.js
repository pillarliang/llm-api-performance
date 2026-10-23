"use strict";
(() => {
  const modality=document.getElementById('input-modality');
  let assets=[],defaultInfo=null,readyJob=null,count=0,working=false,revision=0,poll=null;
  const labels={image_resolution:'图片分辨率',image_count:'图片数量',video_duration:'视频时长',video_resolution:'视频分辨率',video_frame_count:'视频帧数',video_frame_resolution:'视频帧分辨率'};
  function status(text){document.getElementById('media-status').textContent=text;}
  function defaults(){return [...document.querySelectorAll('#media-default-groups input:checked')].map(e=>e.value);}
  window.visualMedia={updateOptions,active:()=>modality.value!=='text',ready:()=>!working&&(modality.value==='default'?defaults().length>0&&!!defaultInfo?.case_count:!!readyJob),caseCount:()=>modality.value==='default'?(defaults().length?defaults().reduce((sum,key)=>sum+(defaultInfo?.group_counts?.[key]||0),0):0):count,selection:()=>modality.value==='text'?null:modality.value==='default'?{source:'default',groups:defaults()}:{source:'upload',job_id:readyJob}};
  function updateOptions(){for(const el of document.querySelectorAll('#media-assets input,#media-files,#upload-media,#prepare-media,#refresh-media,#input-modality'))el.disabled=working;const eligible=[...document.querySelectorAll('#media-assets input[value="image:1024"]:checked')].length;for(const c of document.querySelectorAll('#media-counts input')){c.disabled=working||eligible<+c.value;if(eligible<+c.value)c.checked=false;}}
  function changedMedia(){readyJob=null;count=0;revision++;syncButtons();updateCounts();}
  function check(label,value,container){const line=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.value=value;line.append(input,document.createTextNode(label));container.append(line);return input;}
  function render(){
    const root=document.getElementById('media-assets');root.replaceChildren();
    for(const asset of assets){const card=document.createElement('fieldset');card.dataset.assetId=asset.id;const legend=document.createElement('legend');legend.textContent=asset.name;card.append(legend);const options=document.createElement('div');options.className='token-options';card.append(options);
      if(asset.kind==='image'){for(const edge of asset.info.edges)check(`长边 ${edge}`,`image:${edge}`,options);}
      else {
        const info=document.createElement('p');info.textContent=`${asset.info.width} × ${asset.info.height} · ${asset.info.duration_seconds.toFixed(1)}秒`;card.append(info);
        for(const duration of asset.info.durations)for(const height of asset.info.heights)check(`${duration}秒 / ${height}p`,`video:${duration}:${height}`,options);
        for(const frames of asset.info.frame_counts)for(const height of asset.info.heights)check(`前60秒 / ${frames}帧 / ${height}p`,`frames:${frames}:${height}`,options);
      }
      card.addEventListener('change',changedMedia);root.append(card);
    }
    const groupRoot=document.getElementById('media-default-groups');groupRoot.replaceChildren();
    const missing=new Set((defaultInfo?.skipped_groups||[]).map(g=>g.group));
    if(defaultInfo?.case_count)for(const [key,label]of Object.entries(labels)){if(!defaultInfo.group_counts?.[key])continue;const c=check(label,key,groupRoot);c.checked=!missing.has(key);}
    groupRoot.onchange=()=>{syncButtons();updateCounts();};
  }
  async function refresh(){changedMedia();const result=await(await api('/api/media')).json();assets=result.assets;defaultInfo=result.default;render();status(modality.value==='default'?(result.default_error||(defaultInfo?.case_count?`默认数据集已就绪 · ${defaultInfo.case_count} 个样本`:'默认数据集尚未准备')):'个人素材本地暂存，评测结束后自动清理；请先选择档位并生成');syncButtons();updateCounts();}
  modality.addEventListener('change',async()=>{document.getElementById('media-panel').hidden=modality.value==='text';document.querySelector('.token-field').hidden=modality.value!=='text';document.getElementById('media-upload-area').hidden=modality.value!=='upload';document.getElementById('media-default-area').hidden=modality.value!=='default';syncButtons();updateCounts();if(modality.value!=='text')try{await refresh();}catch(e){status(e.message);}});
  setInterval(async()=>{
    const id=readyJob;
    if(!id||working)return;
    try{const state=await(await api(`/api/media/jobs/${id}`)).json();
      if(readyJob===id&&state.status==='cleaned'){changedMedia();await refresh();status(state.message);}
    }catch{}
  },5000);
  document.getElementById('media-counts').addEventListener('change',changedMedia);
  document.getElementById('refresh-media').onclick=()=>refresh().catch(e=>status(e.message));
  document.getElementById('upload-media').onclick=async()=>{
    if(working)return;working=true;syncButtons();
    try{const files=[...document.getElementById('media-files').files];if(!files.length)throw new Error('请选择文件');for(const file of files){status(`正在上传并检查：${file.name}`);await api('/api/media/upload',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-File-Name':encodeURIComponent(file.name)},body:file});}changedMedia();await refresh();status('上传完成，请选择档位后生成素材');}
    catch(e){await refresh().catch(()=>{});status(e.message);}finally{working=false;syncButtons();}
  };
  document.getElementById('prepare-media').onclick=async()=>{
    if(working)return;
    const items=[];
    for(const card of document.querySelectorAll('#media-assets fieldset')){const values=[...card.querySelectorAll('input:checked')].map(e=>e.value.split(':'));if(!values.length)continue;const asset=assets.find(a=>a.id===card.dataset.assetId);items.push(asset.kind==='image'?{id:asset.id,edges:values.map(v=>+v[1])}:{id:asset.id,videos:values.filter(v=>v[0]==='video').map(v=>[+v[1],+v[2]]),frames:values.filter(v=>v[0]==='frames').map(v=>[+v[1],+v[2]])});}
    const image_counts=[...document.querySelectorAll('#media-counts input:checked')].map(e=>+e.value);
    changedMedia();const myRevision=revision;working=true;syncButtons();
    try{const job=await(await api('/api/media/prepare',{method:'POST',body:JSON.stringify({assets:items,image_counts})})).json();
      const track=async()=>{try{const next=await(await api(`/api/media/jobs/${job.id}`)).json();status(`${next.message} · ${next.completed}/${next.total}`);if(['queued','processing'].includes(next.status)){poll=setTimeout(track,2000);return;}working=false;if(next.status==='ready'&&myRevision===revision){readyJob=next.id;count=next.case_count;}syncButtons();updateCounts();}catch(e){working=false;status(e.message);syncButtons();}};
      track();
    }catch(e){working=false;status(e.message);syncButtons();}
  };
})();
