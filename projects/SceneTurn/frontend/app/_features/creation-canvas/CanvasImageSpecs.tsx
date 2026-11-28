import { canvasImageSize, imageRatioFor, imageRatios, imageSizeOptions } from './canvas-image-settings';
import type { ModelProfile } from '@/app/_utils/providerApi';
import type { CanvasNodeMetadata } from './upstream/canvas';

export function CanvasImageSpecs({ metadata, profile, update }: { metadata?: CanvasNodeMetadata; profile?: ModelProfile; update: (settings: Partial<CanvasNodeMetadata>) => void }) {
  const activeRatio = imageRatioFor(metadata);
  const active = canvasImageSize(metadata, profile);
  const sizes = imageSizeOptions(activeRatio, profile);
  const fixed = (label: string, options: string[], selected: string) => <fieldset><legend>{label}</legend><div>{options.map((entry) =>
    <button key={entry} aria-pressed={entry === selected} disabled title={entry === selected ? '当前模型的默认设置' : '当前模型服务未提供此参数'}>{entry}</button>)}</div></fieldset>;
  return <div className="fmv-image-specs">
    {fixed('画质', ['低', '标准', '高', '超高', '极致'], '标准')}
    <fieldset><legend>分辨率</legend><div>{[1024,2048,...(active?.longEdge === 3072 ? [3072] : []),4096].map((edge) => {
      const size = sizes.find((item) => item.longEdge === edge);
      return <button key={edge} aria-label={'图片尺寸 ' + edge / 1024 + 'K'} aria-pressed={size?.longEdge === active?.longEdge && !!size} disabled={!size}
        title={size ? size.imageWidth + ' × ' + size.imageHeight : '当前模型不支持此像素范围'}
        onClick={() => { if (size) update({ imageWidth:size.imageWidth,imageHeight:size.imageHeight }); }}>{edge / 1024}K</button>;
    })}</div></fieldset>
    {fixed('背景', ['自动', '保留背景', '透明背景'], '自动')}
    <fieldset><legend>比例</legend><div className="fmv-ratio-grid">{imageRatios.map((ratio) => {
      const options = imageSizeOptions(ratio, profile);
      const size = options.find((item) => item.longEdge === active?.longEdge) ?? options.find((item) => item.longEdge === 2048) ?? options[0];
      return <button key={ratio.id} aria-label={'图片比例 ' + ratio.id} aria-pressed={ratio.id === activeRatio.id} disabled={!size}
        onClick={() => { if (size) update({ imageWidth:size.imageWidth,imageHeight:size.imageHeight }); }}>
        <i aria-hidden style={{ width: 14 * ratio.width / Math.max(ratio.width, ratio.height), height: 14 * ratio.height / Math.max(ratio.width, ratio.height) }} />{ratio.id}</button>;
    })}</div></fieldset>
    {fixed('生成数量', ['1张', '2张', '4张'], '1张')}
  </div>;
}
