import { FormEvent, useEffect, useRef, useState } from 'react'
import { LinearProgress, Typography } from '@mui/material'
import { authConfigured, bearerToken, confirmRegistration, currentUsername, login, logout, register } from './auth'
import { ResultView, type DocumentResult } from './ResultView'
type Job = {
  job_id: string
  status: 'UPLOAD_PENDING' | 'UPLOADED' | 'QUEUED' | 'PROCESSING' | 'COMPLETED' | 'FAILED' | 'DELETING'
  filename?: string
  created_at?: string
  updated_at?: string
  error?: string
  last_error?: string
  pollError?: string
  deleteError?: string
  result_unavailable?: boolean
  result_message?: string
  result?: DocumentResult
  current_page?: number
  total_pages?: number
  progress_percent?: number
}
type JobsResponse = { jobs: Job[]; count?: number; has_more?: boolean; limit?: number }
type CreatedJob = {
  job_id: string
  status: string
  upload_url: string
  upload_headers: Record<string, string>
  count?: number
  limit?: number
}

const apiBase = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '')
const terminalStatuses = new Set(['COMPLETED', 'FAILED', 'DELETING'])

function App() {
  const [username, setUsername] = useState<string | null>(null)
  const [authMode, setAuthMode] = useState<'login' | 'register' | 'confirm'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmationCode, setConfirmationCode] = useState('')
  const [authMessage, setAuthMessage] = useState('')
  const [authError, setAuthError] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [prompt, setPrompt] = useState('')
  const [jobs, setJobs] = useState<Job[]>([])
  const [jobCount, setJobCount] = useState<number | null>(null)
  const [jobLimit, setJobLimit] = useState(20)
  const [deletingJobs, setDeletingJobs] = useState<Set<string>>(new Set())
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const timers = useRef<Map<string, number>>(new Map())
  const polling = useRef<Set<string>>(new Set())
  const stoppedPolling = useRef<Set<string>>(new Set())
  const authGeneration = useRef(0)

  useEffect(() => {
    let active = true
    void currentUsername().then((current) => {
      if (active) setUsername(current)
    })
    return () => { active = false }
  }, [])

  function clearJobActivity() {
    authGeneration.current += 1
    timers.current.forEach((timer) => window.clearTimeout(timer))
    timers.current.clear()
    polling.current.clear()
    stoppedPolling.current.clear()
  }

  useEffect(() => {
    clearJobActivity()
    setJobs([])
    setJobCount(null)
    setDeletingJobs(new Set())
    setError('')
    if (!username) return

    const generation = authGeneration.current
    async function restoreJobs() {
      if (!apiBase) {
        setError('Configura VITE_API_BASE_URL en frontend/.env.local con la URL de JobsApi.')
        return
      }
      try {
        const token = await bearerToken()
        const response = await fetch(`${apiBase}/jobs`, { headers: { Authorization: token } })
        if (!response.ok) throw new Error(await responseError(response))
        const data: JobsResponse = await response.json()
        if (authGeneration.current !== generation) return
        const restored = Array.isArray(data.jobs) ? data.jobs : []
        setJobs(restored)
        setJobCount(typeof data.count === 'number' ? data.count : restored.length)
        setJobLimit(typeof data.limit === 'number' ? data.limit : 20)
        for (const job of restored) {
          if (!terminalStatuses.has(job.status)) void poll(job.job_id, generation)
          else if (job.status === 'COMPLETED') void poll(job.job_id, generation)
        }
      } catch (err) {
        if (authGeneration.current === generation) {
          setError(err instanceof Error ? err.message : 'No se pudo restaurar el historial de trabajos')
        }
      }
    }

    void restoreJobs()
    return () => {
      clearJobActivity()
    }
  }, [username])

  function updateJob(jobId: string, update: (current: Job) => Job) {
    setJobs((current) => current.map((item) => item.job_id === jobId ? update(item) : item))
  }

  function schedulePoll(jobId: string, generation: number) {
    if (authGeneration.current !== generation || stoppedPolling.current.has(jobId) || timers.current.has(jobId)) return
    const timer = window.setTimeout(() => {
      timers.current.delete(jobId)
      void poll(jobId, generation)
    }, 2500)
    timers.current.set(jobId, timer)
  }

  async function poll(jobId: string, generation = authGeneration.current) {
    if (authGeneration.current !== generation || stoppedPolling.current.has(jobId) || polling.current.has(jobId) || timers.current.has(jobId)) return
    polling.current.add(jobId)
    try {
      const token = await bearerToken()
      const response = await fetch(`${apiBase}/jobs/${encodeURIComponent(jobId)}`, {
        headers: { Authorization: token },
      })
      if (!response.ok) throw new Error(await responseError(response))
      const updated: Job = await response.json()
      if (authGeneration.current !== generation) return
      setJobs((current) => current.map((item) => item.job_id === jobId ? updated : item))
      if (!terminalStatuses.has(updated.status) && updated.status !== 'UPLOAD_PENDING') {
        schedulePoll(jobId, generation)
      } else {
        timers.current.delete(jobId)
      }
    } catch (err) {
      if (authGeneration.current !== generation) return
      updateJob(jobId, (current) => ({
        ...current,
        pollError: err instanceof Error ? err.message : 'No se pudo consultar el trabajo',
      }))
      schedulePoll(jobId, generation)
    } finally {
      polling.current.delete(jobId)
    }
  }

  async function submitAuth(event: FormEvent) {
    event.preventDefault()
    setAuthError('')
    setAuthMessage('')
    try {
      if (authMode === 'register') {
        const result = await register(email, password)
        if (result.nextStep.signUpStep === 'CONFIRM_SIGN_UP') {
          setAuthMode('confirm')
          setAuthMessage('Te enviamos un código de confirmación al correo.')
        } else {
          setAuthMode('login')
          setAuthMessage('La cuenta está lista. Inicia sesión.')
        }
      } else if (authMode === 'confirm') {
        await confirmRegistration(email, confirmationCode)
        setAuthMode('login')
        setPassword('')
        setAuthMessage('Cuenta confirmada. Ya puedes iniciar sesión.')
      } else {
        const result = await login(email, password)
        if (!result.isSignedIn) throw new Error('Cognito requiere un paso de acceso adicional no configurado en esta aplicación.')
        setError('')
        setUsername(await currentUsername())
        setPassword('')
      }
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : 'No se pudo completar la autenticación.')
    }
  }

  async function doLogout() {
    clearJobActivity()
    setJobs([])
    setJobCount(null)
    setUsername(null)
    await logout()
  }

  async function deleteJob(jobId: string) {
    if (!window.confirm('¿Eliminar este trabajo, su PDF y el resultado almacenado?')) return
    if (!apiBase) return
    const generation = authGeneration.current
    setDeletingJobs((current) => new Set(current).add(jobId))
    updateJob(jobId, (current) => ({ ...current, deleteError: undefined }))
    try {
      const token = await bearerToken()
      const response = await fetch(`${apiBase}/jobs/${encodeURIComponent(jobId)}`, {
        method: 'DELETE',
        headers: { Authorization: token },
      })
      if (!response.ok) throw new Error(await responseError(response))
      if (authGeneration.current !== generation) return
      const data: { count?: number; limit?: number } = await response.json()
      if (authGeneration.current !== generation) return
      stoppedPolling.current.add(jobId)
      const timer = timers.current.get(jobId)
      if (timer !== undefined) window.clearTimeout(timer)
      timers.current.delete(jobId)
      setJobs((current) => current.filter((item) => item.job_id !== jobId))
      setJobCount((current) => typeof data.count === 'number'
        ? data.count
        : current === null ? null : Math.max(0, current - 1))
      if (typeof data.limit === 'number') setJobLimit(data.limit)
    } catch (err) {
      if (authGeneration.current === generation) {
        updateJob(jobId, (current) => ({
          ...current,
          deleteError: err instanceof Error ? err.message : 'No se pudo eliminar el trabajo',
        }))
      }
    } finally {
      if (authGeneration.current === generation) {
        setDeletingJobs((current) => {
          const next = new Set(current)
          next.delete(jobId)
          return next
        })
      }
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError('')
    const selectedFile = file
    const selectedPrompt = prompt
    if (!apiBase) {
      setError('Configura VITE_API_BASE_URL en frontend/.env.local con la URL de JobsApi.')
      return
    }
    if (!selectedFile) {
      setError('Selecciona un archivo PDF.')
      return
    }
    if (selectedFile.size === 0) {
      setError('El PDF está vacío.')
      return
    }
    setUploading(true)
    const generation = authGeneration.current
    try {
      const token = await bearerToken()
      const createdResponse = await fetch(`${apiBase}/jobs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: token },
        body: JSON.stringify({ filename: selectedFile.name, content_type: selectedFile.type || 'application/pdf', file_size: selectedFile.size, user_prompt: selectedPrompt }),
      })
      if (!createdResponse.ok) throw new Error(await responseError(createdResponse))
      const created: CreatedJob = await createdResponse.json()
      if (authGeneration.current !== generation) return
      setJobs((current) => [{ job_id: created.job_id, status: created.status as Job['status'] }, ...current])
      setJobCount(typeof created.count === 'number' ? created.count : (current) => (current ?? 0) + 1)
      if (typeof created.limit === 'number') setJobLimit(created.limit)
      const uploadResponse = await fetch(created.upload_url, {
        method: 'PUT',
        headers: created.upload_headers,
        body: selectedFile,
      })
      if (!uploadResponse.ok) throw new Error(`La carga a S3 falló (${uploadResponse.status}).`)
      if (authGeneration.current === generation) void poll(created.job_id, generation)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'No se pudo iniciar el procesamiento')
    } finally {
      setUploading(false)
    }
  }

  return (
    <main className="container">
      <header>
        <p className="eyebrow">AWS · Ollama · Qwen3-VL</p>
        <h1>Análisis de documentos</h1>
        <p className="intro">Carga un PDF para analizar sus páginas y obtener una consolidación del documento.</p>
      </header>

      {!authConfigured ? (
        <section className="panel error" role="alert">Configura VITE_COGNITO_USER_POOL_ID y VITE_COGNITO_USER_POOL_CLIENT_ID en frontend/.env.local.</section>
      ) : !username ? (
        <form className="panel form" onSubmit={submitAuth}>
          <h2>{authMode === 'register' ? 'Crear cuenta' : authMode === 'confirm' ? 'Confirmar cuenta' : 'Iniciar sesión'}</h2>
          <label htmlFor="auth-email">Correo electrónico</label>
          <input id="auth-email" type="email" autoComplete="email" required value={email} onChange={(event) => setEmail(event.target.value)} />
          {authMode === 'confirm' ? <>
            <label htmlFor="confirmation-code">Código de confirmación</label>
            <input id="confirmation-code" inputMode="numeric" required value={confirmationCode} onChange={(event) => setConfirmationCode(event.target.value)} />
          </> : <>
            <label htmlFor="auth-password">Contraseña</label>
            <input id="auth-password" type="password" autoComplete={authMode === 'register' ? 'new-password' : 'current-password'} minLength={12} required value={password} onChange={(event) => setPassword(event.target.value)} />
          </>}
          <button type="submit">{authMode === 'register' ? 'Registrarse' : authMode === 'confirm' ? 'Confirmar cuenta' : 'Iniciar sesión'}</button>
          {authMode !== 'confirm' && <button type="button" className="secondary" onClick={() => { setAuthMode(authMode === 'login' ? 'register' : 'login'); setAuthError(''); setAuthMessage('') }}>
            {authMode === 'login' ? 'Crear cuenta' : 'Ya tengo una cuenta'}
          </button>}
          {authMode === 'confirm' && <button type="button" className="secondary" onClick={() => setAuthMode('login')}>Volver al inicio de sesión</button>}
          {authMessage && <p role="status">{authMessage}</p>}
          {authError && <p className="error" role="alert">{authError}</p>}
        </form>
      ) : <>
      <div className="account-line"><span>Sesión: {username}</span><button type="button" className="secondary" onClick={() => void doLogout()}>Cerrar sesión</button></div>

      <form className="panel form" onSubmit={submit}>
        <label htmlFor="pdf">Documento PDF</label>
        <input
          id="pdf"
          type="file"
          accept="application/pdf,.pdf"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
        {file && <p className="file-info">{file.name} · {formatBytes(file.size)}</p>}

        <label htmlFor="prompt">Instrucción opcional</label>
        <textarea
          id="prompt"
          rows={3}
          maxLength={4000}
          placeholder="Por ejemplo: identifica todas las cantidades económicas."
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
        />
        <button type="submit" disabled={uploading || !file}>{uploading ? 'Subiendo…' : 'Cargar y analizar'}</button>
        {error && <p className="error" role="alert">{error}</p>}
      </form>

      {jobCount !== null && <p className="file-info quota-count">{jobCount} / {jobLimit} documentos</p>}

      {jobs.map((job) => (
        <section className="panel" aria-live="polite" key={job.job_id}>
          <div className="status-line">
            <h2>Trabajo</h2>
            <span className={`status status-${job.status.toLowerCase()}`}>{job.status}</span>
          </div>
          <p className="job-id">ID: {job.job_id}</p>
          {job.filename && <p className="file-info">{job.filename}</p>}
          {job.status === 'PROCESSING' && typeof job.total_pages === 'number' && job.total_pages > 0 && <div className="job-progress" aria-label="Progreso del procesamiento">
            <Typography variant="body2" color="text.secondary">Procesando documento · Página {job.current_page ?? 0} de {job.total_pages} · {job.progress_percent ?? 0}%</Typography>
            <LinearProgress variant="determinate" value={Math.max(0, Math.min(100, job.progress_percent ?? 0))} />
          </div>}
          <button type="button" className="secondary" disabled={deletingJobs.has(job.job_id)} onClick={() => void deleteJob(job.job_id)}>
            {deletingJobs.has(job.job_id) ? 'Eliminando…' : 'Eliminar'}
          </button>
          {job.status === 'FAILED' && <p className="error">{job.error || job.last_error || 'El procesamiento falló. Consulta los logs del worker.'}</p>}
          {job.status === 'UPLOAD_PENDING' && <p>La subida quedó pendiente. Selecciona nuevamente el archivo para iniciar otro trabajo; no se genera una URL de subida nueva automáticamente.</p>}
          {job.status === 'DELETING' && <p>La eliminación quedó pendiente. Pulsa Eliminar para reintentar.</p>}
          {job.deleteError && <p className="error" role="alert">{job.deleteError}</p>}
          {job.pollError && <p className="error" role="alert">Error consultando el estado: {job.pollError}. Se volverá a intentar.</p>}
          {job.result_unavailable && <p className="file-info" role="status">{job.result_message || 'El resultado almacenado ya no está disponible.'}</p>}
          {job.result && <ResultView result={job.result} status={job.status} />}
        </section>
      ))}
      </>}
    </main>
  )
}

async function responseError(response: Response) {
  try {
    const body = await response.json()
    return body.error || body.detail || `Error HTTP ${response.status}`
  } catch {
    return `Error HTTP ${response.status}`
  }
}

function formatBytes(bytes: number) {
  return new Intl.NumberFormat('es', { maximumFractionDigits: 1 }).format(bytes / 1024 / 1024) + ' MB'
}

export default App
