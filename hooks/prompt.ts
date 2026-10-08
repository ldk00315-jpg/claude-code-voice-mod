// The prompt submitted to the session for one voice request.
export const MARK = '[voice] '

export function promptFor(text: string, userSaid: string[] = []): string {
  const heard = userSaid.length ? `\n(What the user said, as transcribed: ${userSaid.map(s => `"${s}"`).join(' ')})` : ''
  return (
    `${MARK}${text.trim()}${heard}\n` +
    '(This request came in through the voice front desk. Your answer will be summarised into one or two ' +
    'spoken sentences, so put the conclusion first and keep it short. In this turn, deleting, pushing, ' +
    'sending, publishing and browser control are blocked. Answer in the language of the request.)'
  )
}
