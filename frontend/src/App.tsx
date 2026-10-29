import { useEffect, useState } from 'react'
import { fetchHealth, getToken, setToken, type Health } from './api'
import IssueInvestigator from './IssueInvestigator'
import RepositoryList from './RepositoryList'

function TokenField({ onApply }: { onApply: () => void }) {
  const [value, setValue] = useState(getToken())
  return (
    <label className="meta">
      API token{' '}
      <input
        type="password"
        value={value}
        autoComplete="off"
        onChange={(e) => {
          setValue(e.target.value)
          setToken(e.target.value)
        }}
        onBlur={onApply}
      />
    </label>
  )
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Bumping this remounts the data views so they refetch with the newly entered token.
  const [tokenVersion, setTokenVersion] = useState(0)

  useEffect(() => {
    fetchHealth().then(setHealth).catch((e: Error) => setError(e.message))
  }, [])

  return (
    <main>
      <h1>RepoPilot</h1>
      <p>AI bug investigator for GitHub.</p>
      <p className="meta">
        API:{' '}
        {error ? `unreachable (${error})` : health ? `${health.status} (${health.environment})` : 'checking…'}
      </p>
      <TokenField onApply={() => setTokenVersion((v) => v + 1)} />
      <RepositoryList key={`repos-${tokenVersion}`} />
      <IssueInvestigator key={`issues-${tokenVersion}`} />
    </main>
  )
}
