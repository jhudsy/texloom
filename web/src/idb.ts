// IndexedDB, as promises: opening a database (making its stores the first time) and a request's result.

/** A request's result, or its error. */
export const req = <T>(q: IDBRequest<T>): Promise<T> => new Promise<T>((resolve, reject) => {
  q.onsuccess = () => resolve(q.result)
  q.onerror = () => reject(q.error)
})

/** A new connection to the database `name`; `upgrade` makes its stores when it's new (or older than `version`). */
export function connect(name: string, version: number, upgrade: (db: IDBDatabase) => void): Promise<IDBDatabase> {
  const q = indexedDB.open(name, version)
  q.onupgradeneeded = () => upgrade(q.result)
  return req(q)
}

const open = new Map<string, Promise<IDBDatabase>>()
/** One connection to `name` for the page's life, shared by whoever asks (tried again after a failure). */
export function openDb(name: string, version: number, upgrade: (db: IDBDatabase) => void): Promise<IDBDatabase> {
  let db = open.get(name)
  if (!db) {
    db = connect(name, version, upgrade)
    open.set(name, db)
    db.catch(() => { if (open.get(name) === db) open.delete(name) }) // (callers hear the error)
  }
  return db
}
