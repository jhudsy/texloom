import type { Catalog } from '../../i18n'

const es: Catalog = {
  "Couldn’t reach {{service}} (offline?).": "No se pudo conectar con {{service}} (¿sin conexión?).",
  "{{service}} refused the token: it may have expired. Connect again with a new one.": "{{service}} rechazó el token: puede que haya caducado. Vuelva a conectar con uno nuevo.",
  "{{service}} asks to slow down: trying again later.": "{{service}} pide ir más despacio: se volverá a intentar más tarde.",
  "{{service}} doesn’t have that repository, or the token can’t see it.": "{{service}} no tiene ese repositorio, o el token no puede verlo.",
  "{{service}} answered {{status}}{{why}}.": "{{service}} respondió {{status}}{{why}}.",
  "Nothing to send.": "No hay nada que enviar.",
  "{{service}} has new commits: bringing them in first.": "{{service}} tiene commits nuevos: se traen primero.",
}
export default es
