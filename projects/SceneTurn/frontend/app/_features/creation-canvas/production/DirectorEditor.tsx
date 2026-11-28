'use client';

import { useEffect, useRef, useState } from 'react';
import { Camera, Clapperboard, Diamond, Move3d, Pause, Play, RotateCcw, Scale3d, Trash2, X } from 'lucide-react';
import { DirectorViewport } from './director-viewport';
import { importStageModel, initialScene, snapshotPose, stageObject, type DirectorScene, type StageObject, type XYZ } from './director-scene';
import type { CanvasNodeData } from '../upstream/canvas';
import './production.css';

type Props={projectId:string;value?:DirectorScene;images:CanvasNodeData[];onChange:(v:DirectorScene)=>void;onSave:(file:File)=>Promise<void>;onClose:()=>void};
export default function DirectorEditor({projectId,value,images,onChange,onSave,onClose}:Props){
  const [initial]=useState(initialScene);const data=value || initial;
  const [selected,setSelected]=useState(data.objects.find(o=>o.kind==='actor')?.id || data.objects[0]?.id || '');
  const [view,setView]=useState<'director'|'lens'>('director'),[tool,setTool]=useState<'translate'|'rotate'|'scale'>('translate');
  const [tab,setTab]=useState<'属性'|'姿势'>('属性'),[search,setSearch]=useState('');
  const [time,setTime]=useState(0),[playing,setPlaying]=useState(false),[scrub,setScrub]=useState(false),[fps,setFps]=useState(30);
  const [busy,setBusy]=useState(false),[error,setError]=useState('');
  const host=useRef<HTMLDivElement>(null),viewport=useRef<DirectorViewport|null>(null),picker=useRef<HTMLInputElement>(null);
  const latest=useRef({data,onChange});
  const initialized=useRef(false);
  useEffect(()=>{if(!value&&!initialized.current){initialized.current=true;onChange(initial);}},[initial,onChange,value]);
  useEffect(()=>{latest.current={data,onChange};},[data,onChange]);
  useEffect(()=>{if(!host.current)return;
    try{viewport.current=new DirectorViewport(host.current,projectId,(id,transform)=>{const {data:d,onChange:write}=latest.current;write({...d,objects:d.objects.map(o=>o.id===id?{...o,...transform}:o)});setScrub(false);},setError);}
    catch(e){setError(e instanceof Error?e.message:'无法启动 WebGL 视窗。');}
    return ()=>{viewport.current?.dispose();viewport.current=null;};
  },[projectId]);
  useEffect(()=>{viewport.current?.sync(data,selected,view==='director'?'':data.cameraId);},[data,selected,view]);
  useEffect(()=>{viewport.current?.setTool(tool);},[tool]);
  useEffect(()=>{viewport.current?.seek(scrub || playing ? time:null);},[time,scrub,playing]);
  useEffect(()=>{if(!playing)return;const start=performance.now();let frame=0;const step=()=>{const t=(performance.now()-start)/1000;setTime(Math.min(data.duration,t));if(t>=data.duration){setPlaying(false);setScrub(true);}else frame=requestAnimationFrame(step);};frame=requestAnimationFrame(step);return()=>cancelAnimationFrame(frame);},[playing,data.duration]);
  const object=data.objects.find(o=>o.id===selected);const cameras=data.objects.filter(o=>o.kind==='camera');
  const patch=(p:Partial<StageObject>)=>{setScrub(false);onChange({...data,objects:data.objects.map(o=>o.id===selected?{...o,...p}:o)});};
  const add=(kind:StageObject['kind'],label:string)=>{const o=stageObject(kind,label+(1+data.objects.filter(x=>x.kind===kind).length));onChange({...data,objects:[...data.objects,o]});setSelected(o.id);};
  const mutate=async(task:()=>Promise<void>)=>{setBusy(true);setError('');try{await task();}catch(e){setError(e instanceof Error?e.message:'操作失败');}finally{setBusy(false);}};
  const makeKey=()=>{if(!object)return;const t=Number(time.toFixed(2));patch({keys:[...object.keys.filter(k=>k.time!==t),{time:t,pose:snapshotPose(object)}].sort((a,b)=>a.time-b.time)});};
  const importFile=(file?:File)=>{if(!file)return;void mutate(async()=>{const id=await importStageModel(projectId,file);const next=stageObject('model',file.name);next.modelId=id;const d=latest.current.data;onChange({...d,objects:[...d.objects,next]});setSelected(next.id);});};
  const output=(mode:'photo'|'all'|'video')=>void mutate(async()=>{const stage=viewport.current;if(!stage)throw new Error('3D 视窗不可用。');const list=mode==='all'?cameras:cameras.filter(o=>o.id===data.cameraId);for(const c of list){const file=mode==='video'?await stage.movie(c.id,data.duration,fps,c.name):await stage.photo(c.id,time,c.name);await onSave(file);} });
  return <div className="prod-overlay" role="dialog" aria-modal="true" aria-label="3D导演台" data-canvas-no-zoom><section className="prod-editor prod-director">
    <header><strong><Clapperboard size={18}/>3D导演台</strong><div className="prod-segments">{(['director','lens'] as const).map(v=><button key={v} aria-pressed={view===v} onClick={()=>setView(v)}>{v==='director'?'导演视角':'机位视角'}</button>)}</div><button aria-label="关闭导演台" onClick={onClose} disabled={busy}><X size={18}/></button></header>
    <main><aside><strong>场景</strong><input aria-label="搜索场景对象" placeholder="搜索场景对象" value={search} onChange={e=>setSearch(e.target.value)}/>
      {data.objects.filter(o=>o.name.includes(search)).map(o=><button key={o.id} aria-pressed={selected===o.id} onClick={()=>setSelected(o.id)}>{o.kind==='camera'?<Camera size={15}/>:<Move3d size={15}/>} {o.name}</button>)}
      <div className="prod-tools">{([['actor','角色'],['prop','道具'],['camera','机位'],['group','分组']] as const).map(([k,t])=><button key={k} onClick={()=>add(k,t)}>＋{t}</button>)}</div><button onClick={()=>picker.current?.click()} disabled={busy}>导入模型</button><input hidden ref={picker} type="file" accept=".glb,.gltf,.obj,.fbx,.stl,.dae,.ply" onChange={e=>{importFile(e.target.files?.[0]);e.target.value='';}}/>
    </aside><div className="prod-stage"><div className="prod-viewport" ref={host}/><div className="prod-gizmos">{([{mode:'translate',title:'平移',Icon:Move3d},{mode:'rotate',title:'旋转',Icon:RotateCcw},{mode:'scale',title:'缩放',Icon:Scale3d}] as const).map(({mode,title,Icon})=><button aria-pressed={tool===mode} aria-label={title} key={mode} onClick={()=>setTool(mode)}><Icon size={16}/></button>)}<button onClick={()=>viewport.current?.reset()}>重置视角</button></div></div>
    <aside><strong>{object?.name || '场景'}</strong><div className="prod-segments">{(['属性','姿势'] as const).map(t=><button aria-pressed={tab===t} key={t} onClick={()=>setTab(t)}>{t}</button>)}</div>
    {object && (tab==='姿势'? object.kind==='actor' && ['站立','迈步','抬手'].map(p=><button aria-pressed={object.pose===p} key={p} onClick={()=>patch({pose:p})}>{p}</button>):<>
      <label>名称<input value={object.name} onChange={e=>patch({name:e.target.value})}/></label>
      {([['position','位置'],['rotation','旋转'],['scale','缩放']] as const).map(([key,title])=><fieldset key={key}><legend>{title}</legend>{object[key].map((v,i)=><label key={i}>{'xyz'[i]}<input aria-label={title+'xyz'[i]} type="number" step=".1" value={Number(v.toFixed(3))} onChange={e=>{const newVec=[...object[key]] as XYZ;newVec[i]=Number(e.target.value);patch({[key]:newVec});}}/></label>)}</fieldset>)}
      {object.kind==='camera' && <label>视野<input type="number" min="10" max="150" value={object.fov} onChange={e=>patch({fov:Math.min(150,Math.max(10,Number(e.target.value)))})}/></label>}
      {['actor','prop'].includes(object.kind) && <label>颜色<input type="color" value={object.color} onChange={e=>patch({color:e.target.value})}/></label>}
      {object.kind!=='group' && <label>分组<select value={object.parentId || ''} onChange={e=>patch({parentId:e.target.value})}><option value="">场景</option>{data.objects.filter(o=>o.kind==='group').map(o=><option key={o.id} value={o.id}>{o.name}</option>)}</select></label>}
      <button disabled={object.kind==='camera' && cameras.length<2} onClick={()=>{const remaining=data.objects.filter(o=>o.id!==selected).map(o=>o.parentId===selected?{...o,parentId:undefined}:o);onChange({...data,objects:remaining,cameraId:data.cameraId===selected?remaining.find(o=>o.kind==='camera')!.id:data.cameraId});setSelected(remaining[0]?.id || '');}}><Trash2 size={14}/>删除对象</button></>)}
      <label>环境<input type="color" value={data.background} onChange={e=>onChange({...data,background:e.target.value})}/></label><label>全景背景<select value={data.panoramaId || ''} onChange={e=>onChange({...data,panoramaId:e.target.value})}><option value="">纯色</option>{images.filter(n=>n.metadata?.storageKey).map(n=><option key={n.id} value={n.metadata?.storageKey}>{n.title}</option>)}</select></label>
    </aside></main>
    <section className="prod-keyframes"><button aria-label={playing?'暂停':'播放'} onClick={()=>setPlaying(!playing)}>{playing?<Pause size={16}/>:<Play size={16}/>}</button><output>{time.toFixed(2)} s</output><input aria-label="时间轴" type="range" min="0" max={data.duration} step=".01" value={time} onChange={e=>{setPlaying(false);setScrub(true);setTime(Number(e.target.value));}}/><label>时长<input type="number" min="1" max="60" value={data.duration} onChange={e=>{const duration=Math.min(60,Math.max(1,Number(e.target.value)));onChange({...data,duration});setTime(Math.min(time,duration));}}/></label><button disabled={!object} onClick={makeKey}><Diamond size={15}/>记录关键帧</button></section>
    {!!object?.keys.length && <div className="prod-keys">{object.keys.map(k=><span key={k.time}><button onClick={()=>{setTime(k.time);setScrub(true);}}>{k.time} s</button><button aria-label={'删除关键帧 '+k.time} onClick={()=>patch({keys:object.keys.filter(x=>x.time!==k.time)})}>×</button></span>)}</div>}
    <footer><span role="status">{error || (busy?'正在处理素材…':'')}</span><select aria-label="输出机位" value={data.cameraId} onChange={e=>onChange({...data,cameraId:e.target.value})}>{cameras.map(c=><option key={c.id} value={c.id}>{c.name}</option>)}</select><button disabled={busy} onClick={()=>output('photo')}>机位截图</button><button disabled={busy} onClick={()=>output('all')}>全部机位截图</button><select aria-label="动画帧率" value={fps} onChange={e=>setFps(Number(e.target.value))}><option value="30">30 FPS</option><option value="60">60 FPS</option></select><button disabled={busy} onClick={()=>output('video')}>导出无声 MP4</button></footer>
  </section></div>;
}
