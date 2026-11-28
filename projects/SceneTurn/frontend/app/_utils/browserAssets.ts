const DB_NAME = 'sceneturn-browser-assets';
const STORE_NAME = 'assets';
const IDB_PREFIX = 'sceneturn-idb://';
const OPFS_PREFIX = 'sceneturn-opfs://';
const LEGACY_DB_NAME = 'openfmv-browser-assets';
const LEGACY_IDB_PREFIX = 'openfmv-idb://';
const LEGACY_OPFS_PREFIX = 'openfmv-opfs://';
const OPFS_DIRECTORY = 'assets';

interface StoredBrowserAsset {
  id: string;
  blob: Blob;
  name: string;
  type: string;
  importedAt: string;
}

const objectUrlCache = new Map<string, string>();
const getBrowserAssetPrefix = (value: string) =>
  [IDB_PREFIX, OPFS_PREFIX, LEGACY_IDB_PREFIX, LEGACY_OPFS_PREFIX].find((prefix) => value.startsWith(prefix));

export const isBrowserAssetRef = (value?: string | null) => {
  return typeof value === 'string' && Boolean(getBrowserAssetPrefix(value));
};

const openBrowserAssetDb = (databaseName: string) => {
  return new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(databaseName, 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: 'id' });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('Unable to open browser asset database'));
  });
};

const runBrowserAssetStore = async <T>(mode: IDBTransactionMode, runner: (store: IDBObjectStore) => IDBRequest<T>, databaseName = DB_NAME) => {
  const db = await openBrowserAssetDb(databaseName);
  return new Promise<T>((resolve, reject) => {
    const transaction = db.transaction(STORE_NAME, mode);
    const request = runner(transaction.objectStore(STORE_NAME));
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('Browser asset database operation failed'));
    transaction.oncomplete = () => db.close();
    transaction.onerror = () => {
      db.close();
      reject(transaction.error ?? new Error('Browser asset database operation failed'));
    };
  });
};

export const saveBrowserAssetFile = async (file: File) => {
  const id = crypto.randomUUID();
  const opfsRef = `${OPFS_PREFIX}${id}`;
  const opfsSaved = await saveOpfsAssetFile(id, file).catch(() => false);
  if (opfsSaved) {
    objectUrlCache.set(opfsRef, URL.createObjectURL(file));
    return opfsRef;
  }

  const record: StoredBrowserAsset = {
    id,
    blob: file,
    name: file.name,
    type: file.type,
    importedAt: new Date().toISOString(),
  };
  await runBrowserAssetStore('readwrite', (store) => store.put(record));
  const ref = `${IDB_PREFIX}${id}`;
  objectUrlCache.set(ref, URL.createObjectURL(file));
  return ref;
};

const getOpfsAssetDirectory = async () => {
  const storage = navigator.storage as StorageManager & { getDirectory?: () => Promise<FileSystemDirectoryHandle> };
  const root = await storage.getDirectory?.();
  if (!root) return null;
  return root.getDirectoryHandle(OPFS_DIRECTORY, { create: true });
};

const saveOpfsAssetFile = async (id: string, file: File) => {
  const directory = await getOpfsAssetDirectory();
  if (!directory) return false;
  const handle = await directory.getFileHandle(id, { create: true });
  const writable = await handle.createWritable();
  await writable.write(file);
  await writable.close();
  return true;
};

const resolveOpfsAssetRef = async (id: string) => {
  const directory = await getOpfsAssetDirectory();
  if (!directory) return undefined;
  const handle = await directory.getFileHandle(id);
  return URL.createObjectURL(await handle.getFile());
};

export const resolveBrowserAssetRef = async (ref: string) => {
  const cached = objectUrlCache.get(ref);
  if (cached) return cached;
  const prefix = getBrowserAssetPrefix(ref);
  if (!prefix) return undefined;
  const id = ref.slice(prefix.length);
  if (prefix === OPFS_PREFIX || prefix === LEGACY_OPFS_PREFIX) {
    const objectUrl = await resolveOpfsAssetRef(id).catch(() => undefined);
    if (objectUrl) objectUrlCache.set(ref, objectUrl);
    return objectUrl;
  }
  const databaseName = prefix === LEGACY_IDB_PREFIX ? LEGACY_DB_NAME : DB_NAME;
  const record = await runBrowserAssetStore<StoredBrowserAsset | undefined>('readonly', (store) => store.get(id), databaseName);
  if (!record?.blob) return undefined;
  const objectUrl = URL.createObjectURL(record.blob);
  objectUrlCache.set(ref, objectUrl);
  return objectUrl;
};

export const getCachedBrowserAssetUrl = (ref: string) => objectUrlCache.get(ref);
