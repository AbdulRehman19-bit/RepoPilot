import { useEffect, useState } from 'react'
import { fetchHealth, type Health } from './api'

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchHealth().then(setHealth).catch((e: Error) => setError(e.message))
  }, [])

  return (
    <main>
      <h1>RepoPilot</h1>
      <p>AI bug investigator for GitHub.</p>
      <p>
        API:{' '}
        {error ? `unreachable (${error})` : health ? `${health.status} (${health.environment})` : 'checking…'}
      </p>
    </main>
  )
}
