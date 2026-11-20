'use client';

import { useRef, useState } from 'react';
import { answerMaterialReview, type MaterialConversation } from '@/app/_utils/materialApi';
import { assetContentUrl, operationId } from '@/app/_utils/cloudApi';
import { useMaterialWorkspace } from './store';
import MaterialReferenceImages, { requireProjectReferenceImages } from './MaterialReferenceImages';

export default function MaterialReview({ value }: { value: MaterialConversation }) {
  const review = value.pendingReview;
  const clarification = review?.kind === 'clarification';
  const [answers, setAnswers] = useState<Record<string, string>>(() => clarification ? {} : Object.fromEntries((review?.questions || []).map((item) => [item.id, item.suggestedAnswer])));
  const [changes, setChanges] = useState('');
  const [referenceAssetIds, setReferenceAssetIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const requestRef = useRef<{ payload: string; operation: string } | null>(null);
  const frozenIds = new Set(value.selectedReferenceAssets.map((asset) => asset.assetId));
  const addedReferenceAssetIds = referenceAssetIds.filter((id) => !frozenIds.has(id));
  const needsReferenceChanges = addedReferenceAssetIds.length > 0 && !changes.trim();
  if (!review) return null;
  const hasModifiedAnswer = review.questions.some((item) => answers[item.id]?.trim() !== item.suggestedAnswer.trim());
  const hasUnansweredQuestion = review.questions.some((item) => !answers[item.id]?.trim());
  const willRevise = addedReferenceAssetIds.length > 0 || !!changes.trim() || hasModifiedAnswer;
  const confirmLabel = review.remainingQuestions > review.questions.length ? '确认并继续核对' :
    review.status === 'pending' && review.imageCount ? '确认内容并生成图片' : '确认成果，继续制作';
  const actionLabel = clarification ? '提交答复' : willRevise ? '提交修改要求' : confirmLabel;

  async function submit() {
    const current = useMaterialWorkspace.getState();
    if (!review || busy || !review.canConfirm || hasUnansweredQuestion || current.loading || current.conversation?.id !== value.id) return;
    if (needsReferenceChanges) {
      useMaterialWorkspace.setState({ error: '请填写成果修改要求，说明如何使用新增参考图片。' });
      return;
    }
    setBusy(true);
    try {
      requireProjectReferenceImages(value.projectId, addedReferenceAssetIds);
      const payload = JSON.stringify({ reviewId: review.id, answers, changes, referenceAssetIds: addedReferenceAssetIds, revision: value.revision });
      const request = requestRef.current?.payload === payload ? requestRef.current : { payload, operation: operationId() };
      requestRef.current = request;
      const updated = await answerMaterialReview(value, review.id, answers, changes, value.revision, request.operation, addedReferenceAssetIds);
      if (useMaterialWorkspace.getState().conversation?.id === value.id && !useMaterialWorkspace.getState().loading) useMaterialWorkspace.setState({ conversation: updated, error: '' });
      setReferenceAssetIds([]);
      setChanges('');
    } catch (problem) {
      useMaterialWorkspace.setState({ error: problem instanceof Error ? problem.message : '确认失败' });
    } finally { setBusy(false); }
  }

  return <form className="material-review" data-testid="material-review" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
    <strong>{review.canConfirm ? '总编 · 等待你的确认' : '制作进度'}</strong><p>{review.message}</p>
    {!clarification && <MaterialReferenceImages projectId={value.projectId} selectedIds={addedReferenceAssetIds} onChange={setReferenceAssetIds} frozenAssets={value.selectedReferenceAssets} disabled={busy || !review.canConfirm} actionLabel="补充参考图片" />}
    {review.questions.map((item) => <div key={item.id}>
      <label><span>{item.question}</span><textarea aria-label={item.question} value={answers[item.id] || ''} onChange={(event) => setAnswers({ ...answers, [item.id]: event.target.value })} required maxLength={4000} rows={3} disabled={busy || !review.canConfirm} /></label>
      {clarification && item.suggestedAnswer && <div className="material-review-suggestion">
        <p className="material-subtle">建议：{item.suggestedAnswer}</p>
        <button type="button" disabled={busy || !review.canConfirm} onClick={() => setAnswers({ ...answers, [item.id]: item.suggestedAnswer })}>采用建议</button>
      </div>}
    </div>)}
    {review.status === 'pending' && (review.imageCount > 0 || review.reusedImages.length > 0) && <div><p>确认后新生成 {review.imageCount} 张图片：</p>{review.imageCount > 0 && <ul>{review.images.map((image, index) => <li key={index}>{image.label} · {image.width} × {image.height}</li>)}</ul>}</div>}
    {review.reusedImages.length > 0 && <div><p>复用 {review.reusedImages.length} 张原图：</p><div className="material-video-boundaries">{review.reusedImages.map((image) => <figure key={image.frameKey}>
      <img src={assetContentUrl(image.assetId)} alt={image.label} width={image.width} height={image.height} loading="lazy" />
      <figcaption>{image.label} · 复用原图 · {image.width} × {image.height}</figcaption>
    </figure>)}</div></div>}
    {review.canConfirm && !clarification && <><label><span>{addedReferenceAssetIds.length ? '需要调整的内容（新增参考图片后必填）' : '需要调整的内容（选填）'}</span><textarea aria-label="成果修改要求" value={changes} onChange={(event) => setChanges(event.target.value)} required={addedReferenceAssetIds.length > 0} maxLength={12000} rows={2} disabled={busy} /></label>
      {needsReferenceChanges && <p className="material-subtle" role="status">请说明如何根据新增参考图片调整成果，再提交修改要求。</p>}
    </>}
    {review.canConfirm && <button className="material-primary" disabled={busy || needsReferenceChanges || hasUnansweredQuestion} type="submit">{actionLabel}</button>}
    {!review.canConfirm && <p role="status">任务状态保存在项目中，可以稍后回来继续查看。</p>}
  </form>;
}
