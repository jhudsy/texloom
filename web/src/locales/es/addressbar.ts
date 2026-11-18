import type { Catalog } from '../../i18n'

const es: Catalog = {
  'This address can’t open the project': 'Esta dirección no puede abrir el proyecto',
  'For safety, the address bar doesn’t contain a project’s keys, so a copied address only works in the browser that opened the project.': 'Por seguridad, la barra de direcciones no contiene las claves del proyecto, así que una dirección copiada solo funciona en el navegador que abrió el proyecto.',
  'Ask whoever sent it to use Share and send you a link. If this is your own project, open it from your project list or from its link.': 'Pide a quien te la envió que use Compartir y te mande un enlace. Si el proyecto es tuyo, ábrelo desde tu lista de proyectos o desde su enlace.',
  'OK': 'Aceptar',
  'This link names a server that can’t be used: {{server}}': 'Este enlace indica un servidor que no se puede usar: {{server}}',
  "This link opens “{{name}}” on {{server}}, but your project list has it on {{stored}}. A link naming another server could come from someone who runs that server: only open it there if you trust it.\n\nOK: open it on {{stored}}, as before.\nCancel: open it on {{server}}, in this tab only.": "Este enlace abre «{{name}}» en {{server}}, pero tu lista de proyectos lo tiene en {{stored}}. Un enlace que nombra otro servidor podría venir de alguien que gestiona ese servidor: ábrelo allí solo si confías en él.\n\nAceptar: abrirlo en {{stored}}, como antes.\nCancelar: abrirlo en {{server}}, solo en esta pestaña.",
}

export default es
