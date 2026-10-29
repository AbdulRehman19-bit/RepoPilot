export interface Health {
  status: string
  environment: string
}

export type IndexStatus = 'pending' | 'queued' | 'indexing' | 'ready' | 'failed'

export interface Repository {
  id: number
  full_name: string
  default_branch: string
  index_status: IndexStatus
  index_error: string | null
  indexed_commit: string | null
  file_count: number
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, init)
  if (!response.ok) {
    const detail = await response.json().then((body) => body.detail).catch(() => null)
    throw new Error(detail ?? `API returned ${response.status}`)
  }
  return response.json()
}

export const fetchHealth = () => request<Health>('/health')
export const fetchRepositories = () => request<Repository[]>('/repositories')
export const reindexRepository = (id: number) =>
  request<Repository>(`/repositories/${id}/reindex`, { method: 'POST' })

export interface Issue {
  id: number
  repository: string
  number: number
  title: string
}

export interface Match {
  path: string
  score: number
  explanation: string
  reasons: string[]
}

export interface ContextBlock {
  path: string
  start_line: number
  end_line: number
  label: string
  text: string
  tokens: number
  truncated: boolean
}

export interface Retrieval {
  issue: Issue
  signals: { paths: string[]; identifiers: string[]; error_types: string[] }
  matches: Match[]
  context: ContextBlock[]
  context_tokens: number
}

export const fetchIssues = () => request<Issue[]>('/issues')
export const investigateIssue = (id: number) =>
  request<Retrieval>(`/issues/${id}/retrieve`, { method: 'POST' })

export interface Analysis {
  summary: string
  suspected_cause: string
  confidence: 'low' | 'medium' | 'high'
  files: { path: string; why: string }[]
}

export interface Explained extends Retrieval {
  model_used: boolean
  model_name: string | null
  model_error: string | null
  analysis: Analysis | null
}

export const explainIssue = (id: number) =>
  request<Explained>(`/issues/${id}/explain`, { method: 'POST' })
