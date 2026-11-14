// Past assistant conversations, kept only in this browser and per project. They can quote the
// documents, so they go when the project is removed from this browser. A conversation's list entry
// and its contents are stored apart, because the contents can be large and the list reads every entry.
export type Entry =
  | { k: 'user' | 'text' | 'think'; text: string }
  | { k: 'tool'; text: string; ok: boolean; err?: string }
  | { k: 'change'; text: string; ok: boolean }
  | { k: 'note'; cls: string; text: string }

export interface ChatMeta { id: string; project: string; title: string; provider: string; model: string; at: number }
export interface ChatBody { id: string; entries: Entry[]; state: unknown }

const DB = 'texloom-assistant', MAX_PER_PROJECT = 50

let opened: Promise<IDBDatabase> | null = null
function open(): Promise<IDBDatabase> {
  return opened ??= new Promise<IDBDatabase>((resolve, reject) => {
    const q = indexedDB.open(DB, 1)
    q.onupgradeneeded = () => {
      q.result.createObjectStore('meta', { keyPath: 'id' }).createIndex('project', 'project')
      q.result.createObjectStore('body', { keyPath: 'id' })
    }
    q.onsuccess = () => resolve(q.result)
    q.onerror = () => { opened = null; reject(q.error) }
  })
}
const done = (tx: IDBTransaction) => new Promise<void>((resolve, reject) => {
  tx.oncomplete = () => resolve()
  tx.onerror = tx.onabort = () => reject(tx.error)
})
const result = <T>(q: IDBRequest<T>) => new Promise<T>((resolve, reject) => {
  q.onsuccess = () => resolve(q.result)
  q.onerror = () => reject(q.error)
})
function drop(tx: IDBTransaction, id: string) {
  tx.objectStore('meta').delete(id)
  tx.objectStore('body').delete(id)
}

export const chats = {
  /** Newest first. */
  async list(project: string): Promise<ChatMeta[]> {
    const db = await open()
    const all = await result(db.transaction('meta').objectStore('meta').index('project').getAll(project)) as ChatMeta[]
    return all.sort((a, b) => b.at - a.at)
  },

  async body(id: string): Promise<ChatBody | undefined> {
    const db = await open()
    return result(db.transaction('body').objectStore('body').get(id)) as Promise<ChatBody | undefined>
  },

  async save(meta: ChatMeta, body: ChatBody) {
    const db = await open()
    const tx = db.transaction(['meta', 'body'], 'readwrite')
    tx.objectStore('meta').put(meta)
    tx.objectStore('body').put(body)
    await done(tx)
    const old = (await chats.list(meta.project)).slice(MAX_PER_PROJECT)
    if (!old.length) return
    const trim = db.transaction(['meta', 'body'], 'readwrite')
    for (const m of old) drop(trim, m.id)
    await done(trim)
  },

  async remove(id: string) {
    const db = await open()
    const tx = db.transaction(['meta', 'body'], 'readwrite')
    drop(tx, id)
    await done(tx)
  },

  /** How many are kept, in every project. */
  async count(): Promise<number> {
    const db = await open()
    return result(db.transaction('meta').objectStore('meta').count())
  },

  async forgetAll() {
    const db = await open()
    const tx = db.transaction(['meta', 'body'], 'readwrite')
    tx.objectStore('meta').clear()
    tx.objectStore('body').clear()
    await done(tx)
  },

  async forgetProject(project: string) {
    const db = await open()
    const ids = await result(db.transaction('meta').objectStore('meta').index('project').getAllKeys(project))
    const tx = db.transaction(['meta', 'body'], 'readwrite')
    for (const id of ids) drop(tx, id as string)
    await done(tx)
  },
}
