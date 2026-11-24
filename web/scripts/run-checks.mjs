// Runs every scripts/check-*.mjs, a few at once, printing each one's output whole under its name as
// it ends; the last line names any that failed.
//
//     npm run checks          (one alone: npm run check-props, say)
import { spawn } from 'node:child_process'
import { readdirSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const checks = readdirSync(here).filter((f) => /^check-.+\.mjs$/.test(f)).sort()

const run = (file) => new Promise((resolve) => {
  const p = spawn(process.execPath, [path.join(here, file)], { cwd: path.dirname(here), stdio: ['ignore', 'pipe', 'pipe'] })
  let out = ''
  p.stdout.on('data', (d) => { out += d })
  p.stderr.on('data', (d) => { out += d })
  p.on('close', (code, signal) => resolve({ ok: code === 0, why: signal ?? `exit ${code}`, out }))
})

const failed = []
let next = 0
await Promise.all(Array.from({ length: Math.min(checks.length, os.availableParallelism()) }, async () => {
  while (next < checks.length) {
    const file = checks[next++], name = file.replace(/\.mjs$/, '')
    const { ok, why, out } = await run(file)
    console.log(`\n== ${name}${ok ? '' : `: FAILED (${why})`}\n${out.trimEnd()}`)
    if (!ok) failed.push(name)
  }
}))
console.log(failed.length ? `\n${failed.length} of ${checks.length} checks FAILED: ${failed.join(', ')}` : `\nall ${checks.length} checks passed`)
process.exit(failed.length ? 1 : 0)
