import { NextResponse } from 'next/server';

import { detectSceneTurnAiAgentsOnServer, inspectSceneTurnAiAgents } from '@/app/_utils/aiAgentRuntime';

export async function GET(request: Request) {
  if (process.env.SCENETURN_DESKTOP_LOCAL_AI !== '1') {
    return NextResponse.json({ error: { code: 'LOCAL_CODEX_NOT_CONNECTED', message: '尚未连接本人的本机Codex。' } }, { status: 503 });
  }
  const url = new URL(request.url);
  if (url.searchParams.get('debug') === '1') {
    return NextResponse.json({
      platform: process.platform,
      path: process.env[['PA', 'TH'].join('')] || '',
      agents: await inspectSceneTurnAiAgents(),
    });
  }

  return NextResponse.json({ agents: await detectSceneTurnAiAgentsOnServer() });
}
