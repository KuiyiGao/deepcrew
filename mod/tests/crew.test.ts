import { expect, mock, test } from 'claude-code/testing'
import type { HttpResponse, On, ProcessRunResult, RenderElement } from 'claude-code'

const STATUS = {
  members: [
    { name: 'alpha', state: 'active', task: '2' },
    { name: 'beta', state: 'idle', task: null },
  ],
  usage: { alpha: { cost_usd: 0.12 }, beta: { cost_usd: 0.03 } },
  tasks: [{ state: 'done' }, { state: 'claimed' }, { state: 'open' }, { state: 'cancelled' }],
  permission_requests: [
    {
      id: 'P1a2b3c4d',
      member: 'alpha',
      tool: 'Bash',
      input: { command: 'npm install three', description: 'Install three' },
      review: 'installs a package from npm',
      widening: { kind: 'command', value: 'npm install', text: 'npm install commands from now on' },
    },
  ],
  questions: [{ id: 'Q5e6f7a8b', member: 'beta', question: 'Which chart library?', options: ['Recharts', 'D3'] }],
  budget: { usd: 5, spent: 0.15 },
  paused: null,
  page_url: 'http://127.0.0.1:4242/?t=tok123',
}
const BAND = {
  component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking: false, maxRows: 12, bodyColumns: 100, scroll: { offset: 0, bodyRows: 12 }, view: {} },
} as const
const PANE = {
  component: 'Pane',
  requestId: 'deepcrew',
  props: { title: 'DeepCrew', isFocused: false, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} },
} as const

type Posted = { url: string; token: unknown; body: unknown }
const TYPED = { origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 100 } } as const

function world(on: On, status: () => object | null, page = 'http://127.0.0.1:4343/?t=tok456', missing: string[] = [], env: Record<string, string> = {}) {
  mock.env(on, env)
  const posted: Posted[] = []
  const ran: string[][] = []
  const inbox: { text: string }[] = []
  const prompts: string[] = []
  const toasts: string[] = []
  const lines: (string | undefined)[] = []
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('ui.open', () => ({ value: { isPlaced: true } }) as const)
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Box } = $.ui.resolve(e)
    return h(Box, { key: 'engine' }) as RenderElement
  })
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.status', ($, e) => {
    lines.push(e.text)
    return { value: undefined }
  })
  on('process.run', async ($, e) => {
    ran.push([...e.argv])
    if (missing.includes(e.argv[0] ?? '')) {
      return { deny: `${e.argv[0]}: not found` }
    }
    const found = e.argv[1] === 'ui' ? { url: page } : e.argv[1] === 'inbox' ? { messages: inbox.splice(0) } : e.argv[1] === 'send' ? {} : status()
    const out: ProcessRunResult = { exitCode: found ? 0 : 1, stdout: found ? JSON.stringify(found) : '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
    return { value: out }
  })
  on('http.fetch', async ($, e) => {
    posted.push({ url: e.url, token: e.init?.headers?.['X-Deepcrew-Token'], body: JSON.parse(String(e.init?.body)) })
    const res: HttpResponse = { status: 200, ok: true, headers: {}, text: '{}' }
    return { value: res }
  })
  on('prompt.submit', ($, e) => {
    prompts.push(e.text)
    return { text: e.text }
  })
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('tool.call', () => ({ result: { stdout: '', stderr: '', interrupted: false } }) as never)
  return { posted, ran, toasts, lines, inbox, prompts }
}

test('the oldest request waits above the prompt, and Allow answers it through the team page as the user, on every surface', async ($, on) => {
  const clock = mock.clock(on)
  const { posted, ran } = world(on, () => STATUS)
  await $.session.start({ cwd: '/work/app', surface: 'terminal', isInteractive: true })
  await clock.settle()
  expect(ran[0]).toEqual(['deepcrew', 'status', '--json', '--team', 'main'])
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'deepcrew', surface, ...BAND })
    expect(await ui.find({ text: /wants to run npm install three/ })).toBeDefined()
    expect(await ui.find({ text: /reviewer: installs a package from npm/ })).toBeDefined()
    expect(await ui.find({ text: /1 more: \/crew/ })).toBeDefined()
    await ui.press({ key: 'P1a2b3c4d-allow' })
    expect(posted.at(-1)).toEqual({ url: 'http://127.0.0.1:4242/api/answer', token: 'tok123', body: { id: 'P1a2b3c4d', allow: true } })
    await ui.press({ key: 'P1a2b3c4d-always' })
    expect(posted.at(-1)?.body).toEqual({ id: 'P1a2b3c4d', allow: true, always: true })
    await ui.unmount()
  }
})

test('the pane lists every request, the members and the crew, and answers a question with its option', async ($, on) => {
  const clock = mock.clock(on)
  const { posted } = world(on, () => STATUS)
  await $.session.start({ cwd: '/work/app', surface: 'desktop', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ plugin: 'deepcrew', surface: 'desktop', ...PANE })
  expect(await ui.find({ text: /1 of 2 members working, 1 of 3 tasks done, \$0\.15 of \$5\.00 spent, 2 waiting for an answer/ })).toBeDefined()
  expect(await ui.find({ text: /asks: Which chart library\?/ })).toBeDefined()
  await ui.press({ key: 'Q5e6f7a8b-1' })
  expect(posted.at(-1)).toEqual({ url: 'http://127.0.0.1:4242/api/answer', token: 'tok123', body: { id: 'Q5e6f7a8b', option: 1 } })
  await ui.press({ key: 'rest' })
  expect(posted.at(-1)).toEqual({ url: 'http://127.0.0.1:4242/api/crew', token: 'tok123', body: { action: 'rest' } })
  await ui.unmount()
})

test('/crew tells the leader where the crew stands in one row, with no model turn', async ($, on) => {
  const clock = mock.clock(on)
  world(on, () => STATUS)
  await $.session.start({ cwd: '/work/app', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const out = await $.command.run({ command: 'crew', args: '', ...TYPED })
  expect(out.text).toBe(
    'team main: 1 of 2 members working, 1 of 3 tasks done, $0.15 of $5.00 spent, 2 waiting for an answer' +
      '\n  alpha wants to run npm install three\n  beta asks: Which chart library?',
  )
})

test('with the page closed an answer starts it first, and without a team nothing is drawn', async ($, on) => {
  const clock = mock.clock(on)
  let status: object | null = { ...STATUS, page_url: null }
  const { posted, ran } = world(on, () => status)
  await $.session.start({ cwd: '/work/app', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ plugin: 'deepcrew', surface: 'terminal', ...BAND })
  await ui.press({ key: 'P1a2b3c4d-deny' })
  expect(ran).toContainEqual(['deepcrew', 'ui', '--no-wait', '--json', '--team', 'main'])
  expect(posted.at(-1)).toEqual({ url: 'http://127.0.0.1:4343/api/answer', token: 'tok456', body: { id: 'P1a2b3c4d', allow: false } })
  status = null
  await clock.advance(3000)
  expect(await ui.find({ text: /DeepCrew/ })).toBeUndefined()
  expect((await $.command.run({ command: 'crew', args: '', ...TYPED })).text).toBe("no team main in this project, or `deepcrew` is not on this session's PATH")
  await ui.unmount()
})

test('when Claude Code cannot find deepcrew on its PATH, the copy in ~/.local/bin answers', async ($, on) => {
  const clock = mock.clock(on)
  const { ran } = world(on, () => STATUS, undefined, ['deepcrew'], { HOME: '/Users/me' })
  await $.session.start({ cwd: '/work/app', surface: 'terminal', isInteractive: true })
  await clock.settle()
  expect(ran.slice(0, 2).map(argv => argv[0])).toEqual(['deepcrew', '/Users/me/.local/bin/deepcrew'])
  expect((await $.command.run({ command: 'crew', args: '', ...TYPED })).text).toMatch(/^team main: /)
  expect(ran.at(-1)?.[0]).toBe('/Users/me/.local/bin/deepcrew')
})

const ANSWERED = { durationMs: 900, isAborted: false, turnId: 't1', reason: 'answer' } as const

test('what the user writes to the leader on the team page comes here as a prompt, and the answer goes back to the page', async ($, on) => {
  const clock = mock.clock(on)
  let waiting = 1
  const w = world(on, () => ({ ...STATUS, user_messages: waiting }))
  w.inbox.push({ text: 'Add a dark mode, please.' })
  await $.session.start({ cwd: '/work/app', surface: 'desktop', isInteractive: true })
  await clock.settle()
  expect(w.ran).toContainEqual(['deepcrew', 'inbox', '--from-user', '--json', '--team', 'main'])
  expect(w.prompts).toEqual([
    'On the team page the user wrote to you, the leader of team main:\n\n> Add a dark mode, please.\n\n' +
      'Answer as you would in this chat. Your answer is shown on the team page too, so there is no need to send it there.',
  ])
  waiting = 0
  await $.turn.start({ text: w.prompts[0]!, turnId: 't1' })
  await $.turn.complete({ answer: 'On it: alpha takes the dark mode.', ...ANSWERED })
  await clock.settle()
  expect(w.ran.at(-1)).toEqual(['deepcrew', 'send', 'user', 'On it: alpha takes the dark mode.', '--team', 'main'])
  await $.turn.start({ text: 'And the chart?', turnId: 't2' })
  await $.turn.complete({ answer: 'Another turn.', ...ANSWERED, turnId: 't2' })
  await clock.settle()
  expect(w.ran.filter(argv => argv[1] === 'send')).toHaveLength(1)
})

test('a turn the user started here stays here, and so does one whose answer the leader sent to the page itself', async ($, on) => {
  const clock = mock.clock(on)
  let waiting = 0
  const w = world(on, () => ({ ...STATUS, user_messages: waiting }))
  await $.session.start({ cwd: '/work/app', surface: 'desktop', isInteractive: true })
  await clock.settle()
  await $.turn.start({ text: 'How is the crew doing?', turnId: 't0' })
  await $.turn.complete({ answer: 'Two of three tasks are done.', ...ANSWERED, turnId: 't0' })
  await clock.settle()
  w.inbox.push({ text: 'Is the chart done?' })
  waiting = 1
  await clock.advance(3000)
  expect(w.prompts.at(-1)).toMatch(/^On the team page the user wrote to you/)
  waiting = 0
  await $.turn.start({ text: w.prompts.at(-1)!, turnId: 't3' })
  await $.tool.call({ tool: 'Bash', command: 'deepcrew send user "Not yet: ui is on it."', description: 'tell the user' })
  await $.turn.complete({ answer: 'I told the user on the page.', ...ANSWERED, turnId: 't3' })
  await clock.settle()
  expect(w.ran.filter(argv => argv[1] === 'send')).toEqual([])
})
