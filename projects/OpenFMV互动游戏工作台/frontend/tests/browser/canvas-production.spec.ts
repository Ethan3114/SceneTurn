import { expect, test, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { addCanvasNode } from './manual-studio';

test.use({ channel:'chrome', viewport:{width:1512,height:982} });
async function openStudio(page:Page){
  const origin=process.env.OPENFMV_TEST_BASE_URL || 'http://127.0.0.1:3210';
  const account=await page.request.post('/api/v1/auth/register',{headers:{Origin:origin},data:{email:`production-${crypto.randomUUID()}@example.com`,password:crypto.randomUUID()}});expect(account.ok()).toBe(true);
  const project=await page.request.post('/api/v1/projects',{headers:{Origin:origin},data:{title:'音频与导演台验证'}});expect(project.ok()).toBe(true);
  await page.goto('/zh-CN/studio?id='+(await project.json()).project.id,{waitUntil:'domcontentloaded'});
  await expect(page.getByTestId('canvas-add-node')).toBeEnabled({timeout:30000});
}
test('四类音频标签独立保存，真实旁白保存并继续剪辑', async({page})=>{
  test.setTimeout(120000);await openStudio(page);
  await addCanvasNode(page,'audio');
  const panel=page.getByRole('region',{name:'音频生成'});
  await panel.getByRole('button',{name:'音乐配乐',exact:true}).click();
  await panel.getByRole('textbox',{name:'音乐配乐描述'}).fill('无歌词，慢速弦乐。');
  await panel.getByRole('button',{name:'音效',exact:true}).click();
  await panel.getByRole('button',{name:'雨声',exact:true}).click();
  await panel.getByRole('button',{name:'音色生成',exact:true}).click();
  await panel.getByRole('textbox',{name:'音色生成描述'}).fill('一位声音温和且语速平稳的成年旁白，发音清晰，叙述平静。');
  await panel.getByRole('button',{name:'音乐配乐',exact:true}).click();
  await expect(panel.getByRole('textbox',{name:'音乐配乐描述'})).toHaveValue('无歌词，慢速弦乐。');
  await panel.getByRole('button',{name:'配音旁白',exact:true}).click();
  await panel.getByRole('textbox',{name:'配音旁白描述'}).fill('山谷中的灯光渐渐亮了起来。');
  await expect(panel.getByLabel('音色',{exact:true})).toHaveValue(/^Tingting/,{timeout:20000});
  const narration=page.waitForResponse(r=>r.url().endsWith('/media-tools/narrate'),{timeout:60000});
  await panel.getByRole('button',{name:'生成音频'}).click();
  const generated=await narration;expect(generated.status(),generated.ok()?'':await generated.text()).toBe(200);
  const audioNode=page.getByTestId('canvas-node').filter({hasText:'配音旁白.wav'});
  await expect.poll(async()=>await audioNode.count() || (await panel.getByRole('status').allTextContents()).join(' '),{timeout:45000}).toBe(1);
  await audioNode.getByRole('button',{name:'配音旁白.wav',exact:true}).click();
  await page.getByRole('button',{name:'编辑音频',exact:true}).click();
  const editor=page.getByRole('dialog',{name:'音频编辑'});
  await editor.getByRole('spinbutton',{name:'配音旁白.wav入点'}).fill('0.05');
  await editor.getByRole('button',{name:'导出到画布'}).click();
  await expect(page.getByTestId('canvas-node').filter({hasText:'音频剪辑.wav'})).toHaveCount(1,{timeout:45000});
});
test('导演台导入真实 OBJ、记录关键帧、截图及导出无声影片',async({page})=>{
  test.setTimeout(120000);await openStudio(page);
  await page.getByTestId('canvas-add-node').click();await page.getByTestId('canvas-create-director').click();
  await page.getByRole('button',{name:'打开导演台'}).click();
  const dialog=page.getByRole('dialog',{name:'3D导演台'});
  await expect(dialog.locator('canvas')).toBeVisible();
  await dialog.getByRole('button',{name:'＋道具'}).click();
  await dialog.getByLabel('位置x').fill('1.2');await dialog.getByRole('button',{name:'记录关键帧'}).click();
  await dialog.locator('input[type=file]').setInputFiles({name:'场景三角.obj',mimeType:'text/plain',buffer:Buffer.from('o Triangle\nv -1 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')});
  await expect(dialog.getByRole('button',{name:'场景三角.obj'})).toBeVisible();
  await dialog.getByRole('button',{name:'机位截图',exact:true}).click();
  await expect(page.getByTestId('canvas-node').filter({hasText:'机位1.jpg'})).toHaveCount(1,{timeout:45000});
  await dialog.getByLabel('时长',{exact:true}).fill('1');await dialog.getByRole('button',{name:'导出无声 MP4'}).click();
  await expect(page.getByTestId('canvas-node').filter({hasText:'机位1.mp4'})).toHaveCount(1,{timeout:45000});
  await dialog.getByRole('button',{name:'关闭导演台'}).click();await page.reload();
  await page.getByRole('button',{name:'打开导演台'}).click();await expect(dialog.getByRole('button',{name:'场景三角.obj'})).toBeVisible();
});

test('智能剪辑读取相连视频，调整片段和字幕后导出到画布',async({page})=>{
  test.setTimeout(120000);
  await openStudio(page);
  const projectId=new URL(page.url()).searchParams.get('id')!;
  const base='/api/v1/projects/'+projectId;
  const origin=new URL(page.url()).origin;
  const envelope=await (await page.request.get(base)).json();
  const scene=envelope.project.graphData.nodes.find((n:{type:string})=>n.type==='scene');
  const sources=[];
  for (const index of [0,1]) {
    const name='来源'+index+'.mp4';
    const upload=await page.request.post(base+'/assets',{headers:{Origin:origin},multipart:{file:{name,mimeType:'video/mp4',buffer:await readFile('public/sceneturn/canvas/guidance/rhythm-'+index+'.mp4')}}});
    expect(upload.ok()).toBe(true);
    const asset=(await upload.json()).asset;
    sources.push({id:crypto.randomUUID(),type:'video',title:name,position:{x:0,y:index*225},width:320,height:180,metadata:{storageKey:asset.id,durationMs:asset.durationMs,content:asset.url,status:'success'}});
  }
  const cutter={id:crypto.randomUUID(),type:'config',title:'智能剪辑',position:{x:430,y:100},width:350,height:200,metadata:{dramaTask:'join',generationMode:'video'}};
  const canvasUrl=base+'/canvases/'+scene.id;
  const current=await (await page.request.get(canvasUrl)).json();
  const saved=await page.request.put(canvasUrl,{headers:{Origin:origin},data:{operationId:crypto.randomUUID(),expectedRevision:current.revision,nodes:[...sources,cutter],connections:sources.map(n=>({id:crypto.randomUUID(),fromNodeId:n.id,toNodeId:cutter.id})),viewport:{x:20,y:50,k:.8}}});
  expect(saved.ok()).toBe(true);
  await page.reload({waitUntil:'domcontentloaded'});
  await page.getByTestId('canvas-node').filter({hasText:'智能剪辑'}).getByRole('button',{name:'素材混剪'}).click();
  const d=page.getByRole('dialog',{name:'素材混剪'});
  await d.getByLabel('来源0.mp4出点').fill('0.5');await d.getByLabel('来源1.mp4出点').fill('0.5');
  await d.getByLabel('提前片段').last().click();await d.getByRole('button',{name:'添加',exact:true}).click();
  await d.getByLabel('字幕正文').fill('山谷');await d.getByLabel('导出分辨率').selectOption('480');
  await d.getByRole('button',{name:'导出到画布'}).click();
  await expect(page.getByTestId('canvas-node').filter({hasText:'素材混剪.mp4'})).toHaveCount(1,{timeout:45000});
});
