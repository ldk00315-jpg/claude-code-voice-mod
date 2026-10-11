// Voice-origin guard: what a turn that came from the voice front desk may not do.
// Pure functions (no `$`), so the tests can call them directly.
//
// Scope: VoiceScope decides whether a tool call belongs to a voice-originated turn
// (the main loop while that turn runs, and every subagent it started, including
// background ones that outlive it). checkVoiceToolCall decides whether the call is
// one of the blocked kinds. Typed turns are never checked.
import type { ScopeSnapshot } from '../types'

/** Why a tool call is blocked, or null when it may run. */
export function checkVoiceToolCall(tool: string, input: Record<string, unknown>): string | null {
  // Browser / computer-use control.
  for (const prefix of BROWSER_PREFIXES) {
    if (tool.startsWith(prefix)) return 'browser / screen control'
  }
  // Publishing artifacts (publish is the default action) and shared artifact writes.
  if (tool === 'Artifact') {
    const action = typeof input.action === 'string' ? input.action : 'publish'
    if (!ARTIFACT_READ_ACTIONS.has(action)) return `Artifact ${action} (publish / delete / change)`
  }
  if (tool === 'ArtifactData') {
    const action = typeof input.action === 'string' ? input.action : ''
    if (!['get', 'list', 'query'].includes(action)) return `ArtifactData ${action || 'write'}`
  }
  if (tool === 'ArtifactComments') {
    const action = typeof input.action === 'string' ? input.action : ''
    if (!/^(list|read|get|watch)/.test(action)) return 'posting an Artifact comment'
  }
  // Scheduled tasks, cron, remote triggers.
  if (SCHEDULE_TOOLS.test(tool)) return 'creating or changing a scheduled task'
  if (tool === 'Skill' && typeof input.skill === 'string' && SCHEDULE_SKILLS.test(input.skill)) {
    return 'creating a scheduled task (skill)'
  }
  // MCP tools that send, post, publish, delete, share, or create events.
  if (tool.startsWith('mcp__')) {
    const name = tool.split('__').slice(2).join('__').toLowerCase() || tool.toLowerCase()
    if (/schedul|cron|trigger/.test(tool.toLowerCase()) && !/^(list|get|read)/.test(name)) {
      return 'creating or changing a scheduled task'
    }
    const hit = MCP_WORDS.find(w => name.includes(w))
    if (hit) return `sending / publishing / deleting outside (${tool})`
    if (MCP_SETTINGS.test(name)) return `changing settings (${tool})`
  }
  // Shell commands (Bash, PowerShell, Monitor, ...): anything with a string `command`.
  const command = typeof input.command === 'string' ? input.command : null
  if (command !== null) return checkCommand(command)
  return null
}

/** Why a shell command is blocked, or null. Checked per pipeline segment. */
export function checkCommand(command: string): string | null {
  // Normalise line continuations, then look at each command segment separately so a
  // flag in one command does not count for another.
  const flat = command.replace(/\\\r?\n|`\r?\n/g, ' ')
  const segments = flat.split(/\|\||&&|[|;&\n]/)
  for (const raw of segments) {
    const seg = raw.trim()
    if (!seg) continue
    for (const [re, why] of COMMAND_RULES) {
      if (re.test(seg)) return why
    }
  }
  return null
}

const BROWSER_PREFIXES = ['mcp__computer-use__', 'mcp__Claude_Browser__', 'mcp__claude-in-chrome__', 'mcp__Control_Chrome__']
const ARTIFACT_READ_ACTIONS = new Set(['read', 'list', 'open', 'quickstart'])
const SCHEDULE_TOOLS = /^(Cron(Create|Delete|Update)|RemoteTrigger|ScheduleWakeup|CreateScheduledTask)$/
const SCHEDULE_SKILLS = /(^|:)(schedule|loop)$/
const MCP_WORDS = ['send', 'post', 'publish', 'delete', 'trash', 'share', 'create_event']
const MCP_SETTINGS = /^(set_setting|set_session_permission_mode)$/

// Each rule is matched against one command segment (case-insensitive).
const COMMAND_RULES: [RegExp, string][] = [
  // Recursive / destructive deletes.
  [/\brm\b(?=.*\s(-[a-z]*r[a-z]*|--recursive)\b)/i, 'recursive delete (rm -r)'],
  [/\b(del|erase)\b(?=.*\s\/s\b)/i, 'recursive delete (del /s)'],
  [/\b(rmdir|rd)\b(?=.*\s\/s\b)/i, 'recursive delete (rmdir /s)'],
  [/\b(remove-item|ri|rm|rmdir|del|erase|rd)\b(?=.*\s-r(e(c(u(r(se?)?)?)?)?)?\b)/i, 'recursive delete (Remove-Item -Recurse)'],
  [/\bformat(\.com)?\s+[a-z]:/i, 'formatting a drive'],
  [/\b(format-volume|clear-disk|initialize-disk|diskpart|mkfs(\.\w+)?)\b/i, 'initialising a disk'],
  [/\bgit\b(?=.*\bclean\b)(?=.*\s(-[a-z]*f[a-z]*|--force)\b)/i, 'git clean -f'],
  [/\bgit\b(?=.*\breset\b)(?=.*\s--hard\b)/i, 'git reset --hard'],
  [/\bgit\b(?=.*\bpush\b)/i, 'git push'],
  // GitHub CLI writes.
  [/\bgh\s+pr\s+(create|merge|close|edit|comment|review|ready|reopen)\b/i, 'pull request change via gh'],
  [/\bgh\s+issue\s+(create|close|edit|comment|delete|reopen|transfer|lock|pin)\b/i, 'issue change via gh'],
  [/\bgh\s+release\b/i, 'gh release'],
  [/\bgh\s+repo\s+(edit|delete|create|rename|archive|fork|sync)\b/i, 'repository change via gh'],
  [/\bgh\s+(gist\s+(create|edit|delete)|secret\s+(set|delete)|variable\s+(set|delete)|workflow\s+(run|enable|disable)|run\s+(rerun|cancel|delete))\b/i, 'write via gh'],
  [/\bgh\s+api\b(?=.*(-X|--method)\s*=?\s*['"]?(POST|PATCH|PUT|DELETE)\b)/i, 'write via gh api'],
  [/\bgh\s+api\b(?=.*\s(-f|-F|--field|--raw-field|--input)\b)/i, 'write via gh api'],
  // Package publishes.
  [/\b(npm|pnpm|yarn|bun)\s+(publish|unpublish|deprecate)\b/i, 'publishing a package'],
  [/\b(uv|poetry|flit|hatch|pdm)\s+publish\b|\btwine\s+upload\b/i, 'publishing a package'],
  [/\bcargo\s+(publish|yank)\b|\bgem\s+push\b|\b(dotnet\s+)?nuget\s+push\b|\bvsce\s+publish\b|\bdocker\s+push\b/i, 'publishing a package'],
  // Sending messages / emails / web writes.
  [/\bsend-mailmessage\b|\bsendmail\b|\bmailx?\s+-s\b/i, 'sending mail'],
  [/\bcurl\b(?=.*(\s-X\s*['"]?(POST|PUT|PATCH|DELETE)\b|\s(-d|--data[\w-]*|-F|--form|-T|--upload-file)\b))/i, 'sending data with curl (POST etc.)'],
  [/\b(invoke-webrequest|invoke-restmethod|iwr|irm)\b(?=.*\s-method\s+['"]?(post|put|patch|delete)\b)/i, 'sending data to the web (POST etc.)'],
  [/\bwget\b(?=.*\s--(post-data|post-file|method)\b)/i, 'sending data with wget'],
  // Scheduled tasks.
  [/\bschtasks(\.exe)?\b(?=.*\s\/(create|change|delete|run)\b)/i, 'creating or changing a scheduled task'],
  [/\b(register|new|set|unregister)-scheduledtask\b|\bcrontab\b/i, 'creating or changing a scheduled task'],
]


/**
 * Only the typed set is capped (newest kept). Voice-origin agent ids are never evicted: a
 * background agent of a voice turn may run for the whole session, and forgetting it would turn
 * it into a typed agent. An id is a few dozen bytes, so even 10,000 voice subagents in one
 * session keep the saved snapshot near 400 KB. A typed id that was evicted and shows up again
 * is classified afresh: voice while a voice (or unknown) main turn runs (the safe side), typed
 * otherwise.
 */
export const MAX_TYPED_AGENTS = 500

/**
 * Which tool calls belong to a voice-originated turn.
 *
 * Turns run one at a time on the main loop: a voice turn starts at the `turn.start`
 * whose text is the submitted `[voice] ...` prompt and ends at its main-loop
 * `turn.complete`. Every tool call of the main loop in between is voice-originated,
 * and so is every subagent started in it (registered at `agent.spawn`, or at its first
 * tool call while the voice turn runs), including background subagents and their
 * own subagents after the voice turn has ended.
 *
 * The scope survives a hot reload through `snapshot()` / `restore()` (register.ts keeps the
 * snapshot in `$.state`). When the snapshot cannot be read, `assumeUnknownTurn()` treats the
 * running turn, and every subagent first seen during it, as voice-originated until the next
 * main-loop `turn.start` or `turn.complete` (fail-safe, but typed work is not blocked for good).
 */
export class VoiceScope {
  private mainTurn: string | null = null
  private mainIsVoice = false
  private unknownTurn = false
  private agents = new Set<string>()
  private typedAgents = new Set<string>() // started outside a voice turn: never voice
  /** Bumped on every change, so the owner knows when to save a snapshot. */
  revision = 0

  turnStarted(turnId: string, isVoice: boolean) {
    this.mainTurn = turnId
    this.mainIsVoice = isVoice
    this.unknownTurn = false
    this.revision++
  }

  turnCompleted(turnId: string) {
    if (this.unknownTurn) {
      // The turn that could not be classified has ended: back to normal.
      this.unknownTurn = false
      this.revision++
    }
    if (this.mainTurn === turnId) {
      this.mainTurn = null
      this.mainIsVoice = false
      this.revision++
    }
  }

  /**
   * Fail-safe after a reload whose saved scope could not be read, or whose transcript shows that
   * the running main turn came in by voice while the saved scope says otherwise.
   */
  assumeUnknownTurn() {
    this.unknownTurn = true
    this.revision++
  }

  private get guarded(): boolean {
    return this.mainIsVoice || this.unknownTurn
  }

  /** Whether a call (or spawn) made in this loop is voice-originated; learns new agents. */
  isVoice(agentId: string | undefined): boolean {
    if (agentId === undefined) return this.guarded
    if (this.agents.has(agentId)) return true
    if (this.typedAgents.has(agentId)) return false
    if (this.guarded) {
      this.addAgent(agentId) // a subagent first seen while the voice (or unknown) turn runs
      return true
    }
    this.addTypedAgent(agentId) // a background agent of a typed turn stays typed
    return false
  }

  addAgent(agentId: string) {
    if (this.agents.has(agentId)) return
    this.typedAgents.delete(agentId)
    this.agents.add(agentId) // never evicted (see MAX_TYPED_AGENTS)
    this.revision++
  }

  addTypedAgent(agentId: string) {
    if (this.agents.has(agentId) || this.typedAgents.has(agentId)) return
    this.typedAgents.add(agentId)
    trim(this.typedAgents)
    this.revision++
  }

  get voiceTurnRunning(): boolean {
    return this.guarded
  }

  get unknownTurnRunning(): boolean {
    return this.unknownTurn
  }

  snapshot(): ScopeSnapshot {
    return {
      v: 1,
      mainTurn: this.mainTurn,
      mainIsVoice: this.mainIsVoice,
      unknownTurn: this.unknownTurn,
      agents: [...this.agents],
      typedAgents: [...this.typedAgents],
    }
  }

  /** Takes over a saved scope; false (nothing changed) when `saved` is not one. */
  restore(saved: unknown): boolean {
    if (!isSnapshot(saved)) return false
    this.mainTurn = saved.mainTurn
    this.mainIsVoice = saved.mainIsVoice
    this.unknownTurn = saved.unknownTurn
    this.agents = new Set(saved.agents)
    this.typedAgents = new Set(saved.typedAgents.filter(a => !this.agents.has(a)))
    this.revision++
    return true
  }
}

function trim(set: Set<string>) {
  while (set.size > MAX_TYPED_AGENTS) {
    const oldest = set.values().next().value
    if (oldest === undefined) break
    set.delete(oldest)
  }
}

function isSnapshot(x: unknown): x is ScopeSnapshot {
  if (!x || typeof x !== 'object') return false
  const o = x as Record<string, unknown>
  const strings = (a: unknown) => Array.isArray(a) && a.every(s => typeof s === 'string')
  return (
    o.v === 1 &&
    (o.mainTurn === null || typeof o.mainTurn === 'string') &&
    typeof o.mainIsVoice === 'boolean' &&
    typeof o.unknownTurn === 'boolean' &&
    strings(o.agents) &&
    strings(o.typedAgents)
  )
}

export function denyMessage(reason: string): string {
  return (
    `voice: this action (${reason}) was blocked because it is part of a request that came in by voice. ` +
    'If it is really needed, ask the user to request it directly from the keyboard. ' +
    'Mention this briefly in your final answer.'
  )
}
