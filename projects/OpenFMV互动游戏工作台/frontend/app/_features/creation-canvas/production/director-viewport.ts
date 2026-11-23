import * as T from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { TransformControls } from 'three/addons/controls/TransformControls.js';
import { applyPose, disposeObject, loadStageModel, poseAt, stageShape, type DirectorScene, type StageObject, type XYZ } from './director-scene';

type Transform = Pick<StageObject,'position'|'rotation'|'scale'>;
export class DirectorViewport {
  private renderer=new T.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});
  private scene=new T.Scene();
  private camera=new T.PerspectiveCamera(45,1,.05,1000);
  private orbit:OrbitControls;
  private transform:TransformControls;
  private grid=new T.GridHelper(100,100,0x808080,0x515151);
  private objects=new Map<string,T.Group>();
  private ready=new Map<string,Promise<void>>();
  private observer:ResizeObserver;
  private snapshot?:DirectorScene;
  private viewId='';
  private frame=0;
  private dead=false;
  private panorama='';
  private panoramaTexture?:T.Texture;
  private timelineTime:number|null=null;
  private size=new T.Vector2();
  constructor(private host:HTMLDivElement, private projectId:string, onMove:(id:string,value:Transform)=>void, private onError:(e:string)=>void) {
    this.renderer.setPixelRatio(Math.min(devicePixelRatio,2));host.append(this.renderer.domElement);
    this.orbit=new OrbitControls(this.camera,this.renderer.domElement);this.reset();
    this.transform=new TransformControls(this.camera,this.renderer.domElement);this.transform.setSize(.8);
    this.scene.add(this.grid,this.transform.getHelper(),new T.HemisphereLight(0xffffff,0xafb3bc,2.7));
    const key=new T.DirectionalLight(0xffffff,2);key.position.set(3,6,4);this.scene.add(key);
    this.transform.addEventListener('dragging-changed',e=>{this.orbit.enabled=!e.value&&!this.viewId;});
    this.transform.addEventListener('mouseUp',()=>{const o=this.transform.object;if(o)onMove(o.userData.stageId,{position:o.position.toArray(),rotation:[o.rotation.x,o.rotation.y,o.rotation.z].map(T.MathUtils.radToDeg) as XYZ,scale:o.scale.toArray()});});
    this.observer=new ResizeObserver(()=>{this.renderer.setSize(Math.max(1,host.clientWidth),Math.max(1,host.clientHeight));});this.observer.observe(host);
    this.renderer.setSize(Math.max(1,host.clientWidth),Math.max(1,host.clientHeight));
    const loop=()=>{if(this.dead)return;this.render(this.viewId,this.timelineTime);this.frame=requestAnimationFrame(loop);};loop();
  }
  reset(){this.camera.position.set(4,2.7,5);this.orbit.target.set(0,.85,0);this.orbit.update();}
  setTool(mode:'translate'|'rotate'|'scale'){this.transform.setMode(mode);}
  seek(time:number|null){this.timelineTime=time;}
  sync(data:DirectorScene,selected:string,view:string){
    this.snapshot=data;this.viewId=view;this.orbit.enabled=!view;
    for(const [id,group] of this.objects){if(!data.objects.some(o=>o.id===id)){this.transform.detach();group.removeFromParent();disposeObject(group);this.objects.delete(id);this.ready.delete(id);}}
    for(const o of data.objects){
      let group=this.objects.get(o.id);
      if(!group){
        group=stageShape(o);group.userData.stageId=o.id;this.objects.set(o.id,group);this.scene.add(group);
        if(o.modelId){const target=group;this.ready.set(o.id,loadStageModel(this.projectId,o.modelId).then(model=>{if(this.dead||this.objects.get(o.id)!==target){disposeObject(model);return;}target.add(model);}).catch((e:Error)=>{target.userData.loadError=e.message;this.onError(e.message);}));}
      }
      if(o.kind==='actor'||o.kind==='prop')group.traverse(c=>{if(c instanceof T.Mesh && c.material instanceof T.MeshStandardMaterial)c.material.color.set(o.color);});
      applyPose(group,o);
    }
    for(const o of data.objects){const g=this.objects.get(o.id)!;const parent=(o.parentId && this.objects.get(o.parentId)) || this.scene;if(g.parent!==parent)parent.add(g);}
    const target=this.objects.get(selected);
    if(!view && target)this.transform.attach(target);else this.transform.detach();
    const panorama=data.panoramaId || '';
    if(panorama!==this.panorama){this.panorama=panorama;this.panoramaTexture?.dispose();this.panoramaTexture=undefined;
      if(panorama)new T.TextureLoader().load(`/api/v1/assets/${encodeURIComponent(panorama)}/content`,texture=>{if(this.dead||this.panorama!==panorama){texture.dispose();return;}texture.mapping=T.EquirectangularReflectionMapping;texture.colorSpace=T.SRGBColorSpace;this.panoramaTexture=texture;this.scene.background=texture;},undefined,()=>this.onError('全景背景读取失败。'));
    }
    this.scene.background=this.panoramaTexture || new T.Color(data.background);
  }
  private render(view:string,time:number|null){
    if(!this.snapshot)return;
    let camera=this.camera;
    const size=this.renderer.getSize(this.size);
    for(const o of this.snapshot.objects){
      const g=this.objects.get(o.id)!;
      if(!this.transform.dragging)applyPose(g,time===null?o:poseAt(o,time));
      const marker=g.getObjectByName('camera-marker');if(marker)marker.visible=!view;
      const lens=g.getObjectByName('lens');if(o.id===view && lens instanceof T.PerspectiveCamera)camera=lens;
    }
    this.transform.getHelper().visible=!view && Boolean(this.transform.object);this.grid.visible=!view;
    camera.aspect=size.x/size.y;camera.updateProjectionMatrix();this.renderer.render(this.scene,camera);
  }
  private async prepared(){await Promise.all(this.ready.values());if([...this.objects.values()].some(o=>o.userData.loadError))throw new Error('有模型未能读取，请检查后再导出。');}
  private paintOutput(id:string,time:number,output:HTMLCanvasElement){
    const size=this.renderer.getSize(new T.Vector2()),ratio=this.renderer.getPixelRatio();
    this.renderer.setPixelRatio(1);this.renderer.setSize(output.width,output.height,false);this.render(id,time);
    output.getContext('2d')!.drawImage(this.renderer.domElement,0,0);
    this.renderer.setPixelRatio(ratio);this.renderer.setSize(size.x,size.y,false);this.render(this.viewId,this.timelineTime);
  }
  private surface(){const c=document.createElement('canvas');c.width=1280;c.height=720;return c;}
  async photo(id:string,time:number,name:string){
    await this.prepared();const c=this.surface();this.paintOutput(id,time,c);
    const blob=await new Promise<Blob>((resolve,reject)=>c.toBlob(b=>b?resolve(b):reject(new Error('机位截图失败。')),'image/jpeg',.94));
    return new File([blob],name+'.jpg',{type:'image/jpeg'});
  }
  async movie(id:string,duration:number,fps:number,name:string){
    await this.prepared();
    const mime=['video/mp4;codecs=avc1.42E01E','video/mp4'].find(type=>typeof MediaRecorder!=='undefined' && MediaRecorder.isTypeSupported(type));
    if(!mime)throw new Error('当前浏览器缺少 MP4 录像编码器。');
    const c=this.surface();this.paintOutput(id,0,c);const stream=c.captureStream(fps);
    try{
      const recorder=new MediaRecorder(stream,{mimeType:mime,videoBitsPerSecond:5_000_000});const chunks:Blob[]=[];
      recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data);};
      const end=new Promise<void>((resolve,reject)=>{recorder.onstop=()=>resolve();recorder.onerror=()=>reject(new Error('导出动画失败。'));});
      recorder.start();
      const start=performance.now();
      while(!this.dead){const t=Math.min(duration,(performance.now()-start)/1000);this.paintOutput(id,t,c);if(t>=duration)break;await new Promise(resolve=>setTimeout(resolve,1000/fps));}
      recorder.stop();await end;if(this.dead||!chunks.length)throw new Error('动画导出已中止。');
      return new File(chunks,name+'.mp4',{type:'video/mp4'});
    }finally{for(const track of stream.getTracks())track.stop();}
  }
  dispose(){this.dead=true;cancelAnimationFrame(this.frame);this.observer.disconnect();this.transform.detach();this.transform.dispose();this.orbit.dispose();this.panoramaTexture?.dispose();disposeObject(this.scene);this.grid.dispose();this.renderer.dispose();this.renderer.domElement.remove();}
}
