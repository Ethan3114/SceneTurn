'use client';

import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '@/app/_utils/cloudApi';
import { AudioLines, FileAudio, FolderInput, Music2, Settings2, SlidersHorizontal, Sparkles, Upload, Waves, Zap } from 'lucide-react';
import type { CanvasNodeData, CanvasNodeMetadata } from '../upstream/canvas';
import type { AudioDraft, AudioMode, AudioSettings, LocalAudioMode } from './media-edit';
import './production.css';

const modes = [{ title: '配音旁白', Icon: FileAudio }, { title: '音乐配乐', Icon: Music2 }, { title: '音效', Icon: Waves }, { title: '音色生成', Icon: AudioLines }] as const;
const examples: Record<AudioMode,string[]> = { 配音旁白:['温和叙述','纪录片旁白','角色独白'], 音乐配乐:['清晨的木吉他','紧张的弦乐','宁静的钢琴'], 音效:['脚步声','开门声','雨声','风声','键盘敲击','玻璃碎裂'], 音色生成:['沉稳清晰的青年男声，以均匀节奏讲述城市故事。','温暖而略带沙哑的女声，语气亲切，有自然的停顿。'] };
const hints: Record<AudioMode,string> = { 配音旁白:'描述你想要的音频效果，可用 @ 引用音频', 音乐配乐:'描述您想要的音乐', 音效:'描述你想要的声音，例如「木门缓慢推开的吱呀声」', 音色生成:'用 20～1000 字描述你想要的音色，每次生成一段试听音频' };

type Props = { projectId:string; node:CanvasNodeData; references:CanvasNodeData[]; onChange:(patch:Partial<CanvasNodeMetadata>)=>void; onUpload:()=>void; onReference:()=>void; onEdit:()=>void; onSave:(file:File)=>Promise<void>; onCheckpoint:()=>Promise<void> };
export function AudioComposer({projectId,node,references,onChange,onUpload,onReference,onEdit,onSave,onCheckpoint}:Props) {
  const [popover,setPopover] = useState<'presets'|'settings'|'models'|null>(null);
  const [models,setModels]=useState<Partial<Record<LocalAudioMode,string>>>({});
  const [consent,setConsent]=useState(false);
  const picker=useRef<HTMLInputElement>(null), mounted=useRef(true);
  useEffect(()=>{mounted.current=true;api<{models:Partial<Record<LocalAudioMode,string>>}>('/media-tools/audio-workflows').then(r=>{if(mounted.current)setModels(r.models);}).catch(e=>{if(mounted.current)setError(e.message);});return()=>{mounted.current=false;};},[]);
  const [voices,setVoices]=useState<{name:string;locale:string}[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  useEffect(()=>{let active=true;api<{voices:{name:string;locale:string}[]}>('/media-tools/narrators').then(v=>{if(active)setVoices(v.voices);}).catch(e=>{if(active)setError(e.message);});return()=>{active=false;};},[]);
  const state:AudioSettings = node.metadata?.audioSettings || {};
  const mode = state.mode || (node.metadata?.audioMode === 'music' ? '音乐配乐':'配音旁白');
  const draft = state.drafts?.[mode] || {};
  const prompt = draft.prompt ?? (!state.drafts && (node.metadata?.composerContent || node.metadata?.prompt)) ?? '';
  const text = typeof prompt === 'string' ? prompt : '';
  const set = (value:Partial<AudioDraft>) => onChange({audioSettings:{...state,mode,drafts:{...state.drafts,[mode]:{...draft,prompt:text,...value}}}});
  const switchMode = (next:AudioMode) => { onChange({audioSettings:{...state,mode:next,drafts:{...state.drafts,[mode]:{...draft,prompt:text}}}});setPopover(null); };
  const select = (label:string,key:'voice'|'style'|'language'|'singer',values:string[],disabled=false) => <label className="prod-choice">{label}<select aria-label={label} disabled={disabled} value={draft[key] || values[0]} onChange={e=>set({[key]:e.target.value})}>{values.map(v=><option key={v}>{v}</option>)}</select></label>;
  const locales=voices.filter(v=>v.locale.startsWith(draft.language==='英文'?'en':'zh'));
  const voice=locales.find(v=>v.name===draft.voice) || locales.find(v=>v.name.startsWith(draft.language==='英文'?'Samantha':'Tingting')) || locales[0];
  const localMode:LocalAudioMode=mode==='音乐配乐'?'music':mode==='音效'?'effects':'voice';
  const model = mode === '配音旁白' && voice ? '系统语音' : models[localMode] || (mode === '音乐配乐' ? 'Mureka V8' : 'Seed Audio 1.0');
  async function configure(file:File){
    setError('');
    try{
      if(file.size>96_000)throw new Error('工作流超过 96 KB。');
      const workflow=JSON.parse(await file.text());
      const name=file.name.replace(/\.json$/i,'');
      await api('/media-tools/audio-workflows/'+localMode,{method:'PUT',body:JSON.stringify({name,workflow})});
      setModels(m=>({...m,[localMode]:name}));setPopover(null);
    }catch(e){setError(e instanceof Error?e.message:'无法保存模型工作流');}
  }
  async function generateLocal(){
    setBusy(true);setError('');
    const endpoint=`/projects/${projectId}/media-tools/audio-jobs`;
    let job=state.job;
    try{
      if(!job){
        const prompt=[text,mode==='音乐配乐' && draft.style && draft.style!=='自动' ? draft.style:'',mode==='音乐配乐' && draft.instrument!==false?'纯乐器':'',...references.filter(n=>n.type==='text').map(n=>n.metadata?.content)].filter(Boolean).join('\n');
        const audio=references.filter(n=>n.type==='audio' && n.metadata?.storageKey);
        if(audio.length>1)throw new Error('请选择一项参考音频。');
        job={id:crypto.randomUUID(),mode:localMode,request:{prompt,lyrics:draft.instrument===false?(draft.lyrics || ''):'',referenceAssetId:audio[0]?.metadata?.storageKey,voiceConsent:consent}};
        onChange({audioSettings:{...state,mode,job}});await onCheckpoint();
      }
      if(job.request) {
        try {
          await api(endpoint,{method:'POST',body:JSON.stringify({operationId:job.id,mode:job.mode,...job.request})});
        } catch (problem) {
          if(problem instanceof ApiError && [404,409,413,422].includes(problem.status)) {
            onChange({audioSettings:{...state,job:undefined}});await onCheckpoint();
          }
          throw problem;
        }
      }
      for(let i=0;i<120 && mounted.current;i++){
        const current=await api<{status:string}>(endpoint+'/'+job.id);
        if(current.status==='failed'){onChange({audioSettings:{...state,job:undefined}});await onCheckpoint();throw new Error('本机模型执行失败，请查看 ComfyUI 的执行记录。');}
        if(current.status==='ready'){
          const response=await fetch('/api/v1'+endpoint+'/'+job.id+'/audio',{credentials:'same-origin'});
          if(!response.ok)throw new Error('无法读取已完成的音频，请重新核对任务。');
          await onSave(new File([await response.blob()],({music:'音乐配乐',effects:'音效',voice:'音色试听'}[job.mode])+'.wav',{type:'audio/wav'}));
          return;
        }
        await new Promise(resolve=>setTimeout(resolve,2500));
      }
      if(mounted.current)setError('本机模型仍在执行，可继续核对任务。');
    }catch(e){if(mounted.current)setError(e instanceof Error?e.message:'本机音频生成失败');}
    finally{if(mounted.current)setBusy(false);}
  }
  async function narrate(){
    setBusy(true);setError('');
    try {
      const referenceText=references.filter(n=>n.type==='text').map(n=>n.metadata?.content).filter(Boolean).join('\n');
      const response=await fetch(`/api/v1/projects/${projectId}/media-tools/narrate`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:[text,referenceText].filter(Boolean).join('\n'),voice:voice.name,expression:draft.style || '自然',speed:draft.speed??1,pitch:draft.pitch??0,volume:draft.volume??1})});
      if(!response.ok){const reason=await response.json();throw new Error(reason.error.message);}
      await onSave(new File([await response.blob()],'配音旁白.wav',{type:'audio/wav'}));
    }catch(e){setError(e instanceof Error?e.message:'配音失败');}finally{setBusy(false);}
  }
  return <section className="prod-audio prod-panel" aria-label="音频生成" data-canvas-no-zoom onMouseDown={e=>e.stopPropagation()} onWheel={e=>e.stopPropagation()}>
    <nav aria-label="音频工具">{modes.map(({title,Icon})=><button key={title} aria-pressed={mode===title} onClick={()=>switchMode(title)}><Icon size={17}/>{title}</button>)}</nav>
    <div className="prod-audio-main">
      {mode==='配音旁白' && <div className="prod-options"><label className="prod-choice">音色<select aria-label="音色" value={voice?.name || ''} onChange={e=>set({voice:e.target.value})}>{locales.map(v=><option key={v.name}>{v.name}</option>)}</select></label>{select('表现力','style',['自然','平静','活泼','严肃'])}{select('语言','language',['中文','英文'])}</div>}
      {mode==='音乐配乐' && <div className="prod-options">{select('音乐风格','style',['自动','流行','电子','古典','摇滚','爵士'])}<label className="prod-choice">输入形态<select aria-label="输入形态" value={draft.instrument===false?'歌词':'纯乐器'} onChange={e=>set({instrument:e.target.value==='纯乐器'})}><option>纯乐器</option><option>歌词</option></select></label>{select('音色','singer',draft.instrument===false?['自动','男声','女声']:['纯乐器不可使用音色'],draft.instrument!==false)}</div>}
      <div className="prod-prompt">
        {mode!=='音色生成' && <div className="prod-tools"><button onClick={onUpload}><Upload size={14}/>上传</button><button onClick={onReference}><FolderInput size={14}/>画布参考</button><button aria-expanded={popover==='presets'} onClick={()=>setPopover(popover==='presets'?null:'presets')}><Sparkles size={14}/>预设提示词</button></div>}
        {references.length>0 && <div className="prod-reference">{references.map(n=><span key={n.id}>{n.title}</span>)}</div>}
        {mode==='音色生成' && references.some(n=>n.type==='audio') && <label><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)}/>我已获得参考声音所有者的授权</label>}
        <textarea aria-label={mode+'描述'} placeholder={hints[mode]} maxLength={mode==='音色生成'?1000:4000} value={text} onChange={e=>set({prompt:e.target.value})}/>
        {mode==='音乐配乐' && draft.instrument===false && <textarea aria-label="歌词" placeholder="输入歌词" value={draft.lyrics || ''} onChange={e=>set({lyrics:e.target.value})}/>}
        {(mode==='音效' || popover==='presets') && <div className="prod-tools">{examples[mode].map(x=><button key={x} onClick={()=>{set({prompt:x});setPopover(null);}}>{x}</button>)}</div>}
        <div className="prod-options">{mode==='配音旁白'?<span>{model}</span>:<button aria-label="音频模型" aria-expanded={popover==='models'} onClick={()=>setPopover(popover==='models'?null:'models')}>{model} ﹀</button>}{mode!=='音乐配乐' && <span>{mode==='音色生成'?'文本生成音色':'中文 · 24k · wav'}</span>}
          {(mode==='音效' || mode==='配音旁白') && <button aria-label="高级设置" aria-expanded={popover==='settings'} onClick={()=>setPopover(popover==='settings'?null:'settings')}><SlidersHorizontal size={16}/></button>}
          <button disabled={busy || (mode==='配音旁白'?!voice || !(text.trim() || references.some(n=>n.type==='text'&&n.metadata?.content)):!state.job && (!models[localMode] || !text.trim() || mode==='音色生成' && text.trim().length<20))} title={mode==='配音旁白'?'生成配音旁白':state.job?'继续核对任务':models[localMode]?'生成音频':'请先选择并配置音频模型'} aria-label="生成音频" onClick={()=>void (mode==='配音旁白'?narrate():generateLocal())}><Zap size={15}/></button></div>
        {popover==='models' && <div className="prod-audio-models"><strong>本机 ComfyUI</strong><p>连接 127.0.0.1:8188。选择 API 格式的工作流，将文字输入设为 {'{{prompt}}'}，歌词输入可用 {'{{lyrics}}'}，保留一个 SaveAudio 输出。</p><button onClick={()=>picker.current?.click()}>选择模型工作流</button><input type="file" hidden accept=".json" ref={picker} onChange={e=>{const f=e.target.files?.[0];if(f)void configure(f);e.target.value='';}}/></div>}
      </div>
      {popover==='settings' && <div className="prod-options">{([['语速','speed',0.5,2,1],['声调','pitch',-12,12,0],['音量','volume',0,2,1]] as const).map(([label,key,min,max,defaultValue])=><label key={key}>{label}<input aria-label={label} type="number" min={min} max={max} step={key==='pitch'?1:.1} value={draft[key]??defaultValue} onChange={e=>set({[key]:Number(e.target.value)})}/></label>)}</div>}
      {node.metadata?.storageKey && <button onClick={onEdit}><Settings2 size={14}/>编辑音频</button>}
      {(error||busy||state.job) && <span role="status">{error || (busy?'正在生成音频…':'已有音频任务，可继续核对。')}</span>}
    </div></section>;
}
