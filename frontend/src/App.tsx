import { FormEvent, useEffect, useRef, useState } from 'react'

type PageResult = { page: number; analysis: string }
type DocumentResult = {
  filename: string
  pages: number
  page_results: PageResult[]
  final_analysis: string
  user_prompt: string
}
type Job = {
  job_id: string
  status: 'UPLOAD_PENDING' | 'UPLOADED' | 'QUEUED' | 'PROCESSING' | 'COMPLETED' | 'FAILED'
  error?: string
  pollError?: string
  result?: DocumentResult
}
type CreatedJob = {
  job_id: string
  status: string
  upload_url: string
  upload_headers: Record<string, string>
}

const apiBase = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '')
const terminalStatuses = new Set(['COMPLETED', 'FAILED'])

function App() {
  const [file, setFile] = useState<File | null>(null)
  const [prompt, setPrompt] = useState('')
  const [jobs, setJobs] = useState<Job[]>([])
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const [openPage, setOpenPage] = useState<number | null>(1)
  const timers = useRef<Map<string, number>>(new Map())

  useEffect(() => () => {
    timers.current.forEach((timer) => window.clearTimeout(timer))
    timers.current.clear()
  }, [])

  function updateJob(jobId: string, update: (current: Job) => Job) {
    setJobs((current) => current.map((item) => item.job_id === jobId ? update(item) : item))
  }

  async function poll(jobId: string) {
    try {
      const response = await fetch(`${apiBase}/jobs/${encodeURIComponent(jobId)}`)
      if (!response.ok) throw new Error(await responseError(response))
      const updated: Job = await response.json()
      setJobs((current) => current.map((item) => item.job_id === jobId ? updated : item))
      if (!terminalStatuses.has(updated.status)) {
        timers.current.set(jobId, window.setTimeout(() => void poll(jobId), 2500))
      } else {
        timers.current.delete(jobId)
      }
    } catch (err) {
      updateJob(jobId, (current) => ({
        ...current,
        pollError: err instanceof Error ? err.message : 'No se pudo consultar el trabajo',
      }))
      timers.current.set(jobId, window.setTimeout(() => void poll(jobId), 2500))
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
    try {
      const createdResponse = await fetch(`${apiBase}/jobs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: selectedFile.name, content_type: selectedFile.type || 'application/pdf', file_size: selectedFile.size, user_prompt: selectedPrompt }),
      })
      if (!createdResponse.ok) throw new Error(await responseError(createdResponse))
      const created: CreatedJob = await createdResponse.json()
      setJobs((current) => [{ job_id: created.job_id, status: created.status as Job['status'] }, ...current])
      const uploadResponse = await fetch(created.upload_url, {
        method: 'PUT',
        headers: created.upload_headers,
        body: selectedFile,
      })
      if (!uploadResponse.ok) throw new Error(`La carga a S3 falló (${uploadResponse.status}).`)
      void poll(created.job_id)
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

      {jobs.map((job) => (
        <section className="panel" aria-live="polite" key={job.job_id}>
          <div className="status-line">
            <h2>Trabajo</h2>
            <span className={`status status-${job.status.toLowerCase()}`}>{job.status}</span>
          </div>
          <p className="job-id">ID: {job.job_id}</p>
          {job.status === 'FAILED' && <p className="error">{job.error || 'El procesamiento falló. Consulta los logs del worker.'}</p>}
          {job.pollError && <p className="error" role="alert">Error consultando el estado: {job.pollError}. Se volverá a intentar.</p>}
          {job.result && <Result result={job.result} openPage={openPage} setOpenPage={setOpenPage} />}
        </section>
      ))}
    </main>
  )
}

function Result({ result, openPage, setOpenPage }: {
  result: DocumentResult
  openPage: number | null
  setOpenPage: (page: number | null) => void
}) {
  return (
    <div className="results">
      <h2>Resultado final</h2>
      <p className="file-info">{result.filename} · {result.pages} página(s)</p>
      <div className="analysis">{result.final_analysis}</div>
      <h2>Análisis por página</h2>
      {result.page_results.map((page) => (
        <details key={page.page} open={openPage === page.page} onToggle={(event) => {
          if ((event.currentTarget as HTMLDetailsElement).open) setOpenPage(page.page)
          else if (openPage === page.page) setOpenPage(null)
        }}>
          <summary>Página {page.page}</summary>
          <div className="analysis">{page.analysis}</div>
        </details>
      ))}
    </div>
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
