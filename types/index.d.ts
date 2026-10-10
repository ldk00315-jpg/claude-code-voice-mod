// The values this Mod keeps in `$.state` (the session's, kept across a hot reload of the module).

/** The voice-origin guard's scope (hooks/guard.ts VoiceScope), as saved. */
export type ScopeSnapshot = {
  v: 1
  mainTurn: string | null
  mainIsVoice: boolean
  /** A turn whose start the module did not see may be running: guard it (fail-safe). */
  unknownTurn: boolean
  agents: string[]
  typedAgents: string[]
}

/** The guard as it stands, so a reloaded module restores it before any tool call. */
export type GuardState = {
  scope: ScopeSnapshot
  /** Every prompt text this module submitted (newest last, capped): a second voice signal. */
  submitted: string[]
}

declare module 'claude-code' {
  interface PluginState {
    voice: { guard: GuardState }
  }
}
