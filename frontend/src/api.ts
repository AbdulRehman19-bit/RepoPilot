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

const TOKEN_KEY = 'repopilot_api_token'

export function getToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setToken(token: string): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token)
    else sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    // Storage can be blocked (private windows); the token then lasts until reload only.
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers = new Headers(init.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (init.body) headers.set('Content-Type', 'application/json')
  const response = await fetch(`/api${path}`, { ...init, headers })
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

export interface Execution {
  id: number
  status: string
  kind: string
  selectors: string[]
  failures: { node_id: string; message: string }[]
  error: string | null
  reproduced: boolean | null
}

export interface Attempt {
  number: number
  explanation: string
  outcome: string
  detail: string
}

export interface TestSummary {
  passed: number
  failed: number
}

export interface Patch {
  id: number
  issue_id: number
  status: string
  model_name: string | null
  explanation: string | null
  diff: string | null
  attempts: Attempt[]
  error: string | null
  before: { selectors: string[]; failing: string[]; output: string } | null
  after: { targeted: TestSummary | null; full: TestSummary | null } | null
  review_status: 'approved' | 'rejected' | null
  reviewer: string | null
  review_note: string | null
  pr_url: string | null
  pr_error: string | null
}

export const reproduceIssue = (id: number) =>
  request<Execution>(`/issues/${id}/reproduce`, { method: 'POST' })
export const fetchExecution = (id: number) => request<Execution>(`/executions/${id}`)
export const startPatch = (issueId: number) =>
  request<Patch>(`/issues/${issueId}/patch`, { method: 'POST' })
export const fetchPatches = (issueId: number) => request<Patch[]>(`/issues/${issueId}/patches`)

export interface Review {
  reviewer?: string
  note?: string
}
const review = (id: number, action: string, body?: Review) =>
  request<Patch>(`/patches/${id}/${action}`, {
    method: 'POST',
    body: body ? JSON.stringify(body) : undefined,
  })
export const approvePatch = (id: number, body: Review) => review(id, 'approve', body)
export const rejectPatch = (id: number, body: Review) => review(id, 'reject', body)
export const retryOpenPr = (id: number) => review(id, 'open-pr')
