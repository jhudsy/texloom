// Tools the assistant can call. They act on the shared Yjs document, so edits appear
// live for collaborators and merge with concurrent typing. Every input is validated
// with zod before running: inputs stream unbuffered (eager input streaming) and from
// arbitrary local models, so they can't be trusted to match the schema.
import * as Y from 'yjs'
import { z } from 'zod'
import type { Replica } from '../replica'

export const ASSISTANT = Symbol('assistant') // Yjs transaction origin for assistant edits

export interface Change { path: string; before: string | null; after: string | null; summary: string }

export interface ToolCtx {
  replica: Replica
  writable: boolean
  review: (c: Change) => Promise<boolean> // resolves true to apply
  compile: () => Promise<{ ok: boolean; log: string } | null>
  lastLog: () => string
}

export interface ToolDef {
  name: string
  description: string
  schema: z.ZodObject<any>
  write?: boolean // modifies the project
  run: (input: any, ctx: ToolCtx) => Promise<string>
}

class ToolError extends Error {}

const textOf = (r: Replica, path: string): Y.Text => {
  const t = r.files.get(path)
  if (t) return t
  if (r.bin.has(path)) throw new ToolError(`${path} is a binary file (image, PDF, …) and can't be read as text.`)
  throw new ToolError(`No file named ${path}. Use list_files to see what exists.`)
}

const validPath = (p: string) => {
  const clean = p.replace(/^\.?\//, '').trim()
  if (!clean || clean.split('/').some((s) => s === '..' || s === '') || clean.startsWith('.texloom'))
    throw new ToolError(`Invalid path: ${p}`)
  return clean
}

function occurrences(hay: string, needle: string): number[] {
  const out: number[] = []
  for (let i = hay.indexOf(needle); i >= 0 && needle; i = hay.indexOf(needle, i + needle.length)) out.push(i)
  return out
}

/** Replace `old` with `next` in a Y.Text using one small edit (common prefix/suffix trimmed). */
function replaceText(yt: Y.Text, next: string) {
  const old = yt.toString()
  if (old === next) return
  let p = 0
  const n = Math.min(old.length, next.length)
  while (p < n && old[p] === next[p]) p++
  let s = 0
  while (s < n - p && old[old.length - 1 - s] === next[next.length - 1 - s]) s++
  if (old.length - s - p > 0) yt.delete(p, old.length - s - p)
  if (next.length - s - p > 0) yt.insert(p, next.slice(p, next.length - s))
}

/** Pull the useful part out of a LaTeX log: errors with line numbers, then warnings. */
export function summarizeLog(log: string): string {
  const lines = log.split('\n')
  const errors: string[] = []
  const warnings: string[] = []
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i]
    if (/^\S+\.(tex|sty|cls|bib):\d+: /.test(l) || l.startsWith('! ')) {
      const ctx = lines.slice(i, i + 4).filter((x) => x.trim()).join('\n')
      if (!errors.includes(ctx)) errors.push(ctx)
    } else if (/(LaTeX|Package|Class) .*Warning|^Overfull|^Underfull|undefined (references|citations)|Citation .* undefined|Reference .* undefined/.test(l)) {
      if (!warnings.includes(l.trim())) warnings.push(l.trim())
    }
  }
  const out: string[] = []
  if (errors.length) out.push(`${errors.length} error(s):\n` + errors.slice(0, 15).join('\n---\n'))
  if (warnings.length) out.push(`${warnings.length} warning(s):\n` + warnings.slice(0, 25).join('\n'))
  return out.join('\n\n') || 'No errors or warnings found in the log.'
}

export const TOOLS: ToolDef[] = [
  {
    name: 'list_files',
    description: 'List every file in the project with its type and size, and which file is the main document.',
    schema: z.object({}),
    run: async (_i, { replica: r }) => {
      const main = r.meta.get('main')
      const rows: string[] = []
      r.files.forEach((t, p) => rows.push(`${p}\ttext, ${t.length} chars${p === main ? '\t[main file]' : ''}`))
      r.bin.forEach((b, p) => rows.push(`${p}\tbinary, ${(b.size / 1024).toFixed(1)} KB`))
      return rows.sort().join('\n') || '(the project is empty)'
    },
  },
  {
    name: 'read_file',
    description: 'Read a text file. Returns lines prefixed with their line number and a tab; the numbers are not part of the file. Use offset/limit for long files.',
    schema: z.object({
      path: z.string().describe('Project-relative path, e.g. sections/intro.tex'),
      offset: z.number().int().min(1).optional().describe('First line to return (1-based)'),
      limit: z.number().int().min(1).max(5000).optional().describe('Maximum number of lines (default 2000)'),
    }),
    run: async ({ path, offset = 1, limit = 2000 }, { replica: r }) => {
      const lines = textOf(r, validPath(path)).toString().split('\n')
      const slice = lines.slice(offset - 1, offset - 1 + limit)
      const body = slice.map((l, i) => `${String(offset + i).padStart(5)}\t${l}`).join('\n')
      const more = offset - 1 + limit < lines.length ? `\n… (${lines.length} lines total; continue with offset=${offset + limit})` : ''
      return body + more
    },
  },
  {
    name: 'search',
    description: 'Search all text files (or one file) for a string or regular expression. Returns path:line: text for each match (max 100).',
    schema: z.object({
      pattern: z.string().min(1),
      regex: z.boolean().optional().describe('Treat pattern as a JavaScript regular expression'),
      path: z.string().optional().describe('Limit the search to this file'),
    }),
    run: async ({ pattern, regex, path }, { replica: r }) => {
      let re: RegExp | null = null
      if (regex) { try { re = new RegExp(pattern) } catch (e) { throw new ToolError(`Bad regex: ${e}`) } }
      const hits: string[] = []
      const paths = path ? [validPath(path)] : [...r.files.keys()].sort()
      for (const p of paths) {
        textOf(r, p).toString().split('\n').forEach((l, i) => {
          if (hits.length < 100 && (re ? re.test(l) : l.includes(pattern))) hits.push(`${p}:${i + 1}: ${l}`)
        })
      }
      return hits.length ? hits.join('\n') + (hits.length === 100 ? '\n… (stopped at 100 matches)' : '') : 'No matches.'
    },
  },
  {
    name: 'edit_file',
    description: 'Replace an exact string in a text file. old_string must match the file exactly (including whitespace and line breaks, without line-number prefixes) and be unique unless replace_all is true. Prefer small, targeted edits; include a little surrounding context to make old_string unique.',
    write: true,
    schema: z.object({
      path: z.string(),
      old_string: z.string().min(1),
      new_string: z.string(),
      replace_all: z.boolean().optional(),
    }),
    run: async ({ path, old_string, new_string, replace_all }, ctx) => {
      const p = validPath(path)
      const before = textOf(ctx.replica, p).toString()
      const hits = occurrences(before, old_string)
      if (!hits.length) throw new ToolError(`old_string not found in ${p}. Re-read the file; it may have changed.`)
      if (hits.length > 1 && !replace_all) throw new ToolError(`old_string occurs ${hits.length} times in ${p}. Add surrounding context to make it unique, or set replace_all.`)
      const after = replace_all ? before.split(old_string).join(new_string) : before.slice(0, hits[0]) + new_string + before.slice(hits[0] + old_string.length)
      if (!(await ctx.review({ path: p, before, after, summary: `Edit ${p}` }))) return 'The user rejected this edit. Ask what they would like instead, or try a different approach.'
      // A collaborator may have typed while the user was reviewing: re-locate the text now.
      const yt = textOf(ctx.replica, p)
      const now = occurrences(yt.toString(), old_string)
      if (!now.length || (now.length > 1 && !replace_all)) throw new ToolError(`${p} changed while waiting for approval; re-read it and try again.`)
      ctx.replica.doc.transact(() => {
        for (const i of (replace_all ? now : [now[0]]).reverse()) {
          yt.delete(i, old_string.length)
          if (new_string) yt.insert(i, new_string)
        }
      }, ASSISTANT)
      return `Edited ${p} (${replace_all ? now.length : 1} replacement${replace_all && now.length > 1 ? 's' : ''}).`
    },
  },
  {
    name: 'write_file',
    description: 'Create a new text file, or replace the entire contents of an existing one. Prefer edit_file for changes to existing files.',
    write: true,
    schema: z.object({ path: z.string(), content: z.string() }),
    run: async ({ path, content }, ctx) => {
      const p = validPath(path)
      const r = ctx.replica
      if (r.bin.has(p)) throw new ToolError(`${p} is a binary file; choose another name.`)
      const before = r.files.get(p)?.toString() ?? null
      if (!(await ctx.review({ path: p, before, after: content, summary: before === null ? `Create ${p}` : `Rewrite ${p}` })))
        return 'The user rejected this change.'
      r.doc.transact(() => {
        const yt = r.files.get(p)
        if (yt) replaceText(yt, content)
        else r.files.set(p, new Y.Text(content))
      }, ASSISTANT)
      return before === null ? `Created ${p}.` : `Rewrote ${p}.`
    },
  },
  {
    name: 'delete_file',
    description: 'Delete a file from the project (it remains in version history).',
    write: true,
    schema: z.object({ path: z.string() }),
    run: async ({ path }, ctx) => {
      const p = validPath(path)
      const r = ctx.replica
      if (!r.files.has(p) && !r.bin.has(p)) throw new ToolError(`No file named ${p}.`)
      const before = r.files.get(p)?.toString() ?? '(binary file)'
      if (!(await ctx.review({ path: p, before, after: null, summary: `Delete ${p}` }))) return 'The user rejected the deletion.'
      r.doc.transact(() => { r.files.delete(p); r.bin.delete(p) }, ASSISTANT)
      return `Deleted ${p}.`
    },
  },
  {
    name: 'compile',
    description: 'Compile the project now (using this user’s compile setting) and return the errors and warnings from the log. Use after edits to check they work.',
    schema: z.object({}),
    run: async (_i, ctx) => {
      const res = await ctx.compile()
      if (!res) throw new ToolError('Compiling is turned off on this device (Settings → Compiling).')
      return `${res.ok ? 'Compiled successfully.' : 'Compilation FAILED.'}\n\n${summarizeLog(res.log)}`
    },
  },
  {
    name: 'get_compile_log',
    description: 'Get the most recent compile log: by default a summary of errors and warnings; with full=true, the last 300 lines.',
    schema: z.object({ full: z.boolean().optional() }),
    run: async ({ full }, ctx) => {
      const log = ctx.lastLog()
      if (!log) return 'There is no compile log yet. Call compile first.'
      return full ? log.split('\n').slice(-300).join('\n') : summarizeLog(log)
    },
  },
]

export function toolsFor(writable: boolean): ToolDef[] {
  return TOOLS.filter((t) => writable || !t.write)
}

export function jsonSchema(t: ToolDef): Record<string, unknown> {
  const s = z.toJSONSchema(t.schema) as Record<string, unknown>
  delete s.$schema
  return s
}

/** Validate and run one tool call. Never throws: errors become error results for the model. */
export async function runTool(defs: ToolDef[], name: string, input: unknown, ctx: ToolCtx): Promise<{ content: string; isError: boolean }> {
  const def = defs.find((t) => t.name === name)
  if (!def) return { content: `Unknown tool: ${name}`, isError: true }
  const parsed = def.schema.safeParse(input ?? {})
  if (!parsed.success) {
    return { content: `INVALID_INPUT for ${name}: ${parsed.error.issues.map((i) => `${i.path.join('.') || 'input'}: ${i.message}`).join('; ')}. Raw input: ${JSON.stringify(input).slice(0, 500)}`, isError: true }
  }
  try {
    return { content: await def.run(parsed.data, ctx), isError: false }
  } catch (e) {
    return { content: e instanceof ToolError ? e.message : `Tool failed: ${e}`, isError: true }
  }
}
