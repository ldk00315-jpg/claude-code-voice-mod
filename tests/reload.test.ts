// A hot reload in the middle of a voice turn keeps the guard (F1 of the security re-review).
//
// The engine's own test kit loads the plugin once, so a reload is simulated here: the module's
// loader (loadVoiceMod) is loaded twice, each load a fresh `register` with its own module
// variables, over ONE `$.state` store, as the host keeps it across a reload. The second load
// never sees the turn.start of the running turn.
import { expect, test } from 'claude-code/testing'
import { loadVoiceMod } from '../hooks/register.ts'
import { promptFor } from '../hooks/prompt.ts'

type Hook = (...args: unknown[]) => unknown
type Loaded = { hooks: Map<string, Hook>; catches: Map<string, Hook> }

/** The host's session state, shared by every load of the module. */
function stateStore(options: { failReads?: boolean } = {}) {
  const values = new Map<string, unknown>()
  let version = 0
  return {
    values,
    get: async (ref: { plugin: string; key: string }) => {
      if (options.failReads) throw new Error('state unavailable')
      const value = values.get(`${ref.plugin}/${ref.key}`)
      return { value: value === undefined ? undefined : JSON.parse(JSON.stringify(value)), version }
    },
    set: async (ref: { plugin: string; key: string }, value: unknown) => {
      values.set(`${ref.plugin}/${ref.key}`, JSON.parse(JSON.stringify(value)))
      version++
      return { isSet: true, version }
    },
  }
}

/** One load of the module: runs register against a recording `on`. */
function load(): Loaded {
  const hooks = new Map<string, Hook>()
  const catches = new Map<string, Hook>()
  const on = (event: string, a: unknown, b?: unknown) => {
    const hook = (typeof a === 'function' ? a : b) as Hook
    hooks.set(event, hook)
    return { catch: (h: Hook) => void catches.set(event, h) }
  }
  loadVoiceMod(on as never)
  return { hooks, catches }
}

function engine(state: ReturnType<typeof stateStore>) {
  const toasts: string[] = []
  const $ = {
    state: { get: state.get, set: state.set },
    ui: { toast: (t: string) => void toasts.push(t), status: () => undefined, log: () => undefined },
    command: { register: async () => undefined },
  }
  return { $, toasts }
}

const reached = { text: 'reached core' }
const next = async () => reached
const nextSpawn = (agentId: string) => async () => ({ agentId })

async function call(m: Loaded, $: unknown, input: Record<string, unknown>): Promise<string> {
  const r = (await m.hooks.get('tool.call')?.($, input, next)) as { deny?: string; text?: string }
  return r.deny ?? r.text ?? ''
}

const DONE = { durationMs: 1, isAborted: false, reason: 'answer', answer: 'ok' }

test('reload during a voice turn: the main loop stays guarded', async () => {
  const state = stateStore()
  const { $ } = engine(state)
  const first = load()
  await first.hooks.get('turn.start')?.($, { text: promptFor('push the branch'), turnId: 'V1' }, next)
  expect(await call(first, $, { tool: 'Bash', command: 'git push origin main' })).toContain('came in by voice')

  const second = load() // the reload: fresh module, same state, no turn.start for V1
  expect(await call(second, $, { tool: 'Bash', command: 'git push origin main' })).toContain('came in by voice')
  expect(await call(second, $, { tool: 'Bash', command: 'ls -la' })).toBe('reached core')

  // V1 ends; a typed turn after the reload is not guarded.
  await second.hooks.get('turn.complete')?.($, { ...DONE, turnId: 'V1' }, async () => ({ text: 'ok' }))
  await second.hooks.get('turn.start')?.($, { text: 'please git push', turnId: 'U1' }, next)
  expect(await call(second, $, { tool: 'Bash', command: 'git push origin main' })).toBe('reached core')
})

test('reload during a voice turn: its subagents stay guarded, also after the turn', async () => {
  const state = stateStore()
  const { $ } = engine(state)
  const first = load()
  await first.hooks.get('turn.start')?.($, { text: promptFor('clean up'), turnId: 'V1' }, next)
  await first.hooks.get('agent.spawn')?.($, { parentAgentId: undefined }, nextSpawn('sub-1'))
  await first.hooks.get('agent.spawn')?.($, { parentAgentId: 'sub-1' }, nextSpawn('sub-2'))

  const second = load()
  const viaSub = await call(second, $, { tool: 'PowerShell', command: 'Remove-Item -Recurse dist', agentId: 'sub-1' })
  expect(viaSub).toContain('came in by voice')
  await second.hooks.get('turn.complete')?.($, { ...DONE, turnId: 'V1' }, async () => ({ text: 'ok' }))
  // A background subagent of the voice turn, after the turn ended and after another reload.
  const third = load()
  expect(await call(third, $, { tool: 'Bash', command: 'git push', agentId: 'sub-2' })).toContain('came in by voice')
  await third.hooks.get('turn.start')?.($, { text: 'typed work', turnId: 'U1' }, next)
  expect(await call(third, $, { tool: 'Bash', command: 'git push' })).toBe('reached core')
  expect(await call(third, $, { tool: 'Bash', command: 'git push', agentId: 'sub-2' })).toContain('came in by voice')
})

test('reload during a typed turn: nothing is guarded', async () => {
  const state = stateStore()
  const { $ } = engine(state)
  const first = load()
  await first.hooks.get('turn.start')?.($, { text: 'please push', turnId: 'U1' }, next)
  await first.hooks.get('agent.spawn')?.($, { parentAgentId: undefined }, nextSpawn('typed-sub'))
  const second = load()
  expect(await call(second, $, { tool: 'Bash', command: 'git push' })).toBe('reached core')
  expect(await call(second, $, { tool: 'Bash', command: 'git push', agentId: 'typed-sub' })).toBe('reached core')
})

test('a voice prompt submitted before the reload is still recognised by its text', async () => {
  const state = stateStore()
  // What an earlier load saved after submitting a prompt whose marker was later cut off.
  await state.set({ plugin: 'voice', key: 'guard' }, {
    scope: { v: 1, mainTurn: null, mainIsVoice: false, unknownTurn: false, agents: [], typedAgents: [] },
    submitted: ['[voice] tidy the logs'],
  })
  const { $ } = engine(state)
  const fresh = load()
  await fresh.hooks.get('turn.start')?.($, { text: '[voice] tidy the logs', turnId: 'V9' }, next)
  expect(await call(fresh, $, { tool: 'Bash', command: 'rm -rf logs' })).toContain('came in by voice')
})

test('saved guard unreadable after a reload: fail-safe until the running turn ends', async () => {
  const good = stateStore()
  const { $ } = engine(good)
  const first = load()
  await first.hooks.get('turn.start')?.($, { text: promptFor('push it'), turnId: 'V1' }, next)

  const broken = engine(stateStore({ failReads: true })).$
  const second = load()
  expect(await call(second, broken, { tool: 'Bash', command: 'git push' })).toContain('came in by voice')
  expect(await call(second, broken, { tool: 'Bash', command: 'git status' })).toBe('reached core')
  expect(await call(second, broken, { tool: 'Bash', command: 'git push', agentId: 'unknown-sub' })).toContain('came in by voice')
  // The running turn ends: typed work is not blocked from then on.
  await second.hooks.get('turn.complete')?.(broken, { ...DONE, turnId: 'V1' }, async () => ({ text: 'ok' }))
  expect(await call(second, broken, { tool: 'Bash', command: 'git push' })).toBe('reached core')
  await second.hooks.get('turn.start')?.(broken, { text: 'typed', turnId: 'U2' }, next)
  expect(await call(second, broken, { tool: 'Bash', command: 'git push' })).toBe('reached core')
})

test('the guard refuses blocked kinds when it fails before deciding', async () => {
  const m = load()
  const handler = m.catches.get('tool.call')
  const notCalled = Object.assign(async () => reached, { called: false })
  const denied = (await handler?.({}, { tool: 'Bash', command: 'git push' }, notCalled)) as { deny?: string }
  expect(denied.deny ?? '').toContain('could not check')
  const passed = (await handler?.({}, { tool: 'Bash', command: 'ls' }, notCalled)) as { text?: string }
  expect(passed.text).toBe('reached core')
})
