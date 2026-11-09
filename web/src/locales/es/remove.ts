import type { Catalog } from '../../i18n'

const es: Catalog = {
  'Removing the server’s copy…': 'Eliminando la copia del servidor…',
  'Removing…': 'Quitando…',
  'Couldn’t remove it: {{error}}': 'No se pudo quitar: {{error}}',
  'Collaborators who have no copy of their own (only a link) won’t be able to open it any more.': 'Los colaboradores que no tengan copia propia (solo un enlace) ya no podrán abrirlo.',
  'Remove from the server and this browser': 'Quitar del servidor y de este navegador',
  'Remove “{{name}}”': 'Quitar «{{name}}»',
  'From this browser only': 'Solo de este navegador',
  'Deletes this browser’s copy. The server’s copy and collaborators’ copies stay: open the link again to get it back.': 'Elimina la copia de este navegador. La copia del servidor y las de los colaboradores se conservan: vuelve a abrir el enlace para recuperarlo.',
  'From the server too': 'También del servidor',
  'Deletes the encrypted copy on the server and stops it storing this project, then removes it from this browser. Collaborators keep their copies and can still work together while online at the same time.': 'Elimina la copia cifrada del servidor, hace que deje de guardar este proyecto y lo quita de este navegador. Los colaboradores conservan sus copias y pueden seguir trabajando juntos mientras estén conectados a la vez.',
  'Only the owner can remove the server’s copy.': 'Solo el propietario puede eliminar la copia del servidor.',
  'The server keeps no copy already.': 'El servidor ya no guarda ninguna copia.',
  'The folder “{{folder}}” on this computer and its files stay: delete it yourself if you want.': 'La carpeta «{{folder}}» de este ordenador y sus archivos se conservan: elimínala tú si quieres.',
  'Folders kept in sync by the TeXLoom app stay too: use Leave… there to stop syncing one.': 'Las carpetas sincronizadas por la aplicación TeXLoom también se conservan: usa Abandonar… allí para dejar de sincronizar una.',
  'Deletes this browser’s copies. The server’s copies and collaborators’ copies stay: open a link again to get one back.': 'Elimina las copias de este navegador. Las copias del servidor y las de los colaboradores se conservan: vuelve a abrir un enlace para recuperar una.',
  'Folders on this computer and their files stay: delete them yourself if you want.': 'Las carpetas de este ordenador y sus archivos se conservan: elimínalas tú si quieres.',
  'Removing… ({{done}} of {{total}})': 'Quitando… ({{done}} de {{total}})',
  'These couldn’t be removed: {{list}}': 'No se han podido quitar: {{list}}',
  'Cancel': 'Cancelar',
  'Close': 'Cerrar',
  '{{n}} of these have no copy on the server: if nobody else has one, they will be gone for good.': ['{{n}} de ellos no tiene copia en el servidor: si nadie más la tiene, se perderá para siempre.', '{{n}} de ellos no tienen copia en el servidor: si nadie más la tiene, se perderán para siempre.'],
  'Remove {{n}} projects': ['Quitar {{n}} proyecto', 'Quitar {{n}} proyectos'],
  'Also deletes the encrypted copies on the server of the {{n}} you own that are stored there. Others are only removed from this browser.': ['Elimina también la copia cifrada del servidor del proyecto tuyo que está guardado allí. Los demás solo se quitan de este navegador.', 'Elimina también las copias cifradas del servidor de los {{n}} proyectos tuyos que están guardados allí. Los demás solo se quitan de este navegador.'],
}

export default es
