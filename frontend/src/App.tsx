import { useEffect, useState } from 'react'
import {
  UNAUTHORIZED_EVENT,
  fetchAuthConfig,
  fetchHealth,
  getToken,
  setToken,
  signOut,
  type AuthConfig,
  type Health,
} from './api'
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

function Account({ auth, onChange }: { auth: AuthConfig; onChange: () => void }) {
  if (auth.login) {
    return (
      <p className="meta">
        Signed in as <strong>{auth.login}</strong>{' '}
        <button
          onClick={() =>
            signOut()
              .catch(() => undefined)
              .finally(onChange)
          }
        >
          Sign out
        </button>
      </p>
    )
  }
  return (
    <>
      {auth.login_available && (
        // A full-page navigation: GitHub redirects back here with the session cookie set.
        <p>
          <a href="/api/auth/login">Sign in with GitHub</a>
        </p>
      )}
      {auth.token_required && <TokenField onApply={onChange} />}
      {auth.open && (
        <p className="meta">Development mode: no sign-in is configured, so approvals are disabled.</p>
      )}
    </>
  )
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [auth, setAuth] = useState<AuthConfig | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Bumping this remounts the data views so they refetch after signing in or out.
  const [version, setVersion] = useState(0)

  useEffect(() => {
    fetchHealth().then(setHealth).catch((e: Error) => setError(e.message))
  }, [])
  useEffect(() => {
    fetchAuthConfig().then(setAuth).catch((e: Error) => setError(e.message))
  }, [version])
  // A refused call usually means the session ended. Re-check who we are (this does not remount
  // the data views, so it cannot loop); if we are signed out the sign-in prompt replaces them.
  useEffect(() => {
    const recheck = () => fetchAuthConfig().then(setAuth).catch(() => undefined)
    window.addEventListener(UNAUTHORIZED_EVENT, recheck)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, recheck)
  }, [])

  const canUse = auth !== null && (auth.open || auth.login !== null || getToken() !== '')

  return (
    <main>
      <h1>RepoPilot</h1>
      <p>AI bug investigator for GitHub.</p>
      <p className="meta">
        API:{' '}
        {error ? `unreachable (${error})` : health ? `${health.status} (${health.environment})` : 'checking…'}
      </p>
      {auth && <Account auth={auth} onChange={() => setVersion((v) => v + 1)} />}
      {auth && !canUse && <p>Sign in to see your repositories. (Your session may have ended.)</p>}
      {canUse && (
        <>
          <RepositoryList key={`repos-${version}`} />
          <IssueInvestigator key={`issues-${version}`} />
        </>
      )}
    </main>
  )
}
