// Guard rules and voice scope, as pure functions.
import { describe, expect, test } from 'claude-code/testing'
import { MAX_TYPED_AGENTS, VoiceScope, checkCommand, checkVoiceToolCall } from '../hooks/guard.ts'
import { NOTICE, isVoicePrompt, promptFor } from '../hooks/prompt.ts'

const DENY_COMMANDS = [
  'rm -rf build',
  'rm -r ./tmp',
  'rm -fr x',
  'rm --recursive x',
  'cd x && rm -R y',
  'del /s /q *.log',
  'rmdir /s /q old',
  'rd /S foo',
  'Remove-Item -Recurse -Force .\\dist',
  'Remove-Item .\\dist -r',
  'ri x -Recurse',
  'format d: /q',
  'Format-Volume -DriveLetter D',
  'git clean -fdx',
  'git clean --force',
  'git reset --hard HEAD~1',
  'git push origin main',
  'git -C repo push',
  'gh pr create --fill',
  'gh pr merge 12 --squash',
  'gh issue create -t bug',
  'gh release create v1',
  'gh repo edit --visibility public',
  'gh repo delete me/x --yes',
  'gh api repos/x/y/issues -X POST -f title=a',
  'gh api -X DELETE repos/x/y',
  'gh api repos/x/y/labels --method PATCH',
  'gh api repos/x/y/issues -f title=a',
  'npm publish',
  'pnpm publish --access public',
  'uv publish',
  'twine upload dist/*',
  'cargo publish',
  'docker push me/img',
  'Send-MailMessage -To a@b -Subject x',
  'curl -X POST https://hooks.slack.com/x -d "{}"',
  'curl --data-binary @a.json https://api.x',
  'Invoke-RestMethod -Uri https://x -Method Post -Body $b',
  'schtasks /create /tn x /tr y /sc daily',
  'Register-ScheduledTask -TaskName x',
  'crontab -e',
]

const ALLOW_COMMANDS = [
  'ls -la',
  'Get-ChildItem -Recurse C:\\work',
  'rm build/tmp.txt',
  'Remove-Item .\\a.txt',
  'git status',
  'git log --oneline -5',
  'git diff HEAD~1',
  'git reset HEAD file.txt',
  'git clean -n',
  'gh pr list',
  'gh pr view 12',
  'gh issue list',
  'gh api repos/x/y/issues',
  'npm test',
  'npm run build',
  'curl -s https://example.com',
  'Invoke-WebRequest https://example.com',
  'schtasks /query',
  'echo "rm -rf is dangerous" > note.txt', // quoted text in one segment: still denied? see below
]

describe('checkCommand', () => {
  test('denies destructive and outward commands', () => {
    for (const c of DENY_COMMANDS) expect([c, checkCommand(c) !== null]).toEqual([c, true])
  })
  test('allows read-only and local commands', () => {
    // The last one mentions `rm -rf` inside an echo: the guard errs on the safe side.
    for (const c of ALLOW_COMMANDS.slice(0, -1)) expect([c, checkCommand(c)]).toEqual([c, null])
    expect(checkCommand(ALLOW_COMMANDS[ALLOW_COMMANDS.length - 1] ?? '')).not.toBeNull()
  })
})

describe('checkVoiceToolCall', () => {
  test('shell tools go through the command rules', () => {
    expect(checkVoiceToolCall('Bash', { command: 'git push' })).not.toBeNull()
    expect(checkVoiceToolCall('PowerShell', { command: 'Remove-Item -Recurse x' })).not.toBeNull()
    expect(checkVoiceToolCall('Bash', { command: 'git status' })).toBeNull()
  })
  test('browser and computer use are denied', () => {
    for (const t of [
      'mcp__computer-use__left_click',
      'mcp__Claude_Browser__navigate',
      'mcp__claude-in-chrome__computer',
      'mcp__Control_Chrome__open_url',
    ])
      expect([t, checkVoiceToolCall(t, {}) !== null]).toEqual([t, true])
  })
  test('artifacts: publish and delete denied, read allowed', () => {
    expect(checkVoiceToolCall('Artifact', { file_path: 'a.html' })).not.toBeNull() // publish by default
    expect(checkVoiceToolCall('Artifact', { action: 'delete', url: 'u' })).not.toBeNull()
    expect(checkVoiceToolCall('Artifact', { action: 'read', url: 'u' })).toBeNull()
    expect(checkVoiceToolCall('Artifact', { action: 'list' })).toBeNull()
  })
  test('scheduled tasks and triggers are denied', () => {
    expect(checkVoiceToolCall('mcp__scheduled-tasks__create_scheduled_task', {})).not.toBeNull()
    expect(checkVoiceToolCall('mcp__scheduled-tasks__run_scheduled_task', {})).not.toBeNull()
    expect(checkVoiceToolCall('mcp__scheduled-tasks__list_scheduled_tasks', {})).toBeNull()
    expect(checkVoiceToolCall('CronCreate', {})).not.toBeNull()
    expect(checkVoiceToolCall('RemoteTrigger', {})).not.toBeNull()
    expect(checkVoiceToolCall('Skill', { skill: 'anthropic-skills:schedule' })).not.toBeNull()
    expect(checkVoiceToolCall('Skill', { skill: 'pdf' })).toBeNull()
  })
  test('MCP tools that send, post, publish, delete, trash, share or create events', () => {
    for (const t of [
      'mcp__gmail__send_email',
      'mcp__slack__post_message',
      'mcp__x__publish_page',
      'mcp__drive__trash_file',
      'mcp__drive__share_file',
      'mcp__calendar__create_event',
      'mcp__ccd_session_mgmt__delete_session',
      'mcp__ccd_session_mgmt__send_message',
      'mcp__ccd_settings__set_setting',
    ])
      expect([t, checkVoiceToolCall(t, {}) !== null]).toEqual([t, true])
    for (const t of ['mcp__drive__read_file_content', 'mcp__drive__search_files', 'mcp__postgres__query'])
      expect([t, checkVoiceToolCall(t, {})]).toEqual([t, null])
  })
  test('ordinary tools are allowed', () => {
    expect(checkVoiceToolCall('Read', { file_path: 'a' })).toBeNull()
    expect(checkVoiceToolCall('Grep', { pattern: 'x' })).toBeNull()
    expect(checkVoiceToolCall('Edit', { file_path: 'a', old_string: 'x', new_string: 'y' })).toBeNull()
  })
})

describe('VoiceScope', () => {
  test('main loop, subagents and background agents of a voice turn', () => {
    const s = new VoiceScope()
    expect(s.isVoice(undefined)).toBe(false)
    s.turnStarted('U1', false) // typed
    expect(s.isVoice(undefined)).toBe(false)
    expect(s.isVoice('typed-agent')).toBe(false)
    s.turnCompleted('U1')

    s.turnStarted('V1', true) // voice
    expect(s.isVoice(undefined)).toBe(true)
    expect(s.isVoice('sub1')).toBe(true) // first seen during the voice turn
    s.addAgent('bg1') // spawned (background) in the voice turn
    s.turnCompleted('V1')

    s.turnStarted('U2', false) // typed turn afterwards
    expect(s.isVoice(undefined)).toBe(false)
    expect(s.isVoice('sub1')).toBe(true) // still the voice turn's agent
    expect(s.isVoice('bg1')).toBe(true)
    expect(s.isVoice('typed-sub')).toBe(false) // a new agent of the typed turn
    s.turnCompleted('U2')
  })

  test('a background agent of a typed turn stays typed during a later voice turn', () => {
    const s = new VoiceScope()
    s.turnStarted('U1', false)
    s.addTypedAgent('bg-typed') // spawned in the typed turn, runs on in the background
    s.turnCompleted('U1')
    s.turnStarted('V1', true)
    expect(s.isVoice('bg-typed')).toBe(false)
    s.turnCompleted('V1')

    const t = new VoiceScope()
    t.turnStarted('U1', false)
    expect(t.isVoice('seen-typed')).toBe(false) // its first tool call in the typed turn
    t.turnCompleted('U1')
    t.turnStarted('V1', true)
    expect(t.isVoice('seen-typed')).toBe(false) // remembered as typed
    t.turnCompleted('V1')
  })

  test('voice agents are never evicted, however many there are (R3)', () => {
    const s = new VoiceScope()
    s.turnStarted('V1', true)
    s.addAgent('old-bg') // a background agent of the voice turn, still running later
    for (let i = 0; i < 1200; i++) s.addAgent(`voice-${i}`)
    s.turnCompleted('V1')
    s.turnStarted('U1', false) // typed turn afterwards
    expect(s.isVoice('old-bg')).toBe(true)
    expect(s.isVoice('voice-0')).toBe(true)
    expect(s.snapshot().agents.length).toBe(1201)
    // The snapshot stays small: about 1,200 ids in well under 64 KB of JSON.
    expect(JSON.stringify(s.snapshot()).length < 64 * 1024).toBe(true)
    // ...and survives a reload.
    const t = new VoiceScope()
    expect(t.restore(JSON.parse(JSON.stringify(s.snapshot())))).toBe(true)
    expect(t.isVoice('old-bg')).toBe(true)
  })

  test('typed agents are capped; an evicted one is classified afresh, voice-side during a voice turn', () => {
    const s = new VoiceScope()
    s.turnStarted('U1', false)
    s.addTypedAgent('typed-old')
    for (let i = 0; i < MAX_TYPED_AGENTS; i++) s.addTypedAgent(`typed-${i}`)
    expect(s.snapshot().typedAgents.length).toBe(MAX_TYPED_AGENTS)
    expect(s.snapshot().typedAgents.includes('typed-old')).toBe(false) // evicted
    expect(s.isVoice('typed-old')).toBe(false) // seen again outside a voice turn: typed
    s.turnCompleted('U1')

    const t = new VoiceScope()
    t.turnStarted('U1', false)
    t.addTypedAgent('typed-old')
    for (let i = 0; i < MAX_TYPED_AGENTS; i++) t.addTypedAgent(`typed-${i}`)
    t.turnCompleted('U1')
    t.turnStarted('V1', true)
    expect(t.isVoice('typed-old')).toBe(true) // unknown again, seen in a voice turn: the safe side
    expect(t.isVoice('typed-1')).toBe(false) // still remembered as typed
  })
})

describe('isVoicePrompt', () => {
  test('the marker is MARK at a line start plus the notice block', () => {
    expect(isVoicePrompt(promptFor('x'))).toBe(true)
    expect(isVoicePrompt(promptFor('x', ['y']))).toBe(true)
    expect(isVoicePrompt('earlier queued text\n' + promptFor('x'))).toBe(true) // merged queued prompts
    expect(isVoicePrompt('[voice] x')).toBe(false)
    expect(isVoicePrompt(NOTICE)).toBe(false)
    expect(isVoicePrompt('a [voice] x\n' + NOTICE)).toBe(false) // MARK not at a line start
    expect(isVoicePrompt('git push')).toBe(false)
  })
})
