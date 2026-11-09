// Removing a project, from the start page (✕) or Settings. Two choices:
//   - from this browser only (anyone): the browser's copy goes; the server's copy,
//     collaborators and folders on disk stay;
//   - from the server too (owner): the server deletes its encrypted copy and stops storing
//     the project (the 'relay' storage mode), then this browser's copy goes. Collaborators
//     keep theirs and can still work together while online at the same time.
// Files in folders on disk are never deleted: they are the user's.
// (Later: "delete for everyone", which would also stop the server relaying the project.)

import { dialog, h } from './dom'
import { t, tn } from './i18n'

export interface RemoveInfo {
  name: string
  code: string
  owner: boolean // may remove the server's copy
  onServer: boolean // the server stores the project now
  pending?: number // changes from this browser not yet sent
  folder?: string | null // a folder this browser keeps in sync (its files stay)
}

/** Ask what to remove, then run `remove(fromServer)`; errors are shown in the dialog. */
export function removeDialog(info: RemoveInfo, remove: (fromServer: boolean) => Promise<void>) {
  let fromServer = false
  const option = (value: boolean, label: string, hint: string, disabled: string | null) =>
    h('label', { class: disabled ? 'radio disabled' : 'radio', title: disabled ?? '' },
      h('input', { type: 'radio', name: 'remove', checked: !value, disabled: !!disabled, onchange: () => { fromServer = value; render() } }),
      h('span', {}, h('b', {}, label), h('small', { class: 'muted' }, disabled ?? hint)))
  const warnings = h('div', { class: 'warnlist' })
  const status = h('p', { class: 'muted small', hidden: true })
  const go = h('button', { class: 'danger', onclick: async () => {
    go.setAttribute('disabled', ''); cancel.setAttribute('disabled', '')
    status.hidden = false
    status.textContent = fromServer ? t('Removing the server’s copy…') : t('Removing…')
    try {
      await remove(fromServer)
      dlg.close()
    } catch (e) {
      status.textContent = t('Couldn’t remove it: {{error}}', { error: (e as Error).message ?? e })
      go.removeAttribute('disabled'); cancel.removeAttribute('disabled')
    }
  } }, t('Remove'))
  const cancel = h('button', { onclick: () => dlg.close() }, t('Cancel'))

  function render() {
    const w: string[] = []
    if (info.pending) w.push(tn(info.pending, '{{n}} change from this browser hasn’t reached the server or anyone else yet and will be lost.', '{{n}} changes from this browser haven’t reached the server or anyone else yet and will be lost.'))
    if (!info.onServer && !fromServer) w.push(t('The server keeps no copy of this project: if nobody else has one, it will be gone for good.'))
    if (fromServer) w.push(t('Collaborators who have no copy of their own (only a link) won’t be able to open it any more.'))
    warnings.replaceChildren(...w.map((x) => h('p', { class: 'warn small' }, '⚠ ', x)))
    go.textContent = fromServer ? t('Remove from the server and this browser') : t('Remove from this browser')
  }

  const dlg = dialog('remove',
    h('h3', {}, t('Remove “{{name}}”', { name: info.name }), ' ', h('code', { class: 'pcode' }, info.code)),
    h('div', { class: 'radios' },
      option(false, t('From this browser only'),
        t('Deletes this browser’s copy. The server’s copy and collaborators’ copies stay: open the link again to get it back.'), null),
      option(true, t('From the server too'),
        t('Deletes the encrypted copy on the server and stops it storing this project, then removes it from this browser. Collaborators keep their copies and can still work together while online at the same time.'),
        !info.owner ? t('Only the owner can remove the server’s copy.') : !info.onServer ? t('The server keeps no copy already.') : null)),
    info.folder ? h('p', { class: 'muted small' }, t('The folder “{{folder}}” on this computer and its files stay: delete it yourself if you want.', { folder: info.folder })) : null,
    h('p', { class: 'muted small' }, t('Folders kept in sync by the TeXLoom app stay too: use Leave… there to stop syncing one.')),
    warnings, status,
    h('div', { class: 'row end' }, cancel, go))
  render()
}

export interface BulkItem extends Pick<RemoveInfo, 'name' | 'code' | 'owner' | 'onServer'> { id: string }

/** Remove several projects. The server's copy goes only for those the person owns, and only if
 *  they choose it. Anything that fails stays, and is named. */
export function bulkRemoveDialog(items: BulkItem[], remove: (item: BulkItem, fromServer: boolean) => Promise<void>, done: () => void) {
  const removable = items.filter((x) => x.owner && x.onServer)
  const lastCopy = items.filter((x) => !x.onServer).length
  let fromServer = false, busy = false
  const option = (value: boolean, label: string, hint: string, off: boolean) =>
    h('label', { class: off ? 'radio disabled' : 'radio' },
      h('input', { type: 'radio', name: 'remove', checked: !value, disabled: off, onchange: () => { fromServer = value; render() } }),
      h('span', {}, h('b', {}, label), h('small', { class: 'muted' }, hint)))
  const warnings = h('div', { class: 'warnlist' })
  const status = h('p', { class: 'muted small', role: 'status' })
  const cancel = h('button', { onclick: () => dlg.close() }, t('Cancel'))
  const go = h('button', { class: 'danger', onclick: () => run() })

  function render() {
    const w: string[] = []
    if (lastCopy && !fromServer) w.push(tn(lastCopy, '{{n}} of these has no copy on the server: if nobody else has one, it will be gone for good.', '{{n}} of these have no copy on the server: if nobody else has one, they will be gone for good.'))
    if (fromServer) w.push(t('Collaborators who have no copy of their own (only a link) won’t be able to open it any more.'))
    warnings.replaceChildren(...w.map((x) => h('p', { class: 'warn small' }, '⚠ ', x)))
    go.textContent = tn(items.length, 'Remove {{n}} project', 'Remove {{n}} projects')
  }

  async function run() {
    busy = true
    go.disabled = cancel.disabled = true
    const failed: string[] = []
    for (const [i, item] of items.entries()) {
      status.textContent = t('Removing… ({{done}} of {{total}})', { done: i + 1, total: items.length })
      try { await remove(item, fromServer && item.owner && item.onServer) }
      catch (e) { failed.push(`${item.name}: ${(e as Error).message ?? e}`) }
    }
    busy = false
    done()
    if (!failed.length) { dlg.close(); return }
    status.textContent = t('These couldn’t be removed: {{list}}', { list: failed.join('; ') })
    cancel.disabled = false
    cancel.textContent = t('Close')
    go.hidden = true
  }

  const dlg = dialog('remove',
    h('h3', {}, tn(items.length, 'Remove {{n}} project', 'Remove {{n}} projects')),
    h('ul', { class: 'namelist' }, ...items.map((x) => h('li', {}, x.name, ' ', h('code', { class: 'pcode' }, x.code)))),
    h('div', { class: 'radios' },
      option(false, t('From this browser only'), t('Deletes this browser’s copies. The server’s copies and collaborators’ copies stay: open a link again to get one back.'), false),
      option(true, t('From the server too'), tn(removable.length, 'Also deletes the encrypted copy on the server of the {{n}} you own that is stored there. Others are only removed from this browser.', 'Also deletes the encrypted copies on the server of the {{n}} you own that are stored there. Others are only removed from this browser.'), !removable.length)),
    h('p', { class: 'muted small' }, t('Folders on this computer and their files stay: delete them yourself if you want.')),
    warnings, status,
    h('div', { class: 'row end' }, cancel, go))
  dlg.addEventListener('cancel', (e) => { if (busy) e.preventDefault() })
  render()
}
