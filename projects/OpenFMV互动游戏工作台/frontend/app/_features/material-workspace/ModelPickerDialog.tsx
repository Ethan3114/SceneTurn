'use client';

import { useId, useState, type ReactElement } from 'react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';

import * as DialogPrimitive from '@radix-ui/react-dialog';
import { Check, FileText, Film, Image as ImageIcon } from 'lucide-react';

import { modelKinds, type ModelDefaults, type ModelKind, type ModelProfile, type ProviderCatalog } from '@/app/_utils/providerApi';
import './model-picker.css';

type PickerProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  trigger: ReactElement;
  catalog?: ProviderCatalog | null;
  selection?: Partial<ModelDefaults>;
  onApply: (selection: Partial<ModelDefaults>) => void;
  kind?: ModelKind;
  loading?: boolean;
  error?: string;
  onRetry?: () => void;
  connectHref?: string;
};

const icons = { text: FileText, image: ImageIcon, video: Film };
const providers: Record<string, string> = { deepseek: 'DeepSeek', 'aliyun-qwen': 'qwen', sensetime: 'sensetime', 'volcengine-ark': 'ark', 'aliyun-wan': 'wan' };

export function useModelDisplay() {
  const t = useTranslations('materialModels');
  return (model: ModelProfile) => model.capabilities.localConnection ? t('personalCodex') : model.label;
}

function ModelOptions({ catalog, selection, kind, loading, error, onRetry, onApply, connectHref }: Omit<PickerProps, 'open' | 'onOpenChange' | 'trigger'>) {
  const t = useTranslations('materialModels');
  const displayModel = useModelDisplay();
  const [draft, setDraft] = useState(() => ({ ...selection }));
  const groupId = useId();
  const visibleKinds = kind ? [kind] : modelKinds;
  const canUseFrame = (model: ModelProfile) => Boolean(kind || model.kind !== 'video' || model.capabilities.lastFrame);
  const modelsFor = (entry: ModelKind) => catalog?.models.filter((model) => model.kind === entry && (canUseFrame(model) || model.id === catalog.defaults[entry])) ?? [];
  const canApply = catalog && !loading && !error && visibleKinds.every((entry) => {
    const id = draft[entry];
    const model = modelsFor(entry).find((item) => item.id === (id ?? catalog.defaults[entry]));
    if (model && !canUseFrame(model)) return false;
    return kind || id ? model?.available : true;
  });

  function selectModel(entry: ModelKind, id: string) {
    setDraft((current) => {
      const next = { ...current };
      if (!kind && id === catalog?.defaults[entry]) delete next[entry];
      else next[entry] = id;
      return next;
    });
  }

  function details(model: ModelProfile) {
    if (model.capabilities.localConnection) return model.available ? t('localConnected') : t('localDisconnected');
    const key = providers[model.providerId];
    const name = key === 'DeepSeek' ? key : key ? t(key) : model.providerId;
    const frameCapability = model.capabilities.lastFrame ? t('bothFrames') : model.capabilities.firstFrame ? t('firstFrame') : '';
    return [name, frameCapability].filter(Boolean).join(' · ');
  }

  return <>
    <header className="material-model-dialog-header">
      <DialogPrimitive.Title>{kind ? t('choose', { model: t(kind) }) : t('title')}</DialogPrimitive.Title>
      <DialogPrimitive.Description>{kind ? t('projectDescription') : t('description')}</DialogPrimitive.Description>
    </header>
    {error && <div className="material-model-dialog-notice" role="alert"><p>{error}</p>{onRetry && <button type="button" onClick={onRetry}>{t('retry')}</button>}</div>}
    {loading && <p className="material-model-dialog-notice" role="status">{t('loading')}</p>}
    {catalog && <div className="material-model-columns" aria-busy={loading || undefined}>
      {visibleKinds.map((entry) => {
        const Icon = icons[entry];
        const selected = draft[entry] ?? catalog.defaults[entry];
        const options = modelsFor(entry);
        const canReset = !kind && draft[entry] && !options.some((model) => model.id === selected && model.available && canUseFrame(model));
        return <fieldset key={entry} className="material-model-column" disabled={loading || Boolean(error)}>
          <legend><Icon size={18} aria-hidden="true" />{t(`${entry}Model`)}</legend>
          <div className="material-model-options">
            {options.map((model) => <div key={model.id}>
              <label className="material-model-option" data-model-id={model.id}>
                <input name={`${groupId}-${entry}`} type="radio" value={model.id} aria-label={displayModel(model)} aria-describedby={`${groupId}-${model.id}`} checked={selected === model.id} disabled={!model.available || !canUseFrame(model)} onChange={() => selectModel(entry, model.id)} />
                <span className="material-model-option-body">
                  <strong>{displayModel(model)}</strong>
                  <span id={`${groupId}-${model.id}`} className="material-model-option-info">
                    <span>{details(model)}</span>
                    {!canUseFrame(model) ? <span>{t('requiresBothFrames')}</span> : !model.available && !model.capabilities.localConnection && <span>{model.unavailableReason || t('unavailable')}</span>}
                  </span>
                </span>
                <span className="material-model-option-check" aria-hidden="true"><Check size={13} /></span>
              </label>
              {model.capabilities.localConnection && !model.available && connectHref && <Link className="material-model-connect" href={connectHref} target="_blank" rel="noopener noreferrer">{t('connect')}</Link>}
            </div>)}
            {canReset && <button type="button" onClick={() => selectModel(entry, catalog.defaults[entry])}>{t('resetDefault')}</button>}
          </div>
        </fieldset>;
      })}
    </div>}
    <footer className="material-model-dialog-footer">
      {!kind && <p>{t('usesDefaults')}</p>}
      <div>
        <DialogPrimitive.Close type="button">{t('cancel')}</DialogPrimitive.Close>
        <button className="material-model-apply" disabled={!canApply} type="button" onClick={() => {
          if (!catalog || !canApply) return;
          onApply(kind ? { ...draft, [kind]: draft[kind] ?? catalog.defaults[kind] } : draft);
        }}><Check size={16} aria-hidden="true" />{kind ? t('applyDefault') : t('apply')}</button>
      </div>
    </footer>
  </>;
}

export default function ModelPickerDialog({ open, onOpenChange, trigger, ...props }: PickerProps) {
  return <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
    <DialogPrimitive.Trigger asChild>{trigger}</DialogPrimitive.Trigger>
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="material-model-overlay" />
      <DialogPrimitive.Content className="material-model-dialog" data-scope={props.kind ? 'project' : 'next'}>
        <ModelOptions {...props} onApply={(selection) => { props.onApply(selection); onOpenChange(false); }} />
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  </DialogPrimitive.Root>;
}
