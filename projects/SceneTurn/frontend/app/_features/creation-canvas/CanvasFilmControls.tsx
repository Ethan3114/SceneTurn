import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Aperture, Camera, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Focus, Plus, Search, Star, Video, X } from 'lucide-react';
import { cameraColumns, filmLabels, filmPresets, guidanceAsset, type FilmCategory, type FilmGuidance, type FilmPreset } from './film-guidance';
import './film-controls.css';

type Shelf = { mine: FilmPreset[]; recent: FilmPreset[] };
type LibraryCategory = Exclude<FilmCategory, 'camera'>;
const freshShelf: Shelf = { mine: [], recent: [] };
const cameraIcons = [Camera, Video, Focus, Aperture];

function FilmDialog({ title, close, children }: { title: string; close: () => void; children: React.ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current!;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return createPortal(<dialog ref={ref} aria-label={title} className="fmv-film-dialog" data-canvas-no-zoom
    onCancel={(event) => { event.preventDefault(); close(); }}
    onClick={(event) => { event.stopPropagation(); if (event.target === ref.current) close(); }}
    onKeyDown={(event) => event.stopPropagation()} onPointerDown={(event) => event.stopPropagation()} onWheel={(event) => event.stopPropagation()}>
    <div className="fmv-film-window">{children}
      <button type="button" className="fmv-film-dismiss" onClick={close} aria-label="关闭"><X size={17} /></button>
    </div>
  </dialog>, document.body);
}

function FilmLibrary({ category, ownerId, current, close, apply }: { category: LibraryCategory; ownerId: string; current?: string; close: () => void; apply: (preset: FilmPreset) => void }) {
  const name = filmLabels[category];
  const key = 'sceneturn-film-library:' + ownerId + ':' + category;
  const [shelf, setShelf] = useState<Shelf>(() => JSON.parse(localStorage.getItem(key) ?? 'null') ?? freshShelf);
  const [tab, setTab] = useState<'mine' | 'official' | 'recent'>('official');
  const [query, setQuery] = useState('');
  const [draft, setDraft] = useState<FilmPreset | null>(null);
  const [selection, setSelection] = useState<FilmPreset | undefined>(() => [...filmPresets[category], ...shelf.mine].find((item) => item.instruction === current));
  const items = (tab === 'official' ? filmPresets[category] : shelf[tab]).filter((item) => (item.name + item.instruction).toLowerCase().includes(query.trim().toLowerCase()));
  const isText = category === 'prompt';
  const rhythm = category === 'rhythm' && tab === 'official';
  const rhythmIndex = Math.max(0, items.findIndex((item) => item === selection));
  const selected = rhythm ? items[rhythmIndex] : selection;
  const save = (next: Shelf) => { localStorage.setItem(key, JSON.stringify(next)); setShelf(next); };
  const favorite = (item: FilmPreset) => {
    const mine = shelf.mine.some((entry) => entry.name === item.name)
      ? shelf.mine.filter((entry) => entry.name !== item.name) : [item, ...shelf.mine];
    save({ ...shelf, mine });
  };
  return <FilmDialog title={name + '库'} close={close}>
    <header className="fmv-film-tabs">
      <button aria-pressed={tab === 'mine'} onClick={() => setTab('mine')}>我的{name}{isText || category === 'style' ? '库' : ''}</button>
      <button aria-pressed={tab === 'official'} onClick={() => setTab('official')}>{category === 'style' ? '影视风格库' : '官方' + name + (isText ? '库' : '')}</button>
      {(category === 'style' || isText) && <button aria-pressed={tab === 'recent'} onClick={() => setTab('recent')}>最近使用</button>}
    </header>
    <div className="fmv-film-search-row"><label><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={'搜索' + name} aria-label={'搜索' + name} /></label>
      {(tab === 'mine' || category === 'palette') && <button className="fmv-film-white" onClick={() => setDraft({ name: '', instruction: '' })}><Plus size={14} />创建{name}</button>}
    </div>
    {draft ? <form className="fmv-film-form" onSubmit={(event) => {
      event.preventDefault();
      const item = { name: draft.name.trim(), instruction: draft.instruction.trim() };
      if (!item.name || !item.instruction) return;
      save({ ...shelf, mine: [item, ...shelf.mine.filter((entry) => entry.name !== item.name)] });
      setTab('mine'); setSelection(item); setDraft(null); setQuery('');
    }}>
      <label>名称<input required maxLength={60} value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
      <label>{name}内容<textarea required maxLength={250} value={draft.instruction} onChange={(event) => setDraft({ ...draft, instruction: event.target.value })} /></label>
      <div><button type="button" onClick={() => setDraft(null)}>取消</button><button type="submit" className="fmv-film-white">保存</button></div>
    </form> : rhythm && selected ? <div className="fmv-rhythm-preview">
      <button aria-label="上一段" onClick={() => setSelection(items[(rhythmIndex + items.length - 1) % items.length])}><ChevronLeft /></button>
      <figure><video key={selected.name} src={'/sceneturn/canvas/guidance/rhythm-' + filmPresets.rhythm.indexOf(selected) + '.mp4'} muted autoPlay loop controls playsInline /><figcaption>{selected.name}</figcaption></figure>
      <button aria-label="下一段" onClick={() => setSelection(items[(rhythmIndex + 1) % items.length])}><ChevronRight /></button>
    </div> : <div className="fmv-film-library" data-text={isText}>
      {items.map((item) => <article key={item.name} data-selected={selected?.instruction === item.instruction}>
        <button type="button" className="fmv-film-item" aria-pressed={selected?.instruction === item.instruction} onClick={() => setSelection(item)}>
          {item.cover ? <img src={item.cover} alt="" /> : <span>{item.instruction}</span>}<strong>{item.name}</strong>
        </button>
        <button type="button" className="fmv-film-star" aria-label={'收藏 ' + item.name} aria-pressed={shelf.mine.some((entry) => entry.name === item.name)} onClick={() => favorite(item)}><Star size={15} /></button>
      </article>)}
      {!items.length && <p>没有符合条件的{name}</p>}
    </div>}
    {!draft && <footer><button onClick={close}>取消</button><button className="fmv-film-white" disabled={!selected} onClick={() => {
      if (!selected) return;
      save({ ...shelf, recent: [selected, ...shelf.recent.filter((entry) => entry.name !== selected.name)].slice(0, 50) });
      apply(selected);
    }}>确定</button></footer>}
  </FilmDialog>;
}

function CameraSettings({ value, close, apply }: { value: FilmGuidance; close: () => void; apply: (value: FilmGuidance) => void }) {
  const [draft, setDraft] = useState(value);
  return <FilmDialog title="相机设置" close={close}>
    <header>相机设置</header>
    <figure className="fmv-camera-preview"><img src={guidanceAsset('camera-preview')} alt="相机取景预览" /><figcaption>16:9　{draft.focal || 'Auto'}　{draft.aperture || 'Auto'}　ISO Auto　WB 5600K</figcaption></figure>
    <div className="fmv-camera-selectors">{cameraColumns.map(({ key, label, values }, column) => {
      const index = Math.max(0, values.findIndex((option) => option === draft[key]));
      const move = (direction: number) => setDraft({ ...draft, [key]: values[(index + direction + values.length) % values.length] });
      const Icon = cameraIcons[column];
      return <div key={key}>
        <button aria-label={'上一个' + label} onClick={() => move(-1)}><ChevronUp size={15} /></button>
        <label><Icon size={24} /><span>{values[index]}</span><select aria-label={label} value={values[index]} onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}>{values.map((option) => <option key={option}>{option}</option>)}</select></label>
        <button aria-label={'下一个' + label} onClick={() => move(1)}><ChevronDown size={15} /></button><small>{label}</small>
      </div>;
    })}</div>
    <footer><button className="fmv-film-white" onClick={() => apply(draft)}>确认</button></footer>
  </FilmDialog>;
}

export function CanvasFilmControls({ video, ownerId, value, change }: { video: boolean; ownerId: string; value: FilmGuidance; change: (value: FilmGuidance) => void }) {
  const [open, setOpen] = useState<Exclude<FilmCategory, 'prompt'> | null>(null);
  const categories = video ? ['style', 'palette', 'composition', 'rhythm', 'camera'] as const : ['style', 'palette', 'composition'] as const;
  function remove(category: Exclude<FilmCategory, 'prompt'>) {
    const updated = { ...value };
    if (category === 'camera') cameraColumns.forEach(({ key }) => { delete updated[key]; });
    else delete updated[category];
    change(updated);
  }
  return <><div className="fmv-film-bar">{categories.map((category) => {
    const instruction = value[category];
    const preset = category === 'camera' ? undefined : filmPresets[category].find((item) => item.instruction === instruction);
    const label = category === 'camera' ? cameraColumns.map(({ key }) => value[key]).filter((v) => v && v !== 'Auto').join(' · ') : preset?.name ?? instruction;
    return <div key={category}><button type="button" aria-label={filmLabels[category]} title={label || filmLabels[category]} onClick={() => setOpen(category)}><img src={preset?.cover ?? guidanceAsset(category)} alt="" /><span>{label || filmLabels[category]}</span></button>
      {label && <button className="fmv-film-clear" type="button" aria-label={'清除' + filmLabels[category]} onClick={() => remove(category)}><X size={11} /></button>}</div>;
  })}</div>
    {open === 'camera' ? <CameraSettings value={value} close={() => setOpen(null)} apply={(updated) => { change(updated); setOpen(null); }} />
      : open && <FilmLibrary category={open} ownerId={ownerId} current={value[open]} close={() => setOpen(null)} apply={(item) => { change({ ...value, [open]: item.instruction }); setOpen(null); }} />}
  </>;
}

export function PromptPresets({ ownerId, apply, close }: { ownerId: string; apply: (prompt: string) => void; close: () => void }) {
  return <FilmLibrary category="prompt" ownerId={ownerId} close={close} apply={(item) => { apply(item.instruction); close(); }} />;
}
