import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ChevronDown, Download, Grid2x2, MoreHorizontal, X } from 'lucide-react';
import type { CanvasNodeData } from './upstream/canvas';
import { clipFor, renderMediaEdit, type CropRegion } from './production/media-edit';
import { assetContentUrl } from '@/app/_utils/cloudApi';

type Region = CropRegion;
const imagePrompts: Record<string,string> = {
  '高清放大':'提升画面分辨率、纹理和真实细节。沿用原图的构图、光色及人物身份。',
  '扩图':'向外扩展原图画面，延续画面边缘的透视、材质、光线与颜色，保留主体。',
  '局部重绘':'重绘指定区域，保留区域外画面、人物身份与光色。',
  '擦除':'移除指定区域内的主体，以符合上下文的背景填补，保留区域外画面。',
  '重新打光':'根据新的布光要求调整明暗、色温与阴影，保持人物身份、场景和构图。',
  '变化机位':'改变观察机位，保留原图中的人物、布景和空间关系。',
};
const videoPrompts: Record<string,string> = {
  '片段重拍':'重拍所选时间段，保留人物身份、相邻动作和视线方向。',
  '画面编辑':'根据修改要求编辑原视频，保持主体和镜头的时间连续性。',
  '视频高清':'提升视频纹理清晰度，保持帧间一致性、内容与运动方向。',
  '去字幕':'移除指定区域内的字幕，利用相邻画面补全背景，保留其余画面。',
};
const regionTools = ['裁切', '局部重绘', '擦除', '去字幕'];
const timedTools = ['截取片段','片段重拍'];
const frameTools = ['提取首帧','提取当前帧','提取尾帧'];

function regionOrder(a: [number,number], b: [number,number]): Region {
  return { x:Math.min(a[0],b[0]), y:Math.min(a[1],b[1]), right:Math.max(a[0],b[0]), bottom:Math.max(a[1],b[1]) };
}

async function rasterFile(image: HTMLImageElement | HTMLVideoElement, region: Region, filename: string) {
  const width = image instanceof HTMLVideoElement ? image.videoWidth : image.naturalWidth;
  const height = image instanceof HTMLVideoElement ? image.videoHeight : image.naturalHeight;
  if (!width || !height) throw new Error('正在读取素材尺寸。');
  const x = Math.floor(width * region.x), y = Math.floor(height * region.y);
  const result = document.createElement('canvas');
  result.width = Math.max(1,Math.ceil(width * region.right) - x);
  result.height = Math.max(1,Math.ceil(height * region.bottom) - y);
  const ctx = result.getContext('2d', { alpha: !(image instanceof HTMLVideoElement) });
  if (!ctx) throw new Error('浏览器无法读取画面。');
  ctx.drawImage(image,x,y,result.width,result.height,0,0,result.width,result.height);
  const blob = await new Promise<Blob>((resolve,reject) => result.toBlob((value) => value ? resolve(value) : reject(new Error('画面导出失败。')), 'image/png'));
  return new File([blob], filename + '.png', { type:'image/png' });
}

type Props = {
  node: CanvasNodeData;
  projectId: string;
  onSave: (file: File) => Promise<void>;
  onDraft: (name: string, prompt: string) => void;
  onSplit: () => void;
};

export function EditResult({ action, node, projectId, onSave, onDraft, close }: Omit<Props,'onSplit'> & { action:string; close:()=>void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const imageRef = useRef<HTMLImageElement>(null);
  const origin = useRef<[number,number] | null>(null);
  const [region, setRegion] = useState<Region | null>(null);
  const [duration,setDuration] = useState(0);
  const [start,setStart] = useState(0);
  const [end,setEnd] = useState(0);
  const [ready,setReady] = useState(false);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState('');
  const [instructions,setInstructions] = useState('');
  const video = node.type === 'video';
  const src = assetContentUrl(node.metadata!.storageKey!);
  const isDraft = Boolean((video ? videoPrompts : imagePrompts)[action]);
  const hasRegion = regionTools.includes(action);
  const hasRange = timedTools.includes(action);
  const emptyRegion = hasRegion && (!region || region.right <= region.x || region.bottom <= region.y);
  const invalidClip = action === '截取片段' && (duration > 600 || Math.min(videoRef.current?.videoWidth || 0, videoRef.current?.videoHeight || 0) > 1080);
  useEffect(() => { const element=ref.current!; element.showModal(); return () => element.close(); }, []);
  const point = (event:React.PointerEvent<HTMLDivElement>):[number,number] => {
    const rect=event.currentTarget.getBoundingClientRect();
    return [Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)),Math.max(0,Math.min(1,(event.clientY-rect.top)/rect.height))];
  };
  async function submit() {
    if (busy || !ready || emptyRegion || invalidClip) return;
    setBusy(true); setError('');
    try {
      if (isDraft) {
        const target = region && ['局部重绘','擦除','去字幕'].includes(action) ? '矩形选区（画面左上角为原点）：左 ' + Math.round(region.x*100) + '%，上 ' + Math.round(region.y*100) + '%，右 ' + Math.round(region.right*100) + '%，下 ' + Math.round(region.bottom*100) + '%。' : '';
        const time = hasRange ? '处理时间段 ' + start + ' 秒至 ' + end + ' 秒。' : '';
        onDraft(action,[(video ? videoPrompts : imagePrompts)[action],target,time,instructions.trim()].filter(Boolean).join('\n'));
      } else if ((!video && action === '裁切') || frameTools.includes(action)) {
        await onSave(await rasterFile(video ? videoRef.current! : imageRef.current!, region ?? {x:0,y:0,right:1,bottom:1}, node.title + ' · ' + action));
      } else {
        const audio = action === '提取音轨';
        const clip = { ...clipFor(node, {}), start:hasRange ? start : 0, end:hasRange ? end : duration, gain:action === '静音视频' ? 0 : 1 };
        const result = await renderMediaEdit(projectId, { clips:{ [node.id]:clip }, keepSourceFormat: !audio, crop:action==='裁切' ? region ?? undefined : undefined }, [{ ...node,type:audio ? 'audio' : 'video' }],audio);
        await onSave(new File([result],node.title + ' · ' + action + (audio ? '.wav' : '.mp4'),{ type:result.type }));
      }
      close();
    } catch (problem) { setError(problem instanceof Error ? problem.message : '保存素材失败。'); }
    finally { setBusy(false); }
  }
  return createPortal(<dialog className="fmv-result-dialog" ref={ref} aria-label={action} onCancel={event=>{event.preventDefault();if(!busy)close();}} onClick={e=>e.stopPropagation()} onKeyDown={e=>e.stopPropagation()}>
    <header>{action}<button disabled={busy} onClick={close} aria-label="关闭"><X size={18}/></button></header>
    <div className="fmv-result-preview"><div className="fmv-result-visual" data-region={hasRegion} onPointerDown={event=>{if(!hasRegion)return;origin.current=point(event);event.currentTarget.setPointerCapture(event.pointerId);setRegion(null);}} onPointerMove={event=>{if(origin.current)setRegion(regionOrder(origin.current,point(event)));}} onPointerUp={event=>{if(!origin.current)return;setRegion(regionOrder(origin.current,point(event)));origin.current=null;}} onPointerCancel={()=>{origin.current=null;}}>
      {video ? <video ref={videoRef} src={src} controls={!hasRegion && !['提取首帧','提取尾帧'].includes(action)} playsInline preload="auto" onLoadedData={event=>{
        const player=event.currentTarget;
        setDuration(player.duration);setEnd(player.duration);
        const displayed=document.querySelector<HTMLVideoElement>('[data-canvas-video="'+CSS.escape(node.id)+'"]');
        const time=action==='提取尾帧' ? Math.max(0,player.duration-.001) : action==='提取当前帧' ? displayed?.currentTime || 0 : 0;
        if(time && Math.abs(player.currentTime-time)>.0001) player.currentTime=time; else setReady(true);
      }} onSeeking={()=>setReady(false)} onSeeked={()=>setReady(true)} onError={()=>setError('无法读取视频。')}/> :
        <img ref={imageRef} src={src} alt={node.title} draggable={false} onLoad={()=>setReady(true)} onError={()=>setError('无法读取图片。')} />}
      {region && <span className="fmv-result-rect" style={{left:region.x*100+'%',top:region.y*100+'%',width:(region.right-region.x)*100+'%',height:(region.bottom-region.y)*100+'%'}}/>}
    </div></div>
    {hasRegion && <p>拖动选择处理区域。<button disabled={!region} style={{visibility:region?'visible':'hidden'}} onClick={()=>setRegion(null)}>重新选择</button></p>}
    {hasRange && <div className="fmv-result-range"><label>入点（秒）<input type="number" min={0} max={duration} step=".01" value={start} onChange={e=>setStart(Number(e.target.value))}/></label>
      <label>出点（秒）<input type="number" min={0} max={duration} step=".01" value={end} onChange={e=>setEnd(Number(e.target.value))}/></label></div>}
    {isDraft && <textarea aria-label="修改要求" value={instructions} onChange={e=>setInstructions(e.target.value)} placeholder="描述修改要求" maxLength={4000}/>}
    {error && <p role="alert">{error}</p>}{invalidClip && <p role="alert">片段截取支持时长不超过 10 分钟且短边不超过 1080 像素的视频。</p>}
    <footer><button disabled={busy} onClick={close}>取消</button><button disabled={busy || !ready || !!emptyRegion || invalidClip || (hasRange && !(end > start && end <= duration && start >= 0))} onClick={()=>void submit()}>{busy ? '处理中…' : isDraft ? '使用到画布' : '保存到画布'}</button></footer>
  </dialog>,document.body);
}

export function CanvasResultTools(props: Props) {
  const {node,onSplit}=props;
  const [open,setOpen]=useState<string|null>(null);
  const [action,setAction]=useState('');
  const video=node.type==='video';
  const main=video?['提取帧','截取片段','更多']:['高清放大','扩图','局部重绘','擦除','裁切','更多'];
  const options=open==='提取帧'?frameTools:video?['片段重拍','裁切','视频高清','画面编辑','提取音轨','静音视频','去字幕','下载']:['重新打光','变化机位','网格拆图','下载'];
  const start=(name:string)=>{setOpen(null);if(name==='网格拆图')onSplit();else setAction(name);};
  return <div className="fmv-result-tools" data-canvas-no-zoom onPointerDown={e=>e.stopPropagation()} onMouseDown={e=>e.stopPropagation()} onClick={e=>e.stopPropagation()} onKeyDown={e=>{if(e.key==='Escape'){setOpen(null);e.stopPropagation();}}}>
    {main.map(name=><button key={name} aria-expanded={name==='更多'||name==='提取帧'?open===name:undefined} onClick={()=>name==='更多'||name==='提取帧'?setOpen(open===name?null:name):start(name)}>{name==='更多'?<MoreHorizontal size={16}/>:name}{name==='提取帧'&&<ChevronDown size={12}/>} {name==='更多'&&<span className="sr-only">更多</span>}</button>)}
    {open && <div className="fmv-result-menu">{options.map(name=>name==='下载'?<a key={name} href={assetContentUrl(node.metadata!.storageKey!)} download={node.title}><Download size={13}/>{name}</a>:
      <button key={name} onClick={()=>start(name)}>{name==='网格拆图'&&<Grid2x2 size={13}/>} {name}</button>)}</div>}
    {action && <EditResult key={node.id+action} {...props} action={action} close={()=>setAction('')}/>}
  </div>;
}
