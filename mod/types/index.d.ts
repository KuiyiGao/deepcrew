export type DeepcrewMember = { name: string; state: string; task: string | null; cost: number | null }

export type DeepcrewAsk = {
  id: string
  member: string
  kind: 'perm' | 'question'
  text: string
  detail: string
  options: string[]
  widening: string | null
}

export type DeepcrewCrew = {
  team: string
  page: string | null
  members: DeepcrewMember[]
  done: number
  tasks: number
  asks: DeepcrewAsk[]
  spent: number | null
  budget: number | null
  resting: boolean
}

declare module 'claude-code' {
  interface PluginState {
    deepcrew: { crew: DeepcrewCrew | null; told: string[] }
  }
}
