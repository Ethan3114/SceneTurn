import type { CanvasNodeMetadata } from './upstream/canvas';

export type FilmGuidance = NonNullable<CanvasNodeMetadata['filmGuidance']>;
export type FilmCategory = 'style' | 'palette' | 'composition' | 'rhythm' | 'camera' | 'prompt';
export type FilmPreset = { name: string; instruction: string; cover?: string };

export const filmLabels = { style: '风格', palette: '配色', composition: '构图', rhythm: '节奏', camera: '相机', prompt: '提示词' };
export const guidanceAsset = (name: string) => '/sceneturn/canvas/guidance/' + name + '.webp';

const gallery = (names: string[], image: string): FilmPreset[] => names.map((name, index) => ({ name, instruction: name, cover: guidanceAsset(image + '-' + index) }));

export const filmPresets: Record<Exclude<FilmCategory, 'camera'>, FilmPreset[]> = {
  palette: gallery(['暗黑绿色调', '清冷蓝色调', '月光蓝色调', '暖阳金色调', '荒漠燃橙色调', '暗红夜幕色调', '粉紫暮色色调', '暖沙褐色调', '黑白光影色调', '红金迷醉色调', '复古红绿色调', '朦胧霓虹色调'], 'color'),
  composition: gallery(['三分法构图', '中心对称构图', '对角线构图', '十字形构图', 'L 形构图', 'S 形构图', '圆形构图', '金字塔构图', '放射线构图', '黄金螺旋构图', '框架构图', '水平线构图'], 'composition'),
  style: [
    { name: '黑白电影', instruction: '黑白电影，深黑阴影与硬质侧光，呈现鲜明的明暗层次。', cover: guidanceAsset('color-8') },
    { name: '写实电影', instruction: '写实电影，自然光与真实材质，保留皮肤与环境细节。', cover: guidanceAsset('composition-0') },
    { name: '复古胶片', instruction: '复古胶片摄影，柔和高光、细微颗粒与低饱和色彩。', cover: guidanceAsset('color-10') },
    { name: '霓虹夜景', instruction: '夜景电影，霓虹环境光、潮湿反射与浅景深。', cover: guidanceAsset('color-11') },
  ],
  rhythm: [
    { name: '一镜到底', instruction: '一镜到底，以连续运镜呈现完整动作，保持空间连续。' },
    { name: '缓慢推进', instruction: '保持稳定的缓慢推进，从环境逐渐收紧到主体，镜头结尾停留。' },
    { name: '快速剪辑', instruction: '按动作节拍切换镜头，保持动作方向和视线连续。' },
  ],
  prompt: [
    { name: 'AI 漫剧通用负面提示词', instruction: '避免画面模糊、肢体畸形、多余手指、面部变形、乱码文字、标志、水印与画面闪烁。' },
    { name: '白模素材参考提示词', instruction: '将参考白模用于布局、空间关系和环境元素的位置比例。按角色设定重建外观与材质。' },
    { name: 'AI 生图降噪去脏', instruction: '画面干净，保留自然的光影与纹理层次，控制噪点、色带与过度锐化。' },
    { name: '影调参考', instruction: '沿用参考图的色调、配色和光影关系，保留当前主体和构图。' },
    { name: '真实物理运动', instruction: '人物动作遵循重力、惯性和关节活动范围，衣料自然跟随，脚步与地面接触准确。' },
    { name: '深度视频参考', instruction: '沿用源视频的空间层次、运动节奏、镜头构图和运镜，按当前描述替换主体。' },
    { name: '角色换装', instruction: '依照服装参考替换衣物，保留人物脸型、姿态、肤色、背景和光线。' },
    { name: '产品精确还原', instruction: '保持参考产品的轮廓、比例、材质、纹理和可见细节。' },
    { name: '真实皮肤', instruction: '保留自然毛孔与肤纹，光线柔和，肤色过渡自然。' },
    { name: '空间透视正确', instruction: '画面共享一致的视平线和消失点，远近比例、遮挡和投影关系合理。' },
  ],
};

export const cameraColumns = [
  { key: 'camera', label: '相机', values: ['Auto', 'ARRI ALEXA 35', 'RED V-RAPTOR', 'Sony VENICE 2', '35mm 胶片摄影机'] },
  { key: 'lens', label: '镜头', values: ['Auto', '球面定焦', '变形宽银幕', '微距', '变焦'] },
  { key: 'focal', label: '焦距', values: ['Auto', '14mm', '24mm', '35mm', '50mm', '85mm', '135mm'] },
  { key: 'aperture', label: '光圈', values: ['Auto', 'f/1.4', 'f/2', 'f/2.8', 'f/4', 'f/5.6', 'f/8', 'f/11'] },
] as const;
