import { atom, read, update } from 'claude-code'
import type { EngineInterface, ProcessRunResult, Register } from 'claude-code'

import type { DeepcrewAsk, DeepcrewCrew } from '../types'

const PANE = 'deepcrew'
const POLL_MS = 3000
const RUN_MS = 30000
const IDLE_POLLS = 5
const SHOWN = 160
const crew = atom({ plugin: 'deepcrew', key: 'crew' } as const, null)
const told = atom({ plugin: 'deepcrew', key: 'told' } as const, [])

type Request = {
  id: string
  member?: string
  tool?: string
  input?: Record<string, unknown>
  review?: string
  folder?: string
  widening?: { text?: string } | null
}
type Question = { id: string; member?: string; question?: string; options?: string[] }
type Status = {
  members?: { name: string; state: string; task: string | null }[]
  usage?: Record<string, { cost_usd?: number }>
  tasks?: { state: string }[]
  permission_requests?: Request[]
  questions?: Question[]
  budget?: { usd?: number | null; spent?: number | null }
  paused?: unknown
  page_url?: string | null
}
type Answer = { id: string; allow?: boolean; always?: boolean; option?: number }
type Site = Parameters<EngineInterface['ui']['resolve']>[0]

function cut(text: unknown): string {
  const one = String(text ?? '').replace(/\s+/g, ' ').trim()
  return one.length > SHOWN ? `${one.slice(0, SHOWN - 3)}...` : one
}

function asked(r: Request): DeepcrewAsk {
  const input = r.input ?? {}
  const tool = r.tool ?? ''
  const text = r.folder
    ? `wants to reach ${cut(r.folder)}`
    : tool === 'Bash'
      ? `wants to run ${cut(input.command)}`
      : tool === 'WebFetch'
        ? `wants to fetch ${cut(input.url)}`
        : typeof input.file_path === 'string'
          ? `wants to change ${cut(input.file_path)}`
          : `wants to use ${tool}`
  const detail = r.review ? `reviewer: ${cut(r.review)}` : cut(input.description)
  const widening = r.widening?.text ? cut(r.widening.text) : null
  return { id: r.id, member: r.member ?? '', kind: 'perm', text, detail, options: [], widening }
}

function question(q: Question): DeepcrewAsk {
  const options = (q.options ?? []).map(cut)
  return { id: q.id, member: q.member ?? '', kind: 'question', text: `asks: ${cut(q.question)}`, detail: '', options, widening: null }
}

function crewOf(s: Status, team: string): DeepcrewCrew {
  const tasks = s.tasks ?? []
  return {
    team,
    page: s.page_url ?? null,
    members: (s.members ?? []).map(m => ({ name: m.name, state: m.state, task: m.task, cost: s.usage?.[m.name]?.cost_usd ?? null })),
    done: tasks.filter(t => t.state === 'done').length,
    tasks: tasks.filter(t => t.state !== 'cancelled').length,
    asks: [...(s.permission_requests ?? []).map(asked), ...(s.questions ?? []).map(question)],
    spent: s.budget?.spent ?? null,
    budget: s.budget?.usd ?? null,
    resting: Boolean(s.paused),
  }
}

function money(usd: number): string {
  return `$${usd.toFixed(2)}`
}

function facts(c: DeepcrewCrew): string {
  const working = c.members.filter(m => m.state === 'active' || m.state === 'starting').length
  const parts = [c.resting ? 'the crew rests' : `${working} of ${c.members.length} members working`, `${c.done} of ${c.tasks} tasks done`]
  if (c.spent !== null) {
    parts.push(c.budget ? `${money(c.spent)} of ${money(c.budget)} spent` : `${money(c.spent)} spent`)
  }
  if (c.asks.length) {
    parts.push(`${c.asks.length} waiting for an answer`)
  }
  return parts.join(', ')
}

function summary(c: DeepcrewCrew): string {
  return `team ${c.team}: ${facts(c)}`
}

function refusal(text: string): string {
  try {
    return String((JSON.parse(text) as { error?: string }).error ?? text)
  } catch {
    return text
  }
}

type Settings = { command: string; team: string }

let busy = false
let quiet = 0
let found: string | null = null

async function deepcrew($: EngineInterface, cfg: Settings, args: string[]): Promise<ProcessRunResult | null> {
  const home = await $.env.get('HOME')
  const local = home && !cfg.command.includes('/') ? [`${home}/.local/bin/${cfg.command}`] : []
  for (const exe of new Set([...(found ? [found] : []), cfg.command, ...local])) {
    try {
      const out = await $.process.run([exe, ...args], { timeoutMs: RUN_MS })
      found = exe
      return out
    } catch {
      continue
    }
  }
  found = null
  return null
}

async function look($: EngineInterface, cfg: Settings): Promise<DeepcrewCrew | null> {
  const out = await deepcrew($, cfg, ['status', '--json', '--team', cfg.team])
  if (!out || out.exitCode !== 0) {
    return null
  }
  try {
    return crewOf(JSON.parse(out.stdout) as Status, cfg.team)
  } catch {
    return null
  }
}

async function refresh($: EngineInterface, cfg: Settings): Promise<DeepcrewCrew | null> {
  const now = await look($, cfg)
  quiet = now ? 0 : IDLE_POLLS
  const before = await read($, told)
  const fresh = now ? now.asks.filter(a => !before.includes(a.id)) : []
  await update($, crew, () => now)
  await update($, told, () => (now ? now.asks.map(a => a.id) : []))
  $.ui.status(now ? summary(now) : undefined)
  for (const a of fresh.slice(0, 3)) {
    $.ui.toast(`DeepCrew: ${a.member} ${a.text}`)
  }
  return now
}

function poll($: EngineInterface, cfg: Settings): void {
  if (busy) {
    return
  }
  if (quiet > 0) {
    quiet -= 1
    return
  }
  busy = true
  void refresh($, cfg).finally(() => {
    busy = false
  })
}

async function pageOf($: EngineInterface, cfg: Settings): Promise<string | null> {
  const c = await read($, crew)
  if (c?.page) {
    return c.page
  }
  const out = await deepcrew($, cfg, ['ui', '--no-wait', '--json', '--team', cfg.team])
  try {
    return out?.exitCode === 0 ? ((JSON.parse(out.stdout) as { url?: string }).url ?? null) : null
  } catch {
    return null
  }
}

async function post($: EngineInterface, cfg: Settings, path: string, body: Answer | { action: string }): Promise<string | null> {
  const address = await pageOf($, cfg)
  if (!address) {
    return `the team page did not start: answer with \`${cfg.command}\` in the chat`
  }
  const url = new URL(address)
  try {
    const res = await $.http.fetch(`${url.origin}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Deepcrew-Token': url.searchParams.get('t') ?? '' },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      return refusal(res.text)
    }
  } catch (err) {
    return `the team page did not answer (${String(err)})`
  }
  await refresh($, cfg)
  return null
}

async function answer($: EngineInterface, cfg: Settings, body: Answer): Promise<void> {
  const problem = await post($, cfg, '/api/answer', body)
  if (problem) {
    $.ui.toast(`DeepCrew: ${problem}`)
  }
}

function choices($: EngineInterface, cfg: Settings, site: Site, a: DeepcrewAsk, first: boolean) {
  const { Button } = $.ui.resolve(site)
  if (a.kind === 'question') {
    return a.options.map((o, n) => (
      <Button key={`${a.id}-${n}`} label={o} hotkey={first && n < 9 ? String(n + 1) : undefined} onPress={() => answer($, cfg, { id: a.id, option: n })} />
    ))
  }
  return [
    <Button key={`${a.id}-allow`} label="Allow" variant="primary" hotkey={first ? 'a' : undefined} onPress={() => answer($, cfg, { id: a.id, allow: true })} />,
    ...(a.widening
      ? [<Button key={`${a.id}-always`} label="Always" hotkey={first ? 'w' : undefined} onPress={() => answer($, cfg, { id: a.id, allow: true, always: true })} />]
      : []),
    <Button key={`${a.id}-deny`} label="Deny" hotkey={first ? 'd' : undefined} onPress={() => answer($, cfg, { id: a.id, allow: false })} />,
  ]
}

function askRow($: EngineInterface, cfg: Settings, site: Site, a: DeepcrewAsk, first: boolean) {
  const { Box, Text } = $.ui.resolve(site)
  return (
    <Box key={a.id} flexDirection="column">
      <Text wrap="truncate-end">
        <Text bold>{a.member}</Text> {a.text}
      </Text>
      {a.detail ? <Text dimColor wrap="truncate-end">{a.detail}</Text> : null}
      {a.widening ? <Text dimColor wrap="truncate-end">Always: {a.widening}</Text> : null}
      <Box flexDirection="row" gap={1}>
        {choices($, cfg, site, a, first)}
      </Box>
    </Box>
  )
}

export const register: Register = (on, options) => {
  const cfg: Settings = { command: String(options.command || 'deepcrew'), team: String(options.team || 'main') }

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'crew',
      description: "deepcrew: the crew at a glance, and members' requests answered here",
      argumentHint: '[rest|resume]',
      immediate: true,
    })
    quiet = 0
    found = null
    $.clock.every(POLL_MS, () => poll($, cfg))
    poll($, cfg)
    return next(e)
  })

  on('command.run', { command: 'crew' }, async ($, e) => {
    const action = e.args.trim()
    if (action === 'rest' || action === 'resume') {
      const problem = await post($, cfg, '/api/crew', { action })
      return { text: problem ?? `the crew ${action === 'rest' ? 'rests' : 'is back at work'}` }
    }
    const now = await refresh($, cfg)
    if (!now) {
      return { text: `no team ${cfg.team} in this project, or \`${cfg.command}\` is not on this session's PATH` }
    }
    await $.ui.open({ id: PANE, title: 'DeepCrew' })
    const asks = now.asks.map(a => `\n  ${a.member} ${a.text}`).join('')
    return { text: `${summary(now)}${asks}` }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const c = await read($, crew)
    if (e.props.hasSurvey || !c || c.asks.length === 0) {
      return next(e)
    }
    const { Box, Text } = $.ui.resolve(e)
    const [a, ...rest] = c.asks
    return (
      <Box flexDirection="column" width={e.props.bodyColumns}>
        <Text dimColor>
          DeepCrew {c.team}
          {rest.length ? `, ${rest.length} more: /crew` : ''}
        </Text>
        {askRow($, cfg, e, a!, true)}
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Link } = $.ui.resolve(e)
    const c = await read($, crew)
    if (!c) {
      return <Text dimColor>No deepcrew team in this project.</Text>
    }
    return (
      <Box flexDirection="column" gap={1}>
        <Text wrap="wrap">{summary(c)}</Text>
        {c.asks.length ? (
          <Box flexDirection="column" gap={1}>
            {c.asks.map((a, n) => askRow($, cfg, e, a, n === 0))}
          </Box>
        ) : (
          <Text dimColor>Nothing waits for an answer.</Text>
        )}
        <Box flexDirection="column">
          {c.members.map(m => (
            <Text key={m.name} wrap="truncate-end">
              <Text bold>{m.name}</Text> {m.state}
              {m.task ? `, task ${m.task}` : ''}
              {m.cost !== null ? `, ${money(m.cost)}` : ''}
            </Text>
          ))}
        </Box>
        <Box flexDirection="row" gap={1}>
          {c.resting ? (
            <Button key="resume" label="Resume crew" onPress={() => post($, cfg, '/api/crew', { action: 'resume' })} />
          ) : (
            <Button key="rest" label="Rest crew" onPress={() => post($, cfg, '/api/crew', { action: 'rest' })} />
          )}
          {c.page ? <Link key="page" href={c.page} label="Team page" /> : null}
        </Box>
      </Box>
    )
  })
}
