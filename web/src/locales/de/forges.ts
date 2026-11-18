import type { Catalog } from '../../i18n'

const de: Catalog = {
  "Couldn’t reach {{service}} (offline?).": "{{service}} ist nicht erreichbar (offline?).",
  "{{service}} refused the token: it may have expired. Connect again with a new one.": "{{service}} hat das Token abgelehnt: Es ist vielleicht abgelaufen. Verbinden Sie sich mit einem neuen erneut.",
  "{{service}} asks to slow down: trying again later.": "{{service}} bittet um eine Pause: Es wird später erneut versucht.",
  "{{service}} doesn’t have that repository, or the token can’t see it.": "{{service}} kennt dieses Repository nicht, oder das Token kann es nicht sehen.",
  "{{service}} answered {{status}}{{why}}.": "{{service}} antwortete {{status}}{{why}}.",
  "Nothing to send.": "Nichts zu senden.",
  "{{service}} has new commits: bringing them in first.": "{{service}} hat neue Commits: Sie werden zuerst übernommen.",
}
export default de
