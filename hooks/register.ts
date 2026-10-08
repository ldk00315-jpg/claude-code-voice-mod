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
//   /voice status   phase, queue, port
//
// Voice-origin guard: while a voice turn runs (and in every subagent it started), tool.call
// denies destructive deletes, git push, gh writes, publishes, sends, browser/computer use,
// scheduled tasks and sending/deleting MCP tools (hooks/guard.ts). Typed turns are untouched.
import type { EngineInterface, HookStream, ProcessSpawnChunk, ProcessSpawnResult, Register } from 'claude-code'
import { VoiceScope, checkVoiceToolCall, denyMessage } from './guard.ts'
import { MARK, promptFor } from './prompt.ts'

const NAME = 'voice'

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

type Job = {
  id: string
  text: string // the submitted prompt (with MARK)
  state: 'queued' | 'submitted' | 'running' | 'abandoned' | 'done'
  turnId?: string
  timer?: { cancel: () => void }
}

// Module variables: a reload unloads the module, which also kills the child.
let child: HookStream<ProcessSpawnChunk, ProcessSpawnResult> | null = null
let port = 0
let phase = 'stopped'
let jobs: Job[] = []
const scope = new VoiceScope()

function show($: EngineInterface, next: string) {
  phase = next
  $.ui.status(next === 'stopped' ? undefined : `voice: ${next}`)
}

function queued(): number {
  return jobs.filter(j => j.state === 'queued').length
}

function working($: EngineInterface, job: Job) {
  const more = queued()
  const short = job.text.slice(MARK.length).split('\n')[0]?.slice(0, 30) ?? ''
  show($, `Claude working on "${short}"${more ? ` (+${more} queued)` : ''}`)
}

async function postAnswer($: EngineInterface, job: Job, answer: string, status: string, extra: object = {}) {
  if (!port) return
  const body = JSON.stringify({ id: job.id, answer: answer.slice(0, ANSWER_MAX_CHARS), status, ...extra })
  try {
    const res = await $.http.fetch(`http://127.0.0.1:${port}/answer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    })
    if (!res.ok) show($, `answer not spoken (HTTP ${res.status}: voice page closed?)`)
  } catch (err) {
    show($, `POST /answer failed: ${String(err)}`)
  }
}

/** Submits the next queued job when nothing is submitted or running. */
function pump($: EngineInterface) {
  if (jobs.some(j => j.state === 'submitted' || j.state === 'running')) return
  const job = jobs.find(j => j.state === 'queued')
  if (!job) return
  job.state = 'submitted'
  working($, job)
  job.timer = $.clock.after(REQUEST_TIMEOUT_MS, () => {
    if (job.state !== 'submitted' && job.state !== 'running') return
    job.state = 'abandoned' // a late turn.complete is still posted, as "late"
    void postAnswer($, job, '', 'timeout')
    show($, `timed out ${job.id}`)
    pump($)
  })
  // Resolves when its turn starts (the session may be busy with a typed turn first).
  void $.prompt.submit({ text: job.text }).catch(err => {
    job.state = 'done'
    job.timer?.cancel()
    void postAnswer($, job, String(err), 'error')
    pump($)
  })
}

async function onLine($: EngineInterface, line: string) {
  let msg: { type?: string; port?: number; id?: string; text?: string; userSaid?: unknown; state?: string; role?: string; via?: string; error?: string; label?: string }
  try {
    msg = JSON.parse(line)
  } catch {
    $.ui.log(`voice: not JSON: ${line}`, { to: 'debug' })
    return
  }
  switch (msg.type) {
    case 'ready':
      if (typeof msg.port === 'number') {
        port = msg.port
        show($, 'helper ready: press Start on the voice page')
      }
      break
    case 'request': {
      if (!msg.id || !msg.text) break
      if (jobs.some(j => j.id === msg.id)) break
      const said = Array.isArray(msg.userSaid) ? msg.userSaid.filter((s): s is string => typeof s === 'string') : []
      jobs.push({ id: msg.id, text: promptFor(msg.text, said), state: 'queued' })
      pump($)
      const current = jobs.find(j => j.state === 'submitted' || j.state === 'running')
      if (current) working($, current)
      break
    }
    case 'realtime':
      if (msg.state === 'media-connected' && !busy()) show($, 'listening')
      else if (msg.state === 'closed' && !busy()) show($, 'helper running (voice page stopped)')
      else if (msg.state === 'error') $.ui.toast(`voice: realtime error`)
      break
    case 'answer_delivered':
      if (!busy()) show($, 'answered (speaking)')
      break
    case 'answer_undelivered':
      show($, `answer not spoken: ${msg.error ?? ''}`)
      break
    case 'label':
      if (typeof msg.label === 'string' && msg.label) await saveLabel($, msg.label)
      break
    case 'bye':
      $.ui.log(`voice: helper says bye`, { to: 'debug' })
      break
  }
}

function busy(): boolean {
  return jobs.some(j => j.state === 'submitted' || j.state === 'running' || j.state === 'queued')
}

function resetJobs() {
  for (const j of jobs) j.timer?.cancel()
  jobs = []
}

function start($: EngineInterface, label: string) {
  const stream = $.process.spawn({ argv: helperArgv($.plugin.root, label) })
  child = stream
  show($, 'starting helper')
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
          if (line) await onLine($, line)
          cut = buffer.indexOf('\n')
        }
      }
      const { code } = await stream.result
      ended = `helper exited (code ${code})`
    } catch (err) {
      ended = `helper failed: ${String(err)}`
    }
    if (child === stream) {
      child = null
      port = 0
      resetJobs()
      show($, 'stopped')
      $.ui.toast(`voice: ${ended}`)
    }
  })()
}

async function stopHelper($: EngineInterface) {
  const stream = child
  if (!stream) return
  if (port) {
    try {
      await $.http.fetch(`http://127.0.0.1:${port}/shutdown`, { method: 'POST' })
    } catch {
      // fall through to killing it
    }
  }
  child = null
  port = 0
  resetJobs()
  show($, 'stopped')
  // Ending the stream kills the child if it is still alive; not awaited (a pending read
  // settles only with the child's next line or its exit).
  void stream.return({ code: null, signal: null })
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await $.command.register({
      name: NAME,
      description: 'Voice front desk (codex realtime) for this session: on | off | status',
      argumentHint: 'on [name]|off|status',
      immediate: true,
    })
    return started
  })

  on('command.run', { command: NAME }, async ($, e) => {
    const [arg = '', ...rest] = e.args.trim().split(/\s+/)
    if (arg === 'on') {
      if (child) return { text: `voice: already ${phase}` }
      const label = await threadLabel($, rest.join(' '))
      start($, label)
      return { text: `voice: starting the helper as [${label}]; the voice page opens in Edge. Press Start there and talk.` }
    }
    if (arg === 'off') {
      if (!child) return { text: 'voice: not running' }
      await stopHelper($)
      return { text: 'voice: stopped' }
    }
    const list = jobs
      .filter(j => j.state !== 'done')
      .map(j => `${j.id}:${j.state}`)
      .join(', ')
    return {
      text:
        `voice: ${phase}${port ? ` port=${port}` : ''}${list ? ` jobs=[${list}]` : ''}` +
        `${scope.voiceTurnRunning ? ' (voice turn running: guard on)' : ''}`,
    }
  })

  // Correlation: a plugin's own hooks do not see its own $.prompt.submit, so the
  // voice turn is recognised at turn.start by its text containing the submitted prompt.
  // turn.start fires for the main loop only (a subagent's run raises none).
  on('turn.start', async ($, e, next) => {
    const job = jobs.find(j => j.state === 'submitted' && e.text.includes(j.text))
    if (job) {
      job.state = 'running'
      job.turnId = e.turnId
    }
    scope.turnStarted(e.turnId, job !== undefined)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId !== undefined) return result
    scope.turnCompleted(e.turnId)
    const job = jobs.find(j => j.turnId === e.turnId && (j.state === 'running' || j.state === 'abandoned'))
    if (job) {
      const late = job.state === 'abandoned'
      job.state = 'done'
      job.timer?.cancel()
      show($, `answering ${job.id}`)
      const status = late ? 'late' : e.reason === 'answer' ? 'ok' : e.reason
      await postAnswer($, job, e.answer, status, { reason: e.reason, turnId: e.turnId, durationMs: e.durationMs })
      jobs = jobs.filter(j => j.state !== 'done')
      pump($)
      if (!busy() && phase.startsWith('answering')) show($, 'answered')
    }
    return result
  })

  // Voice-origin guard.
  on('agent.spawn', async ($, e, next) => {
    const voice = scope.isVoice(e.parentAgentId)
    const spawned = await next(e)
    if (spawned.agentId) {
      if (voice) scope.addAgent(spawned.agentId)
      else scope.addTypedAgent(spawned.agentId)
    }
    return spawned
  })

  on('tool.call', async ($, e, next) => {
    if (!scope.isVoice(e.agentId)) return next(e)
    const reason = checkVoiceToolCall(e.tool, e as unknown as Record<string, unknown>)
    if (reason) {
      $.ui.toast(`voice: blocked ${e.tool} (${reason})`)
      return { deny: denyMessage(reason) }
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    await stopHelper($)
    return next(e)
  })
}
