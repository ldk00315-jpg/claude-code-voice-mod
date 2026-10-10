// The prompt submitted to the session for one voice request, and how a turn is recognised as one.
export const MARK = '[voice] '

/**
 * The notice block every voice prompt carries. Together with MARK at the start of a line it
 * is the voice marker that `isVoicePrompt` looks for. Keep it on one line and change it only
 * together with the tests.
 */
export const NOTICE =
  '(This request came in through the voice front desk. Your answer will be summarised into one or two ' +
  'spoken sentences, so put the conclusion first and keep it short. In this turn, deleting, pushing, ' +
  'sending, publishing and browser control are blocked. Answer in the language of the request.)'

export function promptFor(text: string, userSaid: string[] = []): string {
  const heard = userSaid.length ? `\n(What the user said, as transcribed: ${userSaid.map(s => `"${s}"`).join(' ')})` : ''
  return `${MARK}${text.trim()}${heard}\n${NOTICE}`
}

const MARK_AT_LINE_START = /(^|\n)\[voice\] /

/**
 * Whether a turn's text carries the voice marker: a line starting with MARK, and the NOTICE block.
 * Checked at turn.start independently of the job queue, so a voice prompt that is still queued
 * in Claude Code after its job timed out, or after `/voice off`, is still guarded when it runs.
 * Fail-safe by design: a typed prompt that happens to contain both (for example a pasted voice
 * prompt) is guarded too; retype the request without the marker to run it unguarded.
 */
export function isVoicePrompt(text: string): boolean {
  return text.includes(NOTICE) && MARK_AT_LINE_START.test(text)
}
