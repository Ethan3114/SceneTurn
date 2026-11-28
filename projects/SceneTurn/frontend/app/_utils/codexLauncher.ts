const sceneTurnOrigin = 'https://sceneturn.ethan-g.online';
const launcherUrl = 'sceneturn-codex://open';
const pairingEndpoint = 'http://127.0.0.1:3212/pair';

type LauncherPairing = { pairingId: string; pairingCode: string; projectId: string };

export function openCodexLauncher(): boolean {
  if (window.location.origin !== sceneTurnOrigin) return false;
  window.location.assign(launcherUrl);
  return true;
}

export async function handOffCodexPairing({ pairingId, pairingCode, projectId }: LauncherPairing): Promise<boolean> {
  if (window.location.origin !== sceneTurnOrigin) return false;

  const signal = AbortSignal.timeout(20000);
  const request: RequestInit & { targetAddressSpace: 'loopback' } = {
    method: 'POST',
    mode: 'cors',
    credentials: 'omit',
    cache: 'no-store',
    redirect: 'error',
    referrerPolicy: 'no-referrer',
    targetAddressSpace: 'loopback',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pairingId, pairingCode, projectId }),
    signal,
  };

  while (!signal.aborted) {
    const response = await fetch(pairingEndpoint, request).catch(() => null);
    if (response) return response.status === 202;
    if (!signal.aborted) await new Promise<void>((resolve) => window.setTimeout(resolve, 400));
  }
  return false;
}
