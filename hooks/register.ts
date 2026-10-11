// voice: talk to Claude Code through the codex realtime voice helper.
//
//   /voice on       spawn the helper (uv run --script <plugin>/helper/voice_helper.py --open):
//                   Edge page = mic/speaker,
//                   codex app-server = OpenAI realtime (ChatGPT login)
//   helper "request" line (a handoff from the realtime model)
//                   -> queued; one at a time -> $.prompt.submit("[voice] ...")
//   turn.complete of that turn (main loop) -> POST http://127.0.0.1:PORT/answer {id, answer, status}
//                   -> the helper hands it to the realtime model, which says a 1-2 sentence summary
//   /voice off      ask the helper to exit, then end the stream (kills it)
//   /voice page     open the voice page again (a fresh one-time link)
//   /voice status   phase, queue, port
//
// Voice-origin guard: while a voice turn runs (and in every subagent it started), tool.call
// denies destructive deletes, git push, gh writes, publishes, sends, browser/computer use,
// scheduled tasks and sending/deleting MCP tools (hooks/guard.ts). Typed turns are untouched.
// The guard's scope is saved in `$.state` on every change, so a hot reload of this module in
// the middle of a voice turn restores it before the next tool call (see restore / save). A save
// that fails is retried at every hook entry and shown in the status line; after a reload the
// saved scope is cross-checked with the transcript, so a lost or stale save still guards a
// running voice turn.
import type { EngineInterface, HookStream, On, ProcessSpawnChunk, ProcessSpawnResult, Register, SessionMessage } from 'claude-code'
import type { GuardState } from '../types'
import { VoiceScope, checkVoiceToolCall, denyMessage } from './guard.ts'
import { MARK, isVoicePrompt, promptFor } from './prompt.ts'

const NAME = 'voice'
const UNSAVED = 'guard NOT saved (retrying)'
const UNSAVED_TOAST = 'voice: the voice guard could not be saved; a reload of this Mod now may drop it. Retrying.'
const GUARD = { plugin: 'voice', key: 'guard' } as const

/**
 * The helper ships inside this plugin (`<plugin root>/helper`). It is stdlib only and carries
 * inline script metadata (PEP 723), so `uv run --script` runs it in uv's cache: nothing is
 * written into the plugin folder (which Claude Code may watch and hot-reload). Its logs and
 * settings live in a per-user data folder (see helper/voice_helper.py, --data-dir).
 */
export function helperArgv(pluginRoot: string, label: string): string[] {
  const dir = `${pluginRoot.replace(/\\/g, '/').replace(/\/+$/, '')}/helper`
  return ['uv', 'run', '--script', `${dir}/voice_helper.py`, '--open', '--label', label]
}

/** Where this thread's name is kept across /voice off, app restarts and resumes. */
async function labelKey($: EngineInterface): Promise<string> {
  return `label:${await $.session.id()}`
}

/** Remembers the thread's name (given with /voice on, or changed on the voice page). */
async function saveLabel($: EngineInterface, label: string) {
  await $.store.set(await labelKey($), label.slice(0, 60))
}

/**
 * The thread's name for the voice window: the given one (remembered), else the one
 * remembered for this session, else "<folder>・<session id head>".
 */
async function threadLabel($: EngineInterface, given: string): Promise<string> {
  if (given) {
    await saveLabel($, given)
    return given.slice(0, 60)
  }
  const saved = await $.store.get(await labelKey($))
  if (typeof saved === 'string' && saved) return saved
  const folder = (await $.session.root()).split(/[\\/]/).filter(Boolean).at(-1) ?? 'session'
  return `${folder}・${(await $.session.id()).slice(0, 4)}`
}
/** A voice request that has not finished this long after it was submitted is answered "timeout". */
const REQUEST_TIMEOUT_MS = 10 * 60 * 1000
const ANSWER_MAX_CHARS = 8000 // the helper trims to ~2000 for speech; this only bounds the POST
const MAX_SUBMITTED = 50 // submitted prompt texts kept for recognising voice turns
const MAX_SUBMITTED_CHARS = 200_000

type Job = {
  id: string
  text: string // the submitted prompt (with MARK)
  state: 'queued' | 'submitted' | 'running' | 'abandoned' | 'done'
  turnId?: string
  timer?: { cancel: () => void }
}

/**
 * What one load of the module holds. A hot reload calls `register` of a fresh load: all of
 * this starts over and the helper child is killed, but `$.state` (the session's) is kept. The
 * guard's scope and the submitted texts are therefore saved there on every change and
 * restored before the first guard decision of a new load (see restore / save).
 */
type Load = {
  child: HookStream<ProcessSpawnChunk, ProcessSpawnResult> | null
  port: number
  token: string // per-run secret from the helper's ready line; sent as X-Voice-Token
  phase: string
  jobs: Job[]
  scope: VoiceScope
  // Every prompt text this module submitted: a second signal, besides the marker, that a
  // starting turn is a voice turn, even after its job timed out or `/voice off` dropped the queue.
  submittedTexts: string[]
  restored: Promise<void> | null
  savedRevision: number // the scope revision the host holds (advanced only by a landed write)
  pendingRevision: number // the revision a write is under way for
  saving: Promise<boolean>
  unsaved: boolean // the last write failed: warned in the status line until one lands
}

function newLoad(): Load {
  return {
    child: null,
    port: 0,
    token: '',
    phase: 'stopped',
    jobs: [],
    scope: new VoiceScope(),
    submittedTexts: [],
    restored: null,
    savedRevision: -1,
    pendingRevision: -1,
    saving: Promise.resolve(true),
    unsaved: false,
  }
}

/** The note the engine adds when the user stops a turn: that turn has ended. */
const INTERRUPTED = /^\[Request interrupted by user/

/**
 * Whether the main turn that is running (per the transcript) came in by voice: the user prompts
 * of the newest turn that has not ended carry the full voice marker. A turn has ended at its
 * final answer (an assistant message with no tool use that ends the assistant's reply) or at the
 * engine's interrupt note; prompts after the last such point belong to the running
 * turn (a prompt queued into it mid-turn included). No running turn (empty transcript, or the
 * newest message is a final answer) is not a voice turn, so a fresh typed session is not blocked.
 */
export function runningTurnIsVoice(messages: readonly SessionMessage[]): boolean {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i]
    if (!msg) continue
    if (msg.role === 'assistant') {
      // A final answer: no tool use, and nothing but a user message (or nothing) after it. (One
      // reply may be split into several assistant messages, text first and tool use next.)
      const after = messages[i + 1]
      if (msg.toolUses.length === 0 && (after === undefined || after.role === 'user')) return false
      continue
    }
    if ((msg.toolResults?.length ?? 0) > 0) continue // a tool result, not a prompt
    if (isVoicePrompt(msg.text)) return true
    if (INTERRUPTED.test(msg.text)) return false // the turn before it was cut off: it has ended
  }
  return false
}

/**
 * Restores the guard saved by an earlier load of this module, once per load. If it cannot be
 * read, the turn that is running is treated as a voice turn until the next main-loop
 * turn.start or turn.complete (fail-safe without blocking typed work for good).
 *
 * The snapshot alone is not trusted: an earlier load may have failed to save it (or saved it
 * before the voice turn started). So, unless the snapshot already guards the main loop, the
 * transcript is read and a running main turn whose prompt carries the voice marker is guarded
 * the same fail-safe way. If the transcript cannot be read, the snapshot stands.
 *
 * Known gaps (security review 4, documented in the README): when the state could not be saved
 * and the module is reloaded, the guard is lost if (a) the snapshot is missing/typed AND the
 * transcript cannot be read, or (b) a voice turn's background subagent is still running after
 * that turn ended (the transcript shows no running voice turn and the subagent's origin was never
 * saved). The status line shows `guard NOT saved` meanwhile; users are told not to reload then.
 */
function restore(m: Load, $: EngineInterface): Promise<void> {
  m.restored ??= (async () => {
    let readable = true
    try {
      const { value } = await $.state.get(GUARD)
      if (value !== undefined) {
        if (!m.scope.restore(value.scope)) throw new Error('saved guard scope has an unknown shape')
        m.submittedTexts = Array.isArray(value.submitted) ? value.submitted.filter(t => typeof t === 'string') : []
      }
    } catch (err) {
      readable = false
      m.scope.assumeUnknownTurn()
      $.ui.log(`voice: guard state not restored (${String(err)}); guarding the running turn`, { to: 'debug' })
    }
    if (readable && !m.scope.voiceTurnRunning) {
      try {
        if (runningTurnIsVoice(await $.session.messages())) {
          m.scope.assumeUnknownTurn()
          $.ui.log('voice: the transcript shows a running voice turn the saved guard missed; guarding it', { to: 'debug' })
        }
      } catch (err) {
        $.ui.log(`voice: transcript not read for the guard cross-check (${String(err)})`, { to: 'debug' })
      }
    }
    m.savedRevision = m.scope.revision // nothing new to save yet
  })()
  return m.restored
}

/**
 * Saves the guard when it changed. The saved revision advances only once the write landed; a
 * failed write leaves the scope unsaved, warns (status line, one toast), and the next call
 * (every hook entry calls this) tries again. Resolves whether the scope is saved now.
 */
function save(m: Load, $: EngineInterface): Promise<boolean> {
  const revision = m.scope.revision
  if (revision === m.savedRevision) return Promise.resolve(true)
  if (revision === m.pendingRevision) return m.saving
  m.pendingRevision = revision
  const value: GuardState = { scope: m.scope.snapshot(), submitted: [...m.submittedTexts] }
  m.saving = m.saving.then(async () => {
    try {
      await $.state.set(GUARD, value)
      if (revision > m.savedRevision) m.savedRevision = revision
      if (m.unsaved && revision === m.scope.revision) {
        m.unsaved = false
        show(m, $, m.phase)
      }
      return true
    } catch (err) {
      if (m.pendingRevision === revision) m.pendingRevision = -1 // retried by the next call
      $.ui.log(`voice: guard state not saved: ${String(err)}`, { to: 'debug' })
      if (!m.unsaved) {
        m.unsaved = true
        $.ui.toast(UNSAVED_TOAST)
      }
      show(m, $, m.phase)
      return false
    }
  })
  return m.saving
}

function rememberSubmitted(m: Load, text: string) {
  m.submittedTexts = m.submittedTexts.filter(t => t !== text)
  m.submittedTexts.push(text)
  let chars = m.submittedTexts.reduce((n, t) => n + t.length, 0)
  while (m.submittedTexts.length > MAX_SUBMITTED || (chars > MAX_SUBMITTED_CHARS && m.submittedTexts.length > 1)) {
    chars -= m.submittedTexts.shift()?.length ?? 0
  }
  m.scope.revision++ // saved together with the scope
}

function show(m: Load, $: EngineInterface, next: string) {
  m.phase = next
  const parts = [...(m.unsaved ? [UNSAVED] : []), ...(next === 'stopped' ? [] : [next])]
  $.ui.status(parts.length ? `voice: ${parts.join(' | ')}` : undefined)
}

function queued(m: Load): number {
  return m.jobs.filter(j => j.state === 'queued').length
}

function working(m: Load, $: EngineInterface, job: Job) {
  const more = queued(m)
  const short = job.text.slice(MARK.length).split('\n')[0]?.slice(0, 30) ?? ''
  show(m, $, `Claude working on "${short}"${more ? ` (+${more} queued)` : ''}`)
}

function helperPost(m: Load, $: EngineInterface, path: string, body = '{}') {
  return $.http.fetch(`http://127.0.0.1:${m.port}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Voice-Token': m.token },
    body,
  })
}

async function postAnswer(m: Load, $: EngineInterface, job: Job, answer: string, status: string, extra: object = {}) {
  if (!m.port) return
  const body = JSON.stringify({ id: job.id, answer: answer.slice(0, ANSWER_MAX_CHARS), status, ...extra })
  try {
    const res = await helperPost(m, $, '/answer', body)
    if (!res.ok) show(m, $, `answer not spoken (HTTP ${res.status}: voice page closed or session restarted?)`)
  } catch (err) {
    show(m, $, `POST /answer failed: ${String(err)}`)
  }
}

/** Submits the next queued job when nothing is submitted or running. */
async function pump(m: Load, $: EngineInterface) {
  if (m.jobs.some(j => j.state === 'submitted' || j.state === 'running')) return
  const job = m.jobs.find(j => j.state === 'queued')
  if (!job) return
  job.state = 'submitted'
  working(m, $, job)
  job.timer = $.clock.after(REQUEST_TIMEOUT_MS, () => {
    if (job.state !== 'submitted' && job.state !== 'running') return
    job.state = 'abandoned' // a late turn.complete is still posted, as "late"
    void postAnswer(m, $, job, '', 'timeout')
    show(m, $, `timed out ${job.id}`)
    void pump(m, $)
  })
  rememberSubmitted(m, job.text)
  await save(m, $) // saved before the prompt can start a turn
  // Resolves when its turn starts (the session may be busy with a typed turn first).
  void $.prompt.submit({ text: job.text }).catch(err => {
    job.state = 'done'
    job.timer?.cancel()
    void postAnswer(m, $, job, String(err), 'error')
    void pump(m, $)
  })
}

type HelperLine = {
  type?: string
  port?: number
  id?: string
  text?: string
  userSaid?: unknown
  state?: string
  role?: string
  via?: string
  error?: string
  label?: string
  token?: string
  reason?: string
}

async function onLine(m: Load, $: EngineInterface, line: string) {
  let msg: HelperLine
  try {
    msg = JSON.parse(line)
  } catch {
    $.ui.log(`voice: helper line is not JSON (${line.length} chars)`, { to: 'debug' })
    return
  }
  switch (msg.type) {
    case 'ready':
      if (typeof msg.port === 'number') {
        m.port = msg.port
        m.token = typeof msg.token === 'string' ? msg.token : ''
        show(m, $, 'helper ready: press Start on the voice page')
      }
      break
    case 'request': {
      if (!msg.id || !msg.text) break
      if (m.jobs.some(j => j.id === msg.id)) break
      const said = Array.isArray(msg.userSaid) ? msg.userSaid.filter((s): s is string => typeof s === 'string') : []
      m.jobs.push({ id: msg.id, text: promptFor(msg.text, said), state: 'queued' })
      await pump(m, $)
      const current = m.jobs.find(j => j.state === 'submitted' || j.state === 'running')
      if (current) working(m, $, current)
      break
    }
    case 'realtime':
      if (msg.state === 'media-connected' && !busy(m)) show(m, $, 'listening')
      else if (msg.state === 'closed' && !busy(m) && !m.phase.startsWith('blocked')) show(m, $, 'helper running (voice page stopped)')
      else if (msg.state === 'error') $.ui.toast(`voice: realtime error`)
      break
    case 'mcp_blocked': {
      // The helper could not confirm that no MCP server runs for its codex thread, so it
      // refuses (or stopped) the realtime session.
      const why = typeof msg.reason === 'string' && msg.reason ? msg.reason : 'MCP isolation not confirmed'
      show(m, $, `blocked: ${why}`)
      $.ui.toast(`voice: realtime refused: ${why} (see the voice page and the README, "MCP isolation")`)
      break
    }
    case 'answer_delivered':
      if (!busy(m)) show(m, $, 'answered (speaking)')
      break
    case 'answer_undelivered':
      show(m, $, `answer not spoken: ${msg.error ?? ''}`)
      break
    case 'label':
      if (typeof msg.label === 'string' && msg.label) await saveLabel($, msg.label)
      break
    case 'bye':
      $.ui.log(`voice: helper says bye (${typeof msg.reason === 'string' ? msg.reason : ''})`, { to: 'debug' })
      break
  }
}

function busy(m: Load): boolean {
  return m.jobs.some(j => j.state === 'submitted' || j.state === 'running' || j.state === 'queued')
}

function resetJobs(m: Load) {
  for (const j of m.jobs) j.timer?.cancel()
  m.jobs = []
}

function start(m: Load, $: EngineInterface, label: string) {
  const stream = $.process.spawn({ argv: helperArgv($.plugin.root, label) })
  m.child = stream
  show(m, $, 'starting helper')
  // Runs on after the command hook returns; ends with the child or the module.
  void (async () => {
    let buffer = ''
    let ended = 'helper exited'
    try {
      for await (const { stream: pipe, text } of stream) {
        if (pipe === 'stderr') {
          $.ui.log(`voice stderr: ${text}`, { to: 'debug' })
          continue
        }
        buffer += text
        let cut = buffer.indexOf('\n')
        while (cut >= 0) {
          const line = buffer.slice(0, cut).trim()
          buffer = buffer.slice(cut + 1)
          if (line) await onLine(m, $, line)
          cut = buffer.indexOf('\n')
        }
      }
      const { code } = await stream.result
      ended = `helper exited (code ${code})`
    } catch (err) {
      ended = `helper failed: ${String(err)}`
    }
    if (m.child === stream) {
      m.child = null
      m.port = 0
      m.token = ''
      resetJobs(m)
      if (!m.phase.startsWith('blocked')) show(m, $, 'stopped')
      $.ui.toast(`voice: ${ended}`)
    }
  })()
}

async function stopHelper(m: Load, $: EngineInterface) {
  const stream = m.child
  if (!stream) return
  if (m.port) {
    try {
      await helperPost(m, $, '/shutdown')
    } catch {
      // fall through to killing it
    }
  }
  m.child = null
  m.port = 0
  m.token = ''
  resetJobs(m)
  show(m, $, 'stopped')
  // Ending the stream kills the child if it is still alive; not awaited (a pending read
  // settles only with the child's next line or its exit).
  void stream.return({ code: null, signal: null })
}

/**
 * Registers the hooks of one load. Exported (besides `register`) so the tests can load the
 * module twice over one `$.state` store, which is what a hot reload does.
 */
export function loadVoiceMod(on: On) {
  const m = newLoad()

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await restore(m, $)
    await $.command.register({
      name: NAME,
      description: 'Voice front desk (codex realtime) for this session: on | off | page | status',
      argumentHint: 'on [name]|off|page|status',
      immediate: true,
    })
    return started
  })

  on('command.run', { command: NAME }, async ($, e) => {
    const [arg = '', ...rest] = e.args.trim().split(/\s+/)
    if (arg === 'on') {
      if (m.child) return { text: `voice: already ${m.phase}` }
      const label = await threadLabel($, rest.join(' '))
      start(m, $, label)
      return { text: `voice: starting the helper as [${label}]; the voice page opens in Edge. Press Start there and talk.` }
    }
    if (arg === 'off') {
      if (!m.child) return { text: 'voice: not running' }
      await stopHelper(m, $)
      return { text: 'voice: stopped' }
    }
    if (arg === 'page') {
      if (!m.child || !m.port) return { text: 'voice: not running (use /voice on)' }
      try {
        const res = await helperPost(m, $, '/reopen')
        return { text: res.ok ? 'voice: opening the voice page again' : `voice: could not reopen the page (HTTP ${res.status})` }
      } catch (err) {
        return { text: `voice: could not reopen the page: ${String(err)}` }
      }
    }
    await restore(m, $)
    const list = m.jobs
      .filter(j => j.state !== 'done')
      .map(j => `${j.id}:${j.state}`)
      .join(', ')
    const guard = m.scope.unknownTurnRunning
      ? ' (turn of unknown origin after a reload: guard on)'
      : m.scope.voiceTurnRunning
        ? ' (voice turn running: guard on)'
        : ''
    await save(m, $)
    const unsaved = m.unsaved ? ` (${UNSAVED})` : ''
    return { text: `voice: ${m.phase}${m.port ? ` port=${m.port}` : ''}${list ? ` jobs=[${list}]` : ''}${guard}${unsaved}` }
  })

  // Correlation: a plugin's own hooks do not see its own $.prompt.submit, so the
  // voice turn is recognised at turn.start by its text containing the submitted prompt.
  // turn.start fires for the main loop only (a subagent's run raises none).
  // Whether the turn is guarded does not depend on the job queue: the voice marker
  // (prompt.ts isVoicePrompt) or any text this module ever submitted is enough, so a voice
  // prompt still queued in Claude Code after a timeout or `/voice off` runs guarded.
  on('turn.start', async ($, e, next) => {
    await restore(m, $)
    const job = m.jobs.find(
      j => (j.state === 'submitted' || (j.state === 'abandoned' && !j.turnId)) && e.text.includes(j.text),
    )
    if (job) {
      if (job.state === 'submitted') job.state = 'running'
      job.turnId = e.turnId // an abandoned job keeps its state: its answer is posted as "late"
    }
    const voice = job !== undefined || isVoicePrompt(e.text) || m.submittedTexts.some(t => e.text.includes(t))
    m.scope.turnStarted(e.turnId, voice)
    // Before any tool call of the turn can run. If it fails, this load still guards the turn
    // in memory, the status line warns, and every later hook entry retries.
    await save(m, $)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId !== undefined) return result
    await restore(m, $)
    m.scope.turnCompleted(e.turnId)
    await save(m, $)
    const job = m.jobs.find(j => j.turnId === e.turnId && (j.state === 'running' || j.state === 'abandoned'))
    if (job) {
      const late = job.state === 'abandoned'
      job.state = 'done'
      job.timer?.cancel()
      show(m, $, `answering ${job.id}`)
      const status = late ? 'late' : e.reason === 'answer' ? 'ok' : e.reason
      await postAnswer(m, $, job, e.answer, status, { reason: e.reason, turnId: e.turnId, durationMs: e.durationMs })
      m.jobs = m.jobs.filter(j => j.state !== 'done')
      await pump(m, $)
      if (!busy(m) && m.phase.startsWith('answering')) show(m, $, 'answered')
    }
    return result
  })

  // Voice-origin guard.
  on('agent.spawn', async ($, e, next) => {
    await restore(m, $)
    const voice = m.scope.isVoice(e.parentAgentId)
    await save(m, $)
    const spawned = await next(e)
    if (spawned.agentId) {
      if (voice) m.scope.addAgent(spawned.agentId)
      else m.scope.addTypedAgent(spawned.agentId)
      await save(m, $)
    }
    return spawned
  }).catch(($, e, next) => next(e)) // spawning itself is not guarded: its tool calls are

  on('tool.call', async ($, e, next) => {
    await restore(m, $)
    const voice = m.scope.isVoice(e.agentId)
    await save(m, $) // a subagent first seen here stays known after a reload
    if (!voice) return next(e)
    const reason = checkVoiceToolCall(e.tool, e as unknown as Record<string, unknown>)
    if (reason) {
      $.ui.toast(`voice: blocked ${e.tool} (${reason})`)
      return { deny: denyMessage(reason) }
    }
    return next(e)
  }).catch(($, e, next) => {
    // The guard failed before deciding (a throw, its time budget, a re-entry): refuse the
    // kinds of calls it blocks, whatever the turn, and let the rest run.
    if (next.called) return next(e)
    const reason = checkVoiceToolCall(e.tool, e as unknown as Record<string, unknown>)
    return reason ? { deny: denyMessage(`${reason}; the voice guard could not check this call`) } : next(e)
  })

  on('session.end', async ($, e, next) => {
    await stopHelper(m, $)
    return next(e)
  })
}

export const register: Register = on => loadVoiceMod(on)
