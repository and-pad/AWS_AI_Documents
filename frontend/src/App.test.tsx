// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const authMocks = vi.hoisted(() => ({
  authConfigured: true,
  bearerToken: vi.fn(async () => 'test-id-token'),
  confirmRegistration: vi.fn(),
  currentUsername: vi.fn(async () => 'alice@example.com'),
  login: vi.fn(),
  logout: vi.fn(async () => undefined),
  register: vi.fn(),
}))

vi.mock('./auth', () => authMocks)

import App from './App'

function jsonResponse(body: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response
}

async function flushReact() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('job history restoration', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_API_BASE_URL', 'https://api.example.test')
    vi.stubGlobal('fetch', vi.fn())
    authMocks.currentUsername.mockResolvedValue('alice@example.com')
    authMocks.bearerToken.mockResolvedValue('test-id-token')
    authMocks.logout.mockResolvedValue(undefined)
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  it('restores completed jobs and fetches their result after app state is rebuilt', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/jobs')) {
        return jsonResponse({ jobs: [{ job_id: 'job-done', status: 'COMPLETED', filename: 'report.pdf' }], count: 1, limit: 20 })
      }
      return jsonResponse({
        job_id: 'job-done',
        status: 'COMPLETED',
        filename: 'report.pdf',
        result: { filename: 'report.pdf', pages: 1, page_results: [], final_analysis: 'Resultado restaurado', user_prompt: '' },
      })
    })

    render(<App />)
    expect(await screen.findByText('Resultado anterior')).toBeTruthy()
    expect(screen.getByText('Resultado restaurado')).toBeTruthy()
    expect(fetchMock.mock.calls.some(([input, init]) =>
      String(input).endsWith('/jobs') && JSON.stringify((init as RequestInit).headers).includes('test-id-token'),
    )).toBe(true)
    expect(fetchMock.mock.calls.some(([input, init]) =>
      String(input).endsWith('/jobs/job-done') && JSON.stringify((init as RequestInit).headers).includes('test-id-token'),
    )).toBe(true)
  })

  it('resumes polling for PROCESSING jobs and clears the timer and jobs on logout', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/jobs')) {
        return jsonResponse({ jobs: [{ job_id: 'job-running', status: 'PROCESSING', filename: 'running.pdf' }], count: 1, limit: 20 })
      }
      return jsonResponse({ job_id: 'job-running', status: 'PROCESSING', filename: 'running.pdf' })
    })

    render(<App />)
    await flushReact()
    expect(fetchMock.mock.calls.some(([input, init]) =>
      String(input).endsWith('/jobs/job-running') && JSON.stringify((init as RequestInit).headers).includes('test-id-token'),
    )).toBe(true)
    expect(vi.getTimerCount()).toBe(1)

    fireEvent.click(screen.getByRole('button', { name: 'Cerrar sesión' }))
    await flushReact()
    expect(screen.getByRole('heading', { name: 'Iniciar sesión' })).toBeTruthy()
    expect(screen.queryByText('ID: job-running')).toBeNull()
    expect(vi.getTimerCount()).toBe(0)

    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes('/jobs/job-running'))).toHaveLength(1)
  })

  it('keeps UPLOAD_PENDING visible without creating a new upload URL or polling repeatedly', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/jobs')) {
        return jsonResponse({ jobs: [{ job_id: 'job-pending', status: 'UPLOAD_PENDING', filename: 'pending.pdf' }], count: 1, limit: 20 })
      }
      return jsonResponse({ job_id: 'job-pending', status: 'UPLOAD_PENDING', filename: 'pending.pdf' })
    })

    render(<App />)
    await flushReact()
    expect(screen.getByText(/La subida quedó pendiente/)).toBeTruthy()
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes('/jobs/job-pending'))).toHaveLength(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('shows count and removes a job from the interface after confirmed deletion', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (_input, init) => {
      if (init?.method === 'DELETE') return jsonResponse({ deleted: true, count: 6, limit: 20 })
      return jsonResponse({
        jobs: [{ job_id: 'job-to-delete', status: 'FAILED', filename: 'failed.pdf' }],
        count: 7,
        limit: 20,
      })
    })
    vi.stubGlobal('confirm', vi.fn(() => true))

    render(<App />)
    expect(await screen.findByText('7 / 20 documentos')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Eliminar' }))

    expect(await screen.findByText('6 / 20 documentos')).toBeTruthy()
    expect(screen.queryByText('ID: job-to-delete')).toBeNull()
    expect(fetchMock.mock.calls.some(([input, init]) =>
      String(input).endsWith('/jobs/job-to-delete') && init?.method === 'DELETE',
    )).toBe(true)
  })

  it('shows the quota error and does not start an S3 upload', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input, init) => {
      if (init?.method === 'POST') {
        return jsonResponse({ error: 'Has alcanzado el límite de 20 documentos. Elimina uno para continuar.' }, 409)
      }
      return jsonResponse({ jobs: [], count: 20, limit: 20 })
    })

    render(<App />)
    expect(await screen.findByText('20 / 20 documentos')).toBeTruthy()
    expect(screen.queryByLabelText('Instrucción opcional')).toBeNull()
    fireEvent.change(screen.getByLabelText('Documento PDF'), {
      target: { files: [new File(['pdf'], 'twenty-first.pdf', { type: 'application/pdf' })] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Cargar y analizar' }))

    expect(await screen.findByText('Has alcanzado el límite de 20 documentos. Elimina uno para continuar.')).toBeTruthy()
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false)
    const createCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(JSON.parse(String((createCall?.[1] as RequestInit).body))).not.toHaveProperty('user_prompt')
  })

  it('renders structured result sections with independent expansion and text-only content', async () => {
    const fetchMock = vi.mocked(fetch)
    const hostileText = 'Texto literal <img src=x onerror=alert(1)>\nsegunda línea'
    fetchMock.mockImplementation(async (input) => {
      if (String(input).endsWith('/jobs')) {
        return jsonResponse({ jobs: [{ job_id: 'structured-job', status: 'COMPLETED', filename: 'oficio.pdf' }], count: 1, limit: 20 })
      }
      return jsonResponse({
        job_id: 'structured-job',
        status: 'COMPLETED',
        result: {
          filename: 'oficio.pdf',
          pages: 2,
          analysis: {
            document_type: 'Oficio administrativo',
            summary: 'Solicita información presupuestal.',
            key_points: ['Fecha: 7 de enero de 2025'],
          },
          transcription: { text: hostileText },
          visual_elements: { stamps: [], signatures: [], other_marks: [] },
          requested_output: { user_request: 'Identifica la fecha límite.', response: 'La fecha límite es el 15 de enero.' },
        },
      })
    })

    const { container } = render(<App />)
    expect(await screen.findByText('Oficio administrativo')).toBeTruthy()
    const analysis = screen.getByRole('button', { name: /Análisis/ })
    const transcription = screen.getByRole('button', { name: /Transcripción literal/ })
    const visuals = screen.getByRole('button', { name: /Elementos visuales/ })
    const requested = screen.getByRole('button', { name: /Respuesta solicitada/ })

    expect(analysis.getAttribute('aria-expanded')).toBe('true')
    expect(transcription.getAttribute('aria-expanded')).toBe('false')
    expect(visuals.getAttribute('aria-expanded')).toBe('false')
    expect(requested.getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(transcription)
    await waitFor(() => expect(transcription.getAttribute('aria-expanded')).toBe('true'))
    expect(container.textContent).toContain(hostileText)
    expect(container.querySelector('img')).toBeNull()

    fireEvent.click(visuals)
    await waitFor(() => expect(visuals.getAttribute('aria-expanded')).toBe('true'))
    expect(await screen.findByText('No se detectaron sellos.')).toBeTruthy()

    fireEvent.click(requested)
    await waitFor(() => expect(requested.getAttribute('aria-expanded')).toBe('true'))
    expect(await screen.findByText('La fecha límite es el 15 de enero.')).toBeTruthy()
    expect(screen.getByText(/Identifica la fecha límite/)).toBeTruthy()

    fireEvent.click(transcription)
    await waitFor(() => expect(transcription.getAttribute('aria-expanded')).toBe('false'))
    expect(visuals.getAttribute('aria-expanded')).toBe('true')
    expect(requested.getAttribute('aria-expanded')).toBe('true')
  })

  it('omits the requested answer section when response is null', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input) => String(input).endsWith('/jobs')
      ? jsonResponse({ jobs: [{ job_id: 'no-request', status: 'COMPLETED', filename: 'simple.pdf' }], count: 1, limit: 20 })
      : jsonResponse({
        job_id: 'no-request',
        status: 'COMPLETED',
        result: {
          filename: 'simple.pdf', pages: 1,
          analysis: { document_type: 'Carta', summary: 'Resumen', key_points: [] },
          transcription: { text: 'Texto' },
          visual_elements: { stamps: [], signatures: [], other_marks: [] },
          requested_output: { user_request: '', response: null },
        },
      }))

    render(<App />)
    expect(await screen.findByText('Resumen')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Respuesta solicitada/ })).toBeNull()
  })

  it('shows when a completed job has no stored result anymore', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input) => String(input).endsWith('/jobs')
      ? jsonResponse({ jobs: [{ job_id: 'expired-result', status: 'COMPLETED', filename: 'old.pdf' }], count: 1, limit: 20 })
      : jsonResponse({
        job_id: 'expired-result',
        status: 'COMPLETED',
        result_unavailable: true,
        result_message: 'El resultado almacenado ya no está disponible.',
      }))

    render(<App />)

    expect(await screen.findByText('El resultado almacenado ya no está disponible.')).toBeTruthy()
    expect(screen.getByText('COMPLETED')).toBeTruthy()
  })

  it('shows processing progress when the backend provides it and keeps legacy jobs usable', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input) => {
      if (String(input).endsWith('/jobs')) {
        return jsonResponse({ jobs: [{ job_id: 'progress-job', status: 'PROCESSING', filename: 'pages.pdf', current_page: 2, total_pages: 3, progress_percent: 66 }], count: 1, limit: 20 })
      }
      return jsonResponse({ job_id: 'progress-job', status: 'PROCESSING', filename: 'pages.pdf', current_page: 2, total_pages: 3, progress_percent: 66 })
    })

    render(<App />)
    expect(await screen.findByText('Procesando documento · Página 2 de 3 · 66%')).toBeTruthy()
    expect(screen.getByLabelText('Progreso del procesamiento')).toBeTruthy()
  })

  it('asks the API using the owning card job id and keeps answers isolated between documents', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.mocked(fetch)
    let askStatusReads = 0
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/jobs') && !init?.method) {
        return jsonResponse({ jobs: [
          { job_id: 'document-a', status: 'COMPLETED', filename: 'a.pdf' },
          { job_id: 'document-b', status: 'COMPLETED', filename: 'b.pdf' },
        ], count: 2, limit: 20 })
      }
      if (url.endsWith('/jobs/document-a/ask')) return jsonResponse({ job_id: 'document-a', ask_id: 'ask-a', status: 'PENDING' }, 202)
      if (url.endsWith('/jobs/document-a/asks/ask-a')) {
        askStatusReads += 1
        if (askStatusReads === 1) return jsonResponse({
          job_id: 'document-a', ask_id: 'ask-a', status: 'PROCESSING', processing_stage: 'WAITING_MODEL',
        })
        return jsonResponse({
          job_id: 'document-a', ask_id: 'ask-a', status: 'COMPLETED', processing_stage: 'COMPLETED',
          answer: 'Respuesta exclusiva de A', source_pages: [2],
        })
      }
      const jobId = url.endsWith('/jobs/document-a') ? 'document-a' : 'document-b'
      return jsonResponse({
        job_id: jobId,
        status: 'COMPLETED',
        filename: `${jobId}.pdf`,
        result: {
          filename: `${jobId}.pdf`, pages: 1,
          analysis: { document_type: 'Contrato', summary: `Resumen ${jobId}`, key_points: [] },
          transcription: { text: 'Documento sintético.' },
          visual_elements: { stamps: [], signatures: [], other_marks: [] },
          requested_output: { user_request: '', response: null },
        },
      })
    })

    render(<App />)
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    const cardA = screen.getByText('ID: document-a').closest('section') as HTMLElement
    const cardB = screen.getByText('ID: document-b').closest('section') as HTMLElement
    const askAccordionA = within(cardA).getByRole('button', { name: /Preguntar a la IA/ })
    fireEvent.click(askAccordionA)
    await act(async () => { await Promise.resolve() })
    fireEvent.change(within(cardA).getByLabelText('Tu pregunta'), { target: { value: '¿Quién firmó el documento?' } })
    fireEvent.click(within(cardA).getByRole('button', { name: 'Preguntar' }))
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(2500) })
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(within(cardA).getByText('Esperando respuesta de la IA (puede estar en cola o procesando).')).toBeTruthy()
    expect(within(cardA).queryByText('Respuesta exclusiva de A')).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    await act(async () => { await Promise.resolve(); await Promise.resolve() })

    expect(fetchMock.mock.calls.some(([input, init]) =>
      String(input).endsWith('/jobs/document-a/ask')
      && init?.method === 'POST'
      && JSON.parse(String(init.body)).question === '¿Quién firmó el documento?',
    )).toBe(true)
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/jobs/document-b/ask'))).toBe(false)
    expect(within(cardA).getByText('Respuesta exclusiva de A')).toBeTruthy()
    expect(within(cardB).queryByText('Respuesta exclusiva de A')).toBeNull()
    expect(within(cardA).getByText('Página 2')).toBeTruthy()
  })

  it('cancels question status polling when the document view unmounts', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input) => String(input).endsWith('/jobs')
      ? jsonResponse({ jobs: [{ job_id: 'pending-ask', status: 'COMPLETED', filename: 'a.pdf' }], count: 1, limit: 20 })
      : String(input).endsWith('/jobs/pending-ask/ask')
        ? jsonResponse({ job_id: 'pending-ask', ask_id: 'ask-pending', status: 'PENDING' }, 202)
        : String(input).endsWith('/jobs/pending-ask/asks/ask-pending')
          ? jsonResponse({ job_id: 'pending-ask', ask_id: 'ask-pending', status: 'PROCESSING' })
          : jsonResponse({
            job_id: 'pending-ask', status: 'COMPLETED', filename: 'a.pdf',
            result: {
              filename: 'a.pdf', pages: 1,
              analysis: { document_type: 'Carta', summary: 'Resumen', key_points: [] },
              transcription: { text: 'Texto' },
              visual_elements: { stamps: [], signatures: [], other_marks: [] },
              requested_output: { user_request: '', response: null },
            },
          }))

    const view = render(<App />)
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    const card = screen.getByText('ID: pending-ask').closest('section') as HTMLElement
    fireEvent.click(within(card).getByRole('button', { name: /Preguntar a la IA/ }))
    await act(async () => { await Promise.resolve() })
    fireEvent.change(within(card).getByLabelText('Tu pregunta'), { target: { value: '¿Qué dice?' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Preguntar' }))
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    expect(vi.getTimerCount()).toBeGreaterThan(0)
    view.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('does not present COMPLETED until the status response includes the answer', async () => {
    vi.useFakeTimers()
    let statusReads = 0
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/jobs') && !init?.method) return jsonResponse({
        jobs: [{ job_id: 'answer-guard', status: 'COMPLETED', filename: 'a.pdf' }], count: 1, limit: 20,
      })
      if (url.endsWith('/jobs/answer-guard/ask')) return jsonResponse({
        job_id: 'answer-guard', ask_id: 'ask-answer-guard', status: 'PENDING', processing_stage: 'QUEUED',
      }, 202)
      if (url.endsWith('/jobs/answer-guard/asks/ask-answer-guard')) {
        statusReads += 1
        return statusReads === 1
          ? jsonResponse({ job_id: 'answer-guard', ask_id: 'ask-answer-guard', status: 'COMPLETED' })
          : jsonResponse({
            job_id: 'answer-guard', ask_id: 'ask-answer-guard', status: 'COMPLETED',
            processing_stage: 'COMPLETED', answer: 'Respuesta recuperada', source_pages: [1],
          })
      }
      return jsonResponse({
        job_id: 'answer-guard', status: 'COMPLETED', filename: 'a.pdf',
        result: {
          filename: 'a.pdf', pages: 1,
          analysis: { document_type: 'Carta', summary: 'Resumen', key_points: [] },
          transcription: { text: 'Texto' },
          visual_elements: { stamps: [], signatures: [], other_marks: [] },
          requested_output: { user_request: '', response: null },
        },
      })
    })

    render(<App />)
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    const card = screen.getByText('ID: answer-guard').closest('section') as HTMLElement
    fireEvent.click(within(card).getByRole('button', { name: /Preguntar a la IA/ }))
    await act(async () => { await Promise.resolve() })
    fireEvent.change(within(card).getByLabelText('Tu pregunta'), { target: { value: '¿Qué dice?' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Preguntar' }))
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(2500) })
    await act(async () => { await Promise.resolve(); await Promise.resolve() })

    expect(within(card).queryByText('Respuesta lista.')).toBeNull()
    expect(within(card).queryByText('Respuesta recuperada')).toBeNull()
    expect(within(card).getByText(/La consulta figura como lista/)).toBeTruthy()

    fireEvent.click(within(card).getByRole('button', { name: 'Revisar estado' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(2500) })
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(within(card).getByText('Respuesta recuperada')).toBeTruthy()
    expect(statusReads).toBe(2)
  })

  it('keeps a pending backend ask pending after the bounded polling window and allows resuming', async () => {
    vi.useFakeTimers()
    let now = 1_000_000
    vi.spyOn(Date, 'now').mockImplementation(() => now)
    let statusReads = 0
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/jobs')) return jsonResponse({ jobs: [
        { job_id: 'slow-job', status: 'COMPLETED', filename: 'slow.pdf' },
      ], count: 1, limit: 20 })
      if (url.endsWith('/jobs/slow-job/ask')) return jsonResponse({
        job_id: 'slow-job', ask_id: 'ask-slow', status: 'PENDING',
      }, 202)
      if (url.endsWith('/jobs/slow-job/asks/ask-slow')) {
        statusReads += 1
        if (statusReads === 1) {
          now += 60 * 60 * 1000 + 1
          return jsonResponse({ job_id: 'slow-job', ask_id: 'ask-slow', status: 'PROCESSING', processing_stage: 'WAITING_MODEL' })
        }
        return jsonResponse({
          job_id: 'slow-job', ask_id: 'ask-slow', status: 'COMPLETED',
          answer: 'Respuesta tardía', source_pages: [1],
        })
      }
      return jsonResponse({
        job_id: 'slow-job', status: 'COMPLETED', filename: 'slow.pdf',
        result: {
          filename: 'slow.pdf', pages: 1,
          analysis: { document_type: 'Carta', summary: 'Resumen', key_points: [] },
          transcription: { text: 'Texto' },
          visual_elements: { stamps: [], signatures: [], other_marks: [] },
          requested_output: { user_request: '', response: null },
        },
      })
    })

    render(<App />)
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    const card = screen.getByText('ID: slow-job').closest('section') as HTMLElement
    fireEvent.click(within(card).getByRole('button', { name: /Preguntar a la IA/ }))
    await act(async () => { await Promise.resolve() })
    fireEvent.change(within(card).getByLabelText('Tu pregunta'), { target: { value: '¿Qué ocurre?' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Preguntar' }))
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(2500) })
    await act(async () => { await Promise.resolve(); await Promise.resolve() })

    expect(within(card).getByText('Esperando respuesta de la IA (puede estar en cola o procesando).')).toBeTruthy()
    expect(within(card).getByText(/Se detuvo la consulta automática/)).toBeTruthy()
    expect(within(card).queryByText('No se pudo responder la pregunta.')).toBeNull()

    const resumeButton = within(card).getByRole('button', { name: 'Revisar estado' })
    act(() => {
      resumeButton.click()
      resumeButton.click()
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(2500) })
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(within(card).getByText('Respuesta tardía')).toBeTruthy()
    expect(statusReads).toBe(2)
  })

  it('shows a question quota error without creating a fake failed ask', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith('/jobs')) return jsonResponse({
        jobs: [{ job_id: 'quota-job', status: 'COMPLETED', filename: 'quota.pdf' }], count: 1, limit: 20,
      })
      if (String(input).endsWith('/jobs/quota-job/ask')) return jsonResponse({
        error: 'Alcanzaste el límite diario de preguntas sobre documentos.',
      }, 429)
      return jsonResponse({
        job_id: 'quota-job', status: 'COMPLETED', filename: 'quota.pdf',
        result: {
          filename: 'quota.pdf', pages: 1,
          analysis: { document_type: 'Carta', summary: 'Resumen', key_points: [] },
          transcription: { text: 'Texto' },
          visual_elements: { stamps: [], signatures: [], other_marks: [] },
          requested_output: { user_request: '', response: null },
        },
      })
    })
    render(<App />)
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    const card = screen.getByText('ID: quota-job').closest('section') as HTMLElement
    fireEvent.click(within(card).getByRole('button', { name: /Preguntar a la IA/ }))
    await act(async () => { await Promise.resolve() })
    fireEvent.change(within(card).getByLabelText('Tu pregunta'), { target: { value: '¿Una pregunta?' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Preguntar' }))
    expect(await within(card).findByText('Alcanzaste el límite diario de preguntas sobre documentos.')).toBeTruthy()
    expect(within(card).queryByText('No se pudo responder la pregunta.')).toBeNull()
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/asks/'))).toBe(false)
  })

  it('stops polling when an ask has expired or is no longer accessible', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/jobs')) return jsonResponse({
        jobs: [{ job_id: 'expired-job', status: 'COMPLETED', filename: 'expired.pdf' }], count: 1, limit: 20,
      })
      if (url.endsWith('/jobs/expired-job/ask')) return jsonResponse({
        job_id: 'expired-job', ask_id: 'ask-expired', status: 'PENDING',
      }, 202)
      if (url.endsWith('/jobs/expired-job/asks/ask-expired')) return jsonResponse({ error: 'Consulta no encontrada' }, 404)
      return jsonResponse({
        job_id: 'expired-job', status: 'COMPLETED', filename: 'expired.pdf',
        result: {
          filename: 'expired.pdf', pages: 1,
          analysis: { document_type: 'Carta', summary: 'Resumen', key_points: [] },
          transcription: { text: 'Texto' },
          visual_elements: { stamps: [], signatures: [], other_marks: [] },
          requested_output: { user_request: '', response: null },
        },
      })
    })
    render(<App />)
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    const card = screen.getByText('ID: expired-job').closest('section') as HTMLElement
    fireEvent.click(within(card).getByRole('button', { name: /Preguntar a la IA/ }))
    await act(async () => { await Promise.resolve() })
    fireEvent.change(within(card).getByLabelText('Tu pregunta'), { target: { value: '¿Qué fecha?' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Preguntar' }))
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(2500) })
    await act(async () => { await Promise.resolve(); await Promise.resolve() })

    expect(within(card).getByText(/La consulta ya no está disponible/)).toBeTruthy()
    expect(within(card).queryByRole('button', { name: 'Revisar estado' })).toBeNull()
  })
})
