import type { Catalog } from '../../i18n'

const pt: Catalog = {
  "Couldn’t reach {{service}} (offline?).": "Não foi possível contactar {{service}} (sem ligação?).",
  "{{service}} refused the token: it may have expired. Connect again with a new one.": "{{service}} recusou o token: pode ter expirado. Ligue-se de novo com um novo.",
  "{{service}} asks to slow down: trying again later.": "{{service}} pede para abrandar: tentará de novo mais tarde.",
  "{{service}} doesn’t have that repository, or the token can’t see it.": "{{service}} não tem esse repositório, ou o token não o consegue ver.",
  "{{service}} answered {{status}}{{why}}.": "{{service}} respondeu {{status}}{{why}}.",
  "Nothing to send.": "Nada para enviar.",
  "{{service}} has new commits: bringing them in first.": "{{service}} tem commits novos: são trazidos primeiro.",
}
export default pt
