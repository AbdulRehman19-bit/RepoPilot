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
