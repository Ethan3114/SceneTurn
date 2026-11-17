'use client';

import { useRef, useState } from 'react';
import { answerMaterialReview, type MaterialConversation } from '@/app/_utils/materialApi';
import { operationId } from '@/app/_utils/cloudApi';
import { useMaterialWorkspace } from './store';

export default function MaterialReview({ value }: { value: MaterialConversation }) {
  const review = value.pendingReview;
  const [answers, setAnswers] = useState<Record<string, string>>(() => Object.fromEntries((review?.questions || []).map((item) => [item.id, item.suggestedAnswer])));
  const [changes, setChanges] = useState('');
  const [busy, setBusy] = useState(false);
  const requestRef = useRef<{ payload: string; operation: string } | null>(null);
  if (!review) return null;

  async function submit() {
    if (!review || busy) return;
    setBusy(true);
    try {
      const payload = JSON.stringify({ reviewId: review.id, answers, changes, revision: value.revision });
      const request = requestRef.current?.payload === payload ? requestRef.current : { payload, operation: operationId() };
      requestRef.current = request;
      const updated = await answerMaterialReview(value, review.id, answers, changes, value.revision, request.operation);
      useMaterialWorkspace.setState({ conversation: updated, error: '' });
    } catch (problem) {
      useMaterialWorkspace.setState({ error: problem instanceof Error ? problem.message : '确认失败' });
    } finally { setBusy(false); }
  }

  return <form className="material-review" data-testid="material-review" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
    <strong>总监 · 等待你的确认</strong><p>{review.message}</p>
    {review.questions.map((item) => <label key={item.id}><span>{item.question}</span><textarea aria-label={item.question} value={answers[item.id] || ''} onChange={(event) => setAnswers({ ...answers, [item.id]: event.target.value })} required maxLength={4000} rows={3} disabled={busy} /></label>)}
    {review.status === 'pending' && review.imageCount > 0 && <div><p>确认后生成 {review.imageCount} 张图片：</p><ul>{review.images.map((image, index) => <li key={index}>{image.label} · {image.width} × {image.height}</li>)}</ul></div>}
    {review.canConfirm && <><label><span>需要调整的内容（选填）</span><textarea aria-label="成果修改要求" value={changes} onChange={(event) => setChanges(event.target.value)} maxLength={12000} rows={2} disabled={busy} /></label>
      <button className="material-primary" disabled={busy || review.questions.some((item) => !answers[item.id]?.trim())} type="submit">{changes.trim() || review.questions.some((item) => answers[item.id]?.trim() !== item.suggestedAnswer.trim()) ? '提交修改要求' : review.remainingQuestions > review.questions.length ? '确认并继续核对' : review.status === 'pending' && review.imageCount ? '确认内容并生成图片' : '确认成果，继续制作'}</button></>}
    {!review.canConfirm && <p role="status">任务状态保存在项目中，可以稍后回来继续查看。</p>}
  </form>;
}
