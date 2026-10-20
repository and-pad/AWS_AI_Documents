// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
    fireEvent.change(screen.getByLabelText('Documento PDF'), {
      target: { files: [new File(['pdf'], 'twenty-first.pdf', { type: 'application/pdf' })] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Cargar y analizar' }))

    expect(await screen.findByText('Has alcanzado el límite de 20 documentos. Elimina uno para continuar.')).toBeTruthy()
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false)
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
})
