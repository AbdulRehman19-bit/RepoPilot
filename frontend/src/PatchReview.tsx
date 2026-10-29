import { useCallback, useEffect, useState } from 'react'
import {
  approvePatch,
  fetchExecution,
  fetchPatches,
  rejectPatch,
  reproduceIssue,
  retryOpenPr,
  startPatch,
  type Execution,
  type Patch,
} from './api'

const POLL_MS = 3000
const ACTIVE = new Set(['queued', 'running'])

function Diff({ text }: { text: string }) {
  return (
    <pre className="diff">
      {text.split('\n').map((line, i) => {
        const kind = line.startsWith('+') && !line.startsWith('+++')
          ? 'add'
          : line.startsWith('-') && !line.startsWith('---')
            ? 'del'
            : ''
        return (
          <div key={i} className={kind}>
            {line}
          </div>
        )
      })}
    </pre>
  )
}

function Reproduction({ issueId }: { issueId: number }) {
  const [execution, setExecution] = useState<Execution | null>(null)
  const [error, setError] = useState<string | null>(null)

  const running = execution !== null && (execution.status === 'queued' || execution.status === 'running')
  useEffect(() => {
    if (!running || !execution) return
    const timer = setInterval(() => {
      fetchExecution(execution.id).then(setExecution).catch((e: Error) => setError(e.message))
    }, 2000)
    return () => clearInterval(timer)
  }, [running, execution])

  async function reproduce() {
    setError(null)
    try {
      setExecution(await reproduceIssue(issueId))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="step">
      <button onClick={reproduce} disabled={running}>
        {running ? 'Reproducing…' : 'Reproduce in sandbox'}
      </button>
      {error && <span role="alert"> {error}</span>}
      {execution?.status === 'error' && <span role="alert"> {execution.error}</span>}
      {execution?.status === 'completed' && (
        <span>
          {' '}
          {execution.reproduced
            ? `Reproduced: ${execution.failures.map((f) => f.node_id).join(', ')} fails.`
            : 'Not reproduced: the named tests pass.'}
        </span>
      )}
    </div>
  )
}

function PatchCard({ patch, onChange }: { patch: Patch; onChange: () => void }) {
  const [reviewer, setReviewer] = useState('')
  const [note, setNote] = useState('')
  const [error, setError] = useState<string | null>(patch.pr_error)
  const [busy, setBusy] = useState(false)

  async function act(action: () => Promise<Patch>, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
      onChange()
    }
  }

  const body = { reviewer: reviewer || undefined, note: note || undefined }
  const reviewable = patch.status === 'verified' && patch.review_status === null

  return (
    <article className="patch">
      <h3>
        Patch #{patch.id} <span className={`badge ${patch.status}`}>{patch.status}</span>
        {patch.review_status && <span className="badge">{patch.review_status}</span>}
      </h3>
      {patch.model_name && <p className="meta">Written by {patch.model_name}</p>}
      {ACTIVE.has(patch.status) && <p>Working… this can take several minutes on a CPU.</p>}
      {patch.error && <p role="alert">{patch.error}</p>}
      {patch.explanation && <p>{patch.explanation}</p>}

      {patch.status === 'verified' && patch.before && patch.after && (
        <table className="results">
          <thead>
            <tr>
              <th />
              <th>Before</th>
              <th>After</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Reproduction</td>
              <td>{patch.before.failing.length} failing</td>
              <td>{patch.after.targeted?.passed ?? '?'} passing</td>
            </tr>
            <tr>
              <td>Full test suite</td>
              <td>baseline</td>
              <td>{patch.after.full?.passed ?? '?'} passed, no new failures</td>
            </tr>
          </tbody>
        </table>
      )}

      {patch.diff && <Diff text={patch.diff} />}

      {patch.attempts.length > 0 && (
        <details open={patch.status === 'failed'}>
          <summary>{patch.attempts.length} attempt(s)</summary>
          <ol>
            {patch.attempts.map((a) => (
              <li key={a.number}>
                <strong>{a.outcome}</strong>
                {a.detail && ` — ${a.detail}`}
              </li>
            ))}
          </ol>
        </details>
      )}

      {reviewable && (
        <div className="review">
          <p className="meta">
            Approving creates a branch and a <strong>draft</strong> pull request on GitHub. Nothing is
            merged.
          </p>
          <input placeholder="Your name (ignored when signed in)" value={reviewer} onChange={(e) => setReviewer(e.target.value)} />
          <input placeholder="Note for the PR (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
          <button
            disabled={busy}
            onClick={() =>
              act(
                () => approvePatch(patch.id, body),
                'This creates a branch and a draft pull request on the repository. Continue?',
              )
            }
          >
            Approve and open draft PR
          </button>
          <button disabled={busy} onClick={() => act(() => rejectPatch(patch.id, body))}>
            Reject
          </button>
        </div>
      )}

      {patch.pr_url && (
        <p>
          Draft pull request:{' '}
          <a href={patch.pr_url} target="_blank" rel="noreferrer">
            {patch.pr_url}
          </a>
        </p>
      )}
      {patch.review_status === 'approved' && !patch.pr_url && (
        <p>
          <button disabled={busy} onClick={() => act(() => retryOpenPr(patch.id))}>
            Retry opening the pull request
          </button>
        </p>
      )}
      {patch.review_status === 'rejected' && patch.review_note && (
        <p className="meta">Rejected: {patch.review_note}</p>
      )}
      {error && <p role="alert">{error}</p>}
    </article>
  )
}

export default function PatchReview({ issueId }: { issueId: number }) {
  const [patches, setPatches] = useState<Patch[]>([])
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setPatches(await fetchPatches(issueId))
    } catch (e) {
      setError((e as Error).message)
    }
  }, [issueId])

  useEffect(() => {
    let cancelled = false
    fetchPatches(issueId)
      .then((result) => !cancelled && setPatches(result))
      .catch((e: Error) => !cancelled && setError(e.message))
    return () => {
      cancelled = true
    }
  }, [issueId])

  const polling = patches.some((p) => ACTIVE.has(p.status))
  useEffect(() => {
    if (!polling) return
    const timer = setInterval(load, POLL_MS)
    return () => clearInterval(timer)
  }, [polling, load])

  async function generate() {
    setError(null)
    try {
      await startPatch(issueId)
      await load()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <section>
      <h2>Fix</h2>
      <Reproduction issueId={issueId} />
      <div className="step">
        <button onClick={generate} disabled={polling}>
          Generate patch
        </button>
        {error && <span role="alert"> {error}</span>}
      </div>
      {patches.map((patch) => (
        <PatchCard key={patch.id} patch={patch} onChange={load} />
      ))}
    </section>
  )
}
