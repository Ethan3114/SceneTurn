import { NextResponse } from 'next/server';

import { testSceneTurnAiAgentOnServer } from '@/app/_utils/aiAgentRuntime';

export async function POST(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  if (process.env.SCENETURN_DESKTOP_LOCAL_AI !== '1') {
    return NextResponse.json({ error: { code: 'LOCAL_CODEX_NOT_CONNECTED', message: '尚未连接本人的本机Codex。' } }, { status: 503 });
  }
  const { id } = await params;

  return NextResponse.json(await testSceneTurnAiAgentOnServer(id));
}
