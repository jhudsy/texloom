// Checks web/src/xr.ts against scripts/xr-vectors.json, the cases that scripts/check_xr.py checks
// the Python against: which documents a compile builds first for \externaldocument.
import fs from 'node:fs'
import path from 'node:path'
import { load, report, web } from './lib.mjs'

const [{ compileOrder, externalDocuments }] = await load('src/xr.ts')
const cases = JSON.parse(fs.readFileSync(path.join(web, '../scripts/xr-vectors.json'), 'utf8'))
const bad = []
for (const c of cases) {
  const files = new Map(Object.entries(c.files))
  const order = JSON.stringify(compileOrder(files, c.main))
  if (order !== JSON.stringify(c.order)) bad.push(`${c.what}: order ${order}, want ${JSON.stringify(c.order)}`)
  for (const [doc, want] of Object.entries(c.externals)) {
    const got = JSON.stringify(externalDocuments(files, doc).map((e) => [e.aux, e.tex]))
    if (got !== JSON.stringify(want)) bad.push(`${c.what}: ${doc} reads ${got}, want ${JSON.stringify(want)}`)
  }
}
report('xr', bad, 'xr: all cases agree')
