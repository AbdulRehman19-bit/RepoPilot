import { useCallback, useEffect, useState } from 'react'
import { fetchRepositories, reindexRepository, type Repository } from './api'

const POLL_MS = 3000
const IN_FLIGHT = new Set(['queued', 'indexing'])

export default function RepositoryList() {
  const [repos, setRepos] = useState<Repository[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setRepos(await fetchRepositories())
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    fetchRepositories()
      .then((result) => !cancelled && setRepos(result))
      .catch((e: Error) => !cancelled && setError(e.message))
    return () => {
      cancelled = true
    }
  }, [])

  const polling = repos?.some((repo) => IN_FLIGHT.has(repo.index_status)) ?? false
  useEffect(() => {
    if (!polling) return
    const timer = setInterval(load, POLL_MS)
    return () => clearInterval(timer)
  }, [polling, load])

  async function reindex(id: number) {
    try {
      await reindexRepository(id)
      await load()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  if (error && !repos) return <p role="alert">Could not load repositories: {error}</p>
  if (!repos) return <p>Loading repositories…</p>

  return (
    <section>
      <h2>Repositories</h2>
      {error && <p role="alert">{error}</p>}
      {repos.length === 0 ? (
        <p>No repositories connected yet. Install the RepoPilot GitHub App on a repository.</p>
      ) : (
        <ul className="repos">
          {repos.map((repo) => (
            <li key={repo.id}>
              <div>
                <strong>{repo.full_name}</strong>
                <span className={`badge ${repo.index_status}`}>{repo.index_status}</span>
                <div className="meta">
                  {repo.index_status === 'failed' && repo.index_error
                    ? repo.index_error
                    : `${repo.file_count} files indexed${
                        repo.indexed_commit ? ` @ ${repo.indexed_commit.slice(0, 7)}` : ''
                      }`}
                </div>
              </div>
              <button
                onClick={() => reindex(repo.id)}
                disabled={IN_FLIGHT.has(repo.index_status)}
              >
                Re-index
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
