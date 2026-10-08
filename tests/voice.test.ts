// Offline check of the voice Mod: queue, correlation, answer POSTs, guard on voice turns only.
// The test's hooks stand in for the engine (a fake helper child, the prompt queue, HTTP, tools).
import { expect, mock, test } from 'claude-code/testing'
import type { HttpInit, On } from 'claude-code'

const TYPED = { origin: { kind: 'composer' as const }, presentation: { isFullscreen: false, columns: 120 } }
const READY = '{"type":"ready","port":4321,"pid":1}\n'
const req = (id: string, text: string) => JSON.stringify({ type: 'request', id, text, userSaid: [] }) + '\n'
const DONE = { durationMs: 1, isAborted: false, reason: 'answer' as const }

type Posts = { url: string; init?: HttpInit }[]

function engine(on: On, lines: string) {
  const submitted: string[] = []
  const posts: Posts = []
  const toolsRun: string[] = []
  const spawned: string[][] = []
  let release = () => {}
  const childLives = new Promise<void>(resolve => (release = resolve))
  let wake = () => {}
  let waiting = new Promise<void>(resolve => (wake = resolve))

  on('process.spawn', async function* (_$, e) {
    spawned.push([...e.argv])
    yield { stream: 'stdout' as const, text: lines }
    await childLives
    return { value: { code: 0, signal: null } }
  })
  on('prompt.submit', (_$, e) => {
    submitted.push(e.text)
    wake()
    return { text: e.text, origin: e.origin }
  })
  on('http.fetch', (_$, e) => {
    posts.push(e)
    return { value: { status: 200, ok: true, headers: {}, text: '{"ok":true}' } }
  })
  on('tool.call', (_$, e) => {
    toolsRun.push(e.tool)
    return { deny: 'reached core' } // stands for "the tool would have run"
  })
  const store = new Map<string, unknown>()
  on('store.get', (_$, e) => ({ value: store.get(e.key) }))
  on('store.set', (_$, e) => {
    store.set(e.key, e.value)
    return { value: undefined }
  })
  on('session.root', () => ({ value: 'C:\\work\\demo-project' }))
  on('session.id', () => ({ value: '0a1b2c3d-0000-4000-8000-000000000000' }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('turn.start', (_$, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_$, e) => ({ text: e.answer }))
  return {
    submitted,
    posts,
    toolsRun,
    spawned,
    release,
    async nextSubmit(count: number) {
      while (submitted.length < count) {
        await waiting
        waiting = new Promise<void>(resolve => (wake = resolve))
      }
    },
  }
}

function bodyOf(posts: Posts, i: number) {
  return JSON.parse(posts[i]?.init?.body ?? '{}')
}

/** What a tool call ends with, as text: the deny reason or the errored result. */
function outcome(r: unknown): string {
  const o = r as { deny?: string; text?: string; result?: unknown }
  return o.deny ?? o.text ?? JSON.stringify(o.result ?? '')
}

test('requests run one at a time; answers go back; the guard covers voice turns only', async ($, on) => {
  const eng = engine(on, READY + req('r1', 'list the files in this folder') + req('r2', 'check the latest log'))

  const ran = await $.command.run({ command: 'voice', args: 'on', ...TYPED })
  expect(ran.text).toContain('[demo-project・0a1b]') // no name given: folder + session id head
  // The helper is found inside this plugin's own folder, never at a fixed path.
  const argv = eng.spawned[0] ?? []
  expect(argv.slice(0, 3)).toEqual(['uv', 'run', '--script'])
  expect(argv[3] ?? '').toMatch(/^([A-Za-z]:)?\/.+\/helper\/voice_helper\.py$/) // absolute, under the plugin
  expect(argv.slice(4)).toEqual(['--open', '--label', 'demo-project・0a1b'])
  await eng.nextSubmit(1)
  expect(eng.submitted.length).toBe(1) // r2 waits for r1
  expect(eng.submitted[0]?.startsWith('[voice] list the files in this folder')).toBe(true)

  // r1's turn.
  await $.turn.start({ text: eng.submitted[0] ?? '', turnId: 'V1' })
  const pushed = await $.tool.call({ tool: 'Bash', command: 'git push origin main' })
  expect(outcome(pushed)).toContain('came in by voice')
  const removed = await $.tool.call({ tool: 'PowerShell', command: 'Remove-Item -Recurse -Force .\\dist' } as never)
  expect(outcome(removed)).toContain('came in by voice')
  const browsed = await $.tool.call({ tool: 'mcp__Claude_Browser__navigate', url: 'https://example.com' } as never)
  expect(outcome(browsed)).toContain('came in by voice')
  const listed = await $.tool.call({ tool: 'Bash', command: 'ls -la' })
  expect(outcome(listed)).toBe('reached core')
  expect(eng.toolsRun).toEqual(['Bash'])

  // A subagent finishing inside it is not the answer.
  await $.turn.complete({ ...DONE, answer: 'sub', turnId: 'V1', agentId: 'a1' })
  expect(eng.posts).toEqual([])
  await $.turn.complete({ ...DONE, answer: 'There are two folders: helper and hooks.', turnId: 'V1' })
  expect(eng.posts.length).toBe(1)
  expect(eng.posts[0]?.url).toBe('http://127.0.0.1:4321/answer')
  expect(bodyOf(eng.posts, 0)).toMatchObject({ id: 'r1', answer: 'There are two folders: helper and hooks.', status: 'ok', turnId: 'V1' })

  // r2 is submitted next.
  await eng.nextSubmit(2)
  expect(eng.submitted[1]?.startsWith('[voice] check the latest log')).toBe(true)

  // A typed turn that runs first is not guarded and not posted.
  await $.turn.start({ text: 'please git push', turnId: 'U1' })
  const typedPush = await $.tool.call({ tool: 'Bash', command: 'git push origin main' })
  expect(outcome(typedPush)).toBe('reached core')
  await $.turn.complete({ ...DONE, answer: 'pushed', turnId: 'U1' })
  expect(eng.posts.length).toBe(1)

  // r2's turn is interrupted: the voice is told.
  await $.turn.start({ text: eng.submitted[1] ?? '', turnId: 'V2' })
  await $.turn.complete({ ...DONE, isAborted: true, reason: 'aborted', answer: '', turnId: 'V2' })
  expect(eng.posts.length).toBe(2)
  expect(bodyOf(eng.posts, 1)).toMatchObject({ id: 'r2', status: 'aborted' })

  // After the voice turns, nothing is guarded.
  await $.turn.start({ text: 'rm -rf build', turnId: 'U2' })
  expect(outcome(await $.tool.call({ tool: 'Bash', command: 'rm -rf build' }))).toBe('reached core')
  await $.turn.complete({ ...DONE, answer: 'ok', turnId: 'U2' })

  const status = await $.command.run({ command: 'voice', args: 'status', ...TYPED })
  expect(status.text).toContain('port=4321')
  const off = await $.command.run({ command: 'voice', args: 'off', ...TYPED })
  expect(off.text).toBe('voice: stopped')
  expect(eng.posts[2]?.url).toBe('http://127.0.0.1:4321/shutdown')
  eng.release()
})

test('a request that takes too long is answered "timeout", and the late answer still goes back', async ($, on) => {
  const clock = mock.clock(on)
  const eng = engine(on, READY + req('r1', 'run a long investigation'))
  await $.command.run({ command: 'voice', args: 'on', ...TYPED })
  await eng.nextSubmit(1)
  await $.turn.start({ text: eng.submitted[0] ?? '', turnId: 'V1' })

  await clock.advance(10 * 60 * 1000 + 1)
  expect(eng.posts.length).toBe(1)
  expect(bodyOf(eng.posts, 0)).toMatchObject({ id: 'r1', status: 'timeout' })

  // Still a voice turn while it runs.
  expect(outcome(await $.tool.call({ tool: 'Bash', command: 'git push' }))).toContain('came in by voice')
  await $.turn.complete({ ...DONE, answer: 'Finally done.', turnId: 'V1' })
  expect(eng.posts.length).toBe(2)
  expect(bodyOf(eng.posts, 1)).toMatchObject({ id: 'r1', status: 'late', answer: 'Finally done.' })

  await $.command.run({ command: 'voice', args: 'off', ...TYPED })
  eng.release()
})
