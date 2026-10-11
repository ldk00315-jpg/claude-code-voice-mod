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
function stateStore(options: { failReads?: boolean; failWrites?: boolean } = {}) {
  const values = new Map<string, unknown>()
  let version = 0
  const store = {
    values,
    failWrites: options.failWrites ?? false,
    writes: 0,
    get: async (ref: { plugin: string; key: string }) => {
      if (options.failReads) throw new Error('state unavailable')
      const value = values.get(`${ref.plugin}/${ref.key}`)
      return { value: value === undefined ? undefined : JSON.parse(JSON.stringify(value)), version }
    },
    set: async (ref: { plugin: string; key: string }, value: unknown) => {
      store.writes++
      if (store.failWrites) throw new Error('state write refused')
      values.set(`${ref.plugin}/${ref.key}`, JSON.parse(JSON.stringify(value)))
      version++
      return { isSet: true, version }
    },
  }
  return store
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

type Row = { role: 'user' | 'assistant'; text: string; toolUses: unknown[]; toolResults?: unknown[] }

/**
 * The engine's `$` for one load. `transcript` is what `$.session.messages()` answers (left out:
 * the call throws, as when the transcript cannot be read).
 */
function engine(state: ReturnType<typeof stateStore>, transcript?: () => Row[]) {
  const toasts: string[] = []
  const statuses: (string | undefined)[] = []
  const $ = {
    state: { get: state.get, set: (ref: { plugin: string; key: string }, value: unknown) => state.set(ref, value) },
    ui: { toast: (t: string) => void toasts.push(t), status: (t?: string) => void statuses.push(t), log: () => undefined },
    command: { register: async () => undefined },
    session: {
      messages: async () => {
        if (!transcript) throw new Error('transcript unavailable')
        return transcript()
      },
    },
  }
  return { $, toasts, statuses }
}

/** Rows of a transcript: a prompt, an assistant reply calling a tool, its result, a final answer. */
const prompt = (text: string): Row => ({ role: 'user', text, toolUses: [] })
const callingTool: Row = { role: 'assistant', text: 'Let me run it.', toolUses: [{ id: 't1', name: 'Bash', input: {} }] }
const toolResult: Row = { role: 'user', text: '', toolUses: [], toolResults: [{ tool_use_id: 't1', text: 'ok', isError: false }] }
const finalAnswer: Row = { role: 'assistant', text: 'Done.', toolUses: [] }

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

// R1 of the third security review: a failed save, then a reload in the running voice turn.
const PUSH = { tool: 'Bash', command: 'git push origin main' }

test('save fails at a voice turn start, reload with nothing saved: the transcript keeps the guard', async () => {
  const state = stateStore({ failWrites: true })
  const running = () => [prompt(promptFor('push the branch')), callingTool]
  const { $, toasts, statuses } = engine(state, running)
  const first = load()
  await first.hooks.get('turn.start')?.($, { text: promptFor('push the branch'), turnId: 'V1' }, next)
  expect(await call(first, $, PUSH)).toContain('came in by voice') // guarded in memory
  expect(toasts.filter(t => t.includes('could not be saved')).length).toBe(1) // one toast
  expect(statuses.at(-1) ?? '').toContain('NOT saved')
  expect(state.values.size).toBe(0)

  const second = load() // the reload: the store answers undefined, no turn.start for V1
  expect(await call(second, engine(state, running).$, PUSH)).toContain('came in by voice')
  expect(await call(second, engine(state, running).$, { tool: 'Bash', command: 'ls' })).toBe('reached core')
  // The running turn ends: typed work afterwards is not blocked.
  await second.hooks.get('turn.complete')?.($, { ...DONE, turnId: 'V1' }, async () => ({ text: 'ok' }))
  await second.hooks.get('turn.start')?.($, { text: 'typed', turnId: 'U2' }, next)
  expect(await call(second, $, PUSH)).toBe('reached core')
})

test('save fails at a voice turn start, reload over an older typed snapshot: still guarded', async () => {
  const state = stateStore()
  const { $ } = engine(state, () => [])
  const first = load()
  await first.hooks.get('turn.start')?.($, { text: 'typed work', turnId: 'U1' }, next)
  await first.hooks.get('turn.complete')?.($, { ...DONE, turnId: 'U1' }, async () => ({ text: 'ok' }))
  expect(state.values.size).toBe(1) // a typed snapshot landed

  state.failWrites = true
  const transcript = () => [prompt('typed work'), finalAnswer, prompt(promptFor('push it')), callingTool]
  const live = engine(state, transcript).$
  await first.hooks.get('turn.start')?.(live, { text: promptFor('push it'), turnId: 'V1' }, next)
  const second = load()
  expect(await call(second, live, PUSH)).toContain('came in by voice')
})

test('a failed save is retried at the next hook entry and the warning clears', async () => {
  const state = stateStore({ failWrites: true })
  const { $, statuses } = engine(state, () => [])
  const first = load()
  await first.hooks.get('turn.start')?.($, { text: promptFor('push it'), turnId: 'V1' }, next)
  const failed = state.writes
  await call(first, $, PUSH) // nothing changed, but the unsaved revision is tried again
  expect(state.writes).toBe(failed + 1)
  state.failWrites = false
  expect(await call(first, $, PUSH)).toContain('came in by voice')
  const saved = state.values.get('voice/guard') as { scope: { mainIsVoice: boolean; mainTurn: string } }
  expect(saved.scope.mainIsVoice).toBe(true)
  expect(saved.scope.mainTurn).toBe('V1')
  expect((statuses.at(-1) ?? '').includes('NOT saved')).toBe(false)
  // Saved now: a reload with an unreadable transcript is guarded by the snapshot alone.
  const second = load()
  expect(await call(second, engine(state).$, PUSH)).toContain('came in by voice')
})

test('a brand-new typed session is not blocked by the transcript cross-check', async () => {
  const state = stateStore()
  const fresh = load()
  const { $ } = engine(state, () => [])
  await fresh.hooks.get('session.start')?.($, {}, async () => ({}))
  expect(await call(fresh, $, PUSH)).toBe('reached core')

  // A reload during a typed turn that follows a finished and an interrupted voice turn.
  const transcript = (): Row[] => [
    prompt(promptFor('look at the logs')), callingTool, toolResult, finalAnswer,
    prompt(promptFor('push it')), callingTool, prompt('[Request interrupted by user for tool use]'),
    prompt('please git push'), callingTool,
  ]
  const second = load()
  expect(await call(second, engine(stateStore(), transcript).$, PUSH)).toBe('reached core')
})
