import type { Catalog } from '../../i18n'

const fr: Catalog = {
  "Couldn’t reach {{service}} (offline?).": "Impossible de joindre {{service}} (hors ligne ?).",
  "{{service}} refused the token: it may have expired. Connect again with a new one.": "{{service}} a refusé le jeton : il a peut-être expiré. Reconnectez-vous avec un nouveau.",
  "{{service}} asks to slow down: trying again later.": "{{service}} demande de ralentir : nouvel essai plus tard.",
  "{{service}} doesn’t have that repository, or the token can’t see it.": "{{service}} n’a pas ce dépôt, ou le jeton ne peut pas le voir.",
  "{{service}} answered {{status}}{{why}}.": "{{service}} a répondu {{status}}{{why}}.",
  "Nothing to send.": "Rien à envoyer.",
  "{{service}} has new commits: bringing them in first.": "{{service}} a de nouveaux commits : ils sont d’abord récupérés.",
}
export default fr
