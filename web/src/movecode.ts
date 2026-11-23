// Moving a stored project to another creation code (issue #32): a project's storage counts against
// the code it was first stored with, so a lapsed code, or a project changing hands, needs this.
// The owner signs the request; the server checks the new code and its room (Replica.moveToCode).
import { dialog, h } from './dom'
import { t } from './i18n'
import { creationCode, type MoveRefusal, type Replica } from './replica'
import { bytes } from './usage'

function refusalText(no: MoveRefusal): string {
  switch (no.code) {
    case 'move-bad-code': return t('That code isn’t valid on this server, or has expired.')
    case 'move-no-room': return t('That code hasn’t room for this project: it needs {{need}}, and the code has {{free}} free.', { need: bytes(no.need ?? 0), free: bytes(no.free ?? 0) })
    case 'move-too-big': return t('This project is larger than a project may be with that code ({{max}}).', { max: bytes(no.max ?? 0) })
    case 'move-not-stored': return t('The server doesn’t keep a copy of this project, so there’s nothing to move.')
    case 'move-rejected': return t('The server refused: only the project’s owner can move it.')
    case 'move-busy': return t('Too many tries with wrong codes. Reload the page to try again.')
  }
}

/** The owner's dialog. Resolves true once the project has moved. */
export function moveCodeDialog(r: Replica): Promise<boolean> {
  return new Promise((resolve) => {
    const mine = creationCode.get(r.server)
    const input = h('input', { value: mine, placeholder: t('e.g. 3PYW-WTHZ-PRCF'), autocomplete: 'off', 'aria-label': t('Creation code') }) as HTMLInputElement
    const keep = h('input', { type: 'checkbox', checked: true }) as HTMLInputElement
    const keepRow = h('label', { class: 'check small' }, keep, t('Also use this code for new projects in this browser'))
    const same = () => !!mine && input.value.trim().toUpperCase() === mine.trim().toUpperCase()
    const sameAsMine = () => { keepRow.style.display = same() ? 'none' : '' } // (.check sets display, which `hidden` doesn't override)
    input.addEventListener('input', sameAsMine)
    sameAsMine()
    const msg = h('p', { class: 'err small' })
    const go = h('button', { class: 'primary' }, t('Move')) as HTMLButtonElement
    let moved = false
    const move = async () => {
      const code = input.value.trim()
      if (!code) return
      go.disabled = true
      msg.textContent = ''
      try {
        const no = await r.moveToCode(code)
        if (no) { msg.textContent = refusalText(no); return }
        moved = true
        if (!same() && keep.checked) creationCode.set(code, Date.now(), r.server)
        r.askUsage()
        dlg.close()
      } catch (e) {
        msg.textContent = (e as Error).message
      } finally {
        go.disabled = false
      }
    }
    go.onclick = move
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing) move() })
    const dlg = dialog('settings',
      h('h3', {}, t('Move to another creation code')),
      h('p', { class: 'muted' }, t('This project’s storage counts against the creation code it was first stored with. Move it to another code, for example when that code has lapsed, or when someone else’s plan should hold the project. Links, collaborators and history stay as they are.')),
      h('label', { class: 'field' }, t('Code'), input),
      keepRow,
      msg,
      h('div', { class: 'row end' }, h('button', { onclick: () => dlg.close() }, t('Cancel')), go),
    )
    dlg.addEventListener('close', () => resolve(moved))
    input.focus()
  })
}
