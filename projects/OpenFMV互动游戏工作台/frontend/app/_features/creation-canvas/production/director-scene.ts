import * as T from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { GLTFExporter } from 'three/addons/exporters/GLTFExporter.js';
import { api } from '@/app/_utils/cloudApi';

export type XYZ = [number,number,number];
export type ObjectPose = { position:XYZ; rotation:XYZ; scale:XYZ; pose:string; fov:number };
export type StageObject = ObjectPose & { id:string; name:string; kind:'actor'|'prop'|'camera'|'group'|'model'; color:string; modelId?:string; parentId?:string; keys:{time:number;pose:ObjectPose}[] };
export type DirectorScene = { objects:StageObject[]; duration:number; cameraId:string; background:string; panoramaId?:string };
export function stageObject(kind:StageObject['kind'],name:string):StageObject {
  return {id:crypto.randomUUID(),kind,name,color:kind==='actor'?'#4f8ef7':'#bdbdbd',position:kind==='camera'?[0,1.55,5]:[0,0,0],rotation:[0,0,0],scale:[1,1,1],pose:'站立',fov:45,keys:[]};
}
export function initialScene():DirectorScene {
  const camera=stageObject('camera','机位1');
  return { objects:[camera,stageObject('actor','角色A')],duration:5,cameraId:camera.id,background:'#2b2c2e' };
}
export function snapshotPose(o:ObjectPose):ObjectPose {
  return {position:[...o.position],rotation:[...o.rotation],scale:[...o.scale],fov:o.fov,pose:o.pose};
}
export function poseAt(o:StageObject,t:number):ObjectPose {
  if (!o.keys.length) return o;
  const after=o.keys.find(k=>k.time>=t) || o.keys[o.keys.length-1];
  const before=o.keys.filter(k=>k.time<=t).at(-1) || {time:0,pose:o};
  const blend=after.time<=before.time?0:(t-before.time)/(after.time-before.time);
  const vector=(a:XYZ,b:XYZ):XYZ=>a.map((v,i)=>T.MathUtils.lerp(v,b[i],blend)) as XYZ;
  return {position:vector(before.pose.position,after.pose.position),rotation:vector(before.pose.rotation,after.pose.rotation),scale:vector(before.pose.scale,after.pose.scale),pose:blend===1?after.pose.pose:before.pose.pose,fov:T.MathUtils.lerp(before.pose.fov,after.pose.fov,blend)};
}
function closedManager() {
  const m=new T.LoadingManager();
  m.setURLModifier(url=>{ if (!/^(data:|blob:)/.test(url)) throw new Error('请选择包含贴图与缓冲数据的模型文件。'); return url; });
  return m;
}
export async function importStageModel(projectId:string,file:File) {
  if (file.size>16*1024*1024) throw new Error('导演台模型须在 16 MiB 以内。');
  const ext=file.name.split('.').at(-1)?.toLowerCase();
  const manager=closedManager();
  let object:T.Object3D;
  if(ext==='glb'||ext==='gltf') object=(await new GLTFLoader(manager).parseAsync(await file.arrayBuffer(),'')).scene;
  else if(ext==='obj') { const {OBJLoader}=await import('three/addons/loaders/OBJLoader.js');object=new OBJLoader(manager).parse(await file.text()); }
  else if(ext==='fbx') { const {FBXLoader}=await import('three/addons/loaders/FBXLoader.js');object=new FBXLoader(manager).parse(await file.arrayBuffer(),''); }
  else if(ext==='dae') { const {ColladaLoader}=await import('three/addons/loaders/ColladaLoader.js');const result=new ColladaLoader(manager).parse(await file.text(),'');if(!result) throw new Error('无法读取 DAE 场景。');object=result.scene; }
  else if(ext==='stl') { const {STLLoader}=await import('three/addons/loaders/STLLoader.js');object=new T.Mesh(new STLLoader(manager).parse(await file.arrayBuffer()),new T.MeshStandardMaterial()); }
  else if(ext==='ply') { const {PLYLoader}=await import('three/addons/loaders/PLYLoader.js');const geometry=new PLYLoader(manager).parse(await file.arrayBuffer());object=geometry.index?new T.Mesh(geometry,new T.MeshStandardMaterial({vertexColors:geometry.hasAttribute('color')})):new T.Points(geometry,new T.PointsMaterial({size:.025,vertexColors:geometry.hasAttribute('color')})); }
  else throw new Error('此格式尚未接入导演台。');
  try {
    const document=await new GLTFExporter().parseAsync(object,{binary:false});
    const body=new FormData();body.append('file',new Blob([JSON.stringify(document)],{type:'model/gltf+json'}),'scene.gltf');
    return (await api<{id:string}>(`/projects/${projectId}/media-tools/director-models`,{method:'POST',body})).id;
  } finally { disposeObject(object); }
}
export function disposeObject(object:T.Object3D) {
  const materials=new Set<T.Material>();
  object.traverse(child=>{if(child instanceof T.Mesh || child instanceof T.Points){child.geometry.dispose(); for(const m of Array.isArray(child.material)?child.material:[child.material]) materials.add(m);}});
  for(const m of materials){for(const value of Object.values(m))if(value instanceof T.Texture)value.dispose();m.dispose();}
}
export async function loadStageModel(projectId:string,id:string) {
  const response=await fetch(`/api/v1/projects/${projectId}/media-tools/director-models/${id}`,{credentials:'same-origin'});
  if(!response.ok)throw new Error('无法读取已保存的导演台模型。');
  return (await new GLTFLoader(closedManager()).parseAsync(await response.text(),'')).scene;
}
export function stageShape(o:StageObject) {
  const group=new T.Group();
  const material=new T.MeshStandardMaterial({color:o.color});
  const part=(name:string,g:T.BufferGeometry,p:XYZ)=>{const m=new T.Mesh(g,material);m.position.set(...p);m.name=name;group.add(m);return m;};
  if(o.kind==='actor') {
    part('头',new T.SphereGeometry(.14,16,12),[0,1.61,0]);
    part('身体',new T.CapsuleGeometry(.19,.39,6,12),[0,1.12,0]);
    for(const direction of [-1,1]){
      part(direction<0?'左臂':'右臂',new T.CapsuleGeometry(.065,.42,4,10),[direction*.27,1.12,0]);
      part(direction<0?'左腿':'右腿',new T.CapsuleGeometry(.085,.6,4,10),[direction*.11,.43,0]);
    }
  } else if(o.kind==='prop') part('道具',new T.BoxGeometry(.8,.8,.8),[0,.4,0]);
  else if(o.kind==='camera'){const c=new T.PerspectiveCamera(o.fov,16/9,.05,1000);c.name='lens';group.add(c);const marker=part('camera-marker',new T.ConeGeometry(.14,.24,4),[0,.05,0]);marker.rotation.x=Math.PI/2;}
  if(!group.children.some(c=>c instanceof T.Mesh)) material.dispose();
  return group;
}
export function applyPose(group:T.Group,pose:ObjectPose) {
  group.position.set(...pose.position);group.rotation.set(...pose.rotation.map(x=>T.MathUtils.degToRad(x)) as XYZ);group.scale.set(...pose.scale);
  const body=group.getObjectByName('右臂');if(body){body.rotation.z=pose.pose==='抬手'?-.95:0;group.getObjectByName('左腿')!.rotation.x=pose.pose==='迈步'?-.5:0;group.getObjectByName('右腿')!.rotation.x=pose.pose==='迈步'?.5:0;}
  const lens=group.getObjectByName('lens');if(lens instanceof T.PerspectiveCamera && lens.fov!==pose.fov){lens.fov=pose.fov;lens.updateProjectionMatrix();}
}
