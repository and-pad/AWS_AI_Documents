import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  Alert,
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Divider,
  List,
  ListItem,
  ListItemText,
  Paper,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import AssignmentOutlinedIcon from '@mui/icons-material/AssignmentOutlined'
import DescriptionOutlinedIcon from '@mui/icons-material/DescriptionOutlined'
import DrawOutlinedIcon from '@mui/icons-material/DrawOutlined'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import FiberManualRecordIcon from '@mui/icons-material/FiberManualRecord'
import QuestionAnswerOutlinedIcon from '@mui/icons-material/QuestionAnswerOutlined'
import TextSnippetOutlinedIcon from '@mui/icons-material/TextSnippetOutlined'
import { bearerToken } from './auth'

const apiBase = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '')
const MAX_QUESTION_LENGTH = 2000
const MAX_QUESTION_POLL_MS = 20 * 60 * 1000
const QUESTION_STATUS_TIMEOUT_MS = 15000
const QUESTION_CREATE_TIMEOUT_MS = 25000

type VisualItem = {
  description: string
  location: string | null
  visible_text?: string | null
  associated_name?: string | null
  type?: string
}

export type StructuredDocumentResult = {
  filename: string
  pages: number
  analysis: {
    document_type: string
    summary: string
    key_points: string[]
  }
  transcription: { text: string }
  visual_elements: {
    stamps: VisualItem[]
    signatures: VisualItem[]
    other_marks: VisualItem[]
  }
  requested_output: {
    user_request: string
    response: string | null
  }
  page_extractions?: {
    page: number
    transcription: string
    analysis_notes: string
    visual_elements: {
      stamps: VisualItem[]
      signatures: VisualItem[]
      other_marks: VisualItem[]
    }
  }[]
}

export type LegacyDocumentResult = {
  filename: string
  pages: number
  page_results: { page: number; analysis: string }[]
  final_analysis: string
  user_prompt: string
}

export type DocumentResult = StructuredDocumentResult | LegacyDocumentResult

function isStructuredResult(result: DocumentResult): result is StructuredDocumentResult {
  return 'analysis' in result
    && typeof result.analysis?.summary === 'string'
    && typeof result.transcription?.text === 'string'
    && Array.isArray(result.visual_elements?.stamps)
    && Array.isArray(result.visual_elements?.signatures)
    && Array.isArray(result.visual_elements?.other_marks)
    && typeof result.requested_output?.response !== 'undefined'
}

function Section({
  title,
  icon,
  children,
  defaultExpanded = false,
}: {
  title: string
  icon: ReactNode
  children: ReactNode
  defaultExpanded?: boolean
}) {
  return (
    <Accordion defaultExpanded={defaultExpanded} disableGutters elevation={0}>
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Stack direction="row" spacing={1.25} sx={{ alignItems: 'center' }}>
          {icon}
          <Typography sx={{ fontWeight: 650 }}>{title}</Typography>
        </Stack>
      </AccordionSummary>
      <AccordionDetails>{children}</AccordionDetails>
    </Accordion>
  )
}

function VisualGroup({ title, items, emptyText }: { title: string; items: VisualItem[]; emptyText: string }) {
  return (
    <Box sx={{ mb: 2.5, '&:last-child': { mb: 0 } }}>
      <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 0.5 }}>{title}</Typography>
      {items.length === 0 ? (
        <Typography variant="body2" color="text.secondary">{emptyText}</Typography>
      ) : (
        <List dense disablePadding>
          {items.map((item, index) => (
            <ListItem key={`${title}-${index}`} alignItems="flex-start" sx={{ px: 0 }}>
              <ListItemText
                primary={[
                  item.type,
                  item.description,
                ].filter(Boolean).join(' · ')}
                secondary={[
                  item.visible_text ? `Texto visible: ${item.visible_text}` : null,
                  item.associated_name ? `Nombre asociado: ${item.associated_name}` : null,
                  item.location ? `Ubicación: ${item.location}` : null,
                ].filter(Boolean).join(' · ') || undefined}
              />
            </ListItem>
          ))}
        </List>
      )}
    </Box>
  )
}

export function ResultView({
  result,
  status = 'COMPLETED',
  jobId,
  askDisabled = false,
}: {
  result: DocumentResult
  status?: string
  jobId: string
  askDisabled?: boolean
}) {
  if (!isStructuredResult(result)) {
    return (
      <Card variant="outlined" sx={{ mt: 2.5, borderColor: 'divider', borderRadius: 2 }}>
        <CardContent>
          <Stack direction="row" spacing={2} sx={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <Box>
              <Typography variant="h6" sx={{ fontWeight: 700 }}>Resultado del análisis</Typography>
              <Typography variant="body2" color="text.secondary">{result.filename} · {result.pages} página(s)</Typography>
            </Box>
            <Chip size="small" label={status} color="success" variant="outlined" />
          </Stack>
          <Divider sx={{ my: 2 }} />
          <Typography variant="subtitle1" sx={{ fontWeight: 650, mb: 1 }}>Resultado anterior</Typography>
          <Typography sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', lineHeight: 1.7 }}>
            {result.final_analysis}
          </Typography>
          {result.page_results.length > 0 && <>
            <Divider sx={{ my: 2 }} />
            <Typography variant="subtitle1" sx={{ fontWeight: 650 }}>Detalle por página</Typography>
            {result.page_results.map((page) => (
              <Section key={page.page} title={`Página ${page.page}`} icon={<DescriptionOutlinedIcon color="action" fontSize="small" />}>
                <Typography sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', lineHeight: 1.7 }}>{page.analysis}</Typography>
              </Section>
            ))}
          </>}
          {status === 'COMPLETED' && <AskSection jobId={jobId} disabled={askDisabled} />}
        </CardContent>
      </Card>
    )
  }

  const { analysis, transcription, visual_elements: visuals, requested_output: requested } = result
  return (
    <Card variant="outlined" sx={{ mt: 2.5, borderColor: 'divider', borderRadius: 2, overflow: 'hidden' }}>
      <CardContent sx={{ p: { xs: 1.5, sm: 2.5 }, '&:last-child': { pb: { xs: 1.5, sm: 2.5 } } }}>
        <Stack direction="row" spacing={2} sx={{ justifyContent: 'space-between', alignItems: 'flex-start', mb: 1.5 }}>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="h6" sx={{ fontWeight: 700 }}>Resultado del análisis</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ overflowWrap: 'anywhere' }}>
              {result.filename} · {result.pages} página(s)
            </Typography>
          </Box>
          <Chip size="small" label={status} color="success" variant="outlined" />
        </Stack>
        <Divider />

        <Section title="Análisis" icon={<AssignmentOutlinedIcon color="action" fontSize="small" />} defaultExpanded>
          <Stack spacing={1.5}>
            {analysis.document_type && <Box><Chip size="small" variant="outlined" label={analysis.document_type} /></Box>}
            <Typography sx={{ lineHeight: 1.75 }}>{analysis.summary}</Typography>
            {analysis.key_points.length > 0 && (
              <List dense disablePadding>
                {analysis.key_points.map((point, index) => (
                  <ListItem key={`point-${index}`} alignItems="flex-start" sx={{ px: 0, py: 0.25 }}>
                    <FiberManualRecordIcon sx={{ fontSize: 8, mt: 1, mr: 1.5, color: 'text.secondary' }} />
                  <ListItemText primary={point} slotProps={{ primary: { sx: { lineHeight: 1.6 } } }} />
                  </ListItem>
                ))}
              </List>
            )}
          </Stack>
        </Section>

        <Section title="Transcripción literal" icon={<TextSnippetOutlinedIcon color="action" fontSize="small" />}>
          <Typography component="pre" sx={{ m: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontFamily: 'inherit', lineHeight: 1.7 }}>
            {transcription.text}
          </Typography>
        </Section>

        <Section title="Elementos visuales" icon={<DrawOutlinedIcon color="action" fontSize="small" />}>
          <VisualGroup title="Sellos" items={visuals.stamps} emptyText="No se detectaron sellos." />
          <VisualGroup title="Firmas" items={visuals.signatures} emptyText="No se detectaron firmas." />
          <VisualGroup title="Otras marcas" items={visuals.other_marks} emptyText="No se detectaron otras marcas." />
        </Section>

        {requested.response !== null && requested.response.trim() !== '' && (
          <Section title="Respuesta solicitada" icon={<AssignmentOutlinedIcon color="action" fontSize="small" />}>
            {requested.user_request && (
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5, whiteSpace: 'pre-wrap' }}>
                <strong>Solicitud:</strong> “{requested.user_request}”
              </Typography>
            )}
            <Paper variant="outlined" sx={{ p: { xs: 1.5, sm: 2 }, bgcolor: 'grey.50', borderLeft: 3, borderLeftColor: 'primary.main' }}>
              <Typography sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', lineHeight: 1.75 }}>
                {requested.response}
              </Typography>
            </Paper>
          </Section>
        )}

        {status === 'COMPLETED' && <AskSection jobId={jobId} disabled={askDisabled} />}
      </CardContent>
    </Card>
  )
}

type AskEntry = {
  id: string
  question: string
  status: 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'FAILED'
  answer?: string
  source_pages?: number[]
  context_limited?: boolean
  error?: string
  polling_paused?: boolean
}

function AskSection({ jobId, disabled }: { jobId: string; disabled: boolean }) {
  const [question, setQuestion] = useState('')
  const [entries, setEntries] = useState<AskEntry[]>([])
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const [pollingIds, setPollingIds] = useState<Set<string>>(new Set())
  const activeRequests = useRef(new Map<string, AbortController>())
  const activePolls = useRef(new Set<string>())

  useEffect(() => () => {
    activeRequests.current.forEach((controller) => controller.abort())
    activeRequests.current.clear()
    activePolls.current.clear()
  }, [])
  useEffect(() => {
    if (disabled) {
      activeRequests.current.forEach((controller) => controller.abort())
      activeRequests.current.clear()
      activePolls.current.clear()
      setPollingIds(new Set())
      setSending(false)
    }
  }, [disabled])

  async function pollQuestion(entryId: string, askId: string, controller: AbortController, requestKey: string) {
    if (!apiBase || activePolls.current.has(entryId)) return
    activePolls.current.add(entryId)
    activeRequests.current.set(requestKey, controller)
    setPollingIds((current) => new Set(current).add(entryId))
    setEntries((current) => current.map((entry) => entry.id === entryId
      ? { ...entry, polling_paused: false, error: undefined }
      : entry))
    const deadline = Date.now() + MAX_QUESTION_POLL_MS
    try {
      while (Date.now() < deadline && !controller.signal.aborted) {
        await waitForNextPoll(2500, controller.signal)
        const statusToken = await bearerToken()
        const statusResponse = await fetchQuestionRequest(
          `${apiBase}/jobs/${encodeURIComponent(jobId)}/asks/${encodeURIComponent(askId)}`,
          { headers: { Authorization: statusToken } },
          controller.signal,
        )
        if (statusResponse.status === 404) {
          setEntries((current) => current.map((entry) => entry.id === entryId
            ? { ...entry, status: 'FAILED', polling_paused: false, error: 'La consulta ya no está disponible. Puedes iniciar una pregunta nueva.' }
            : entry))
          return
        }
        if (!statusResponse.ok) throw new Error(await apiError(statusResponse))
        const update: AskEntry = await statusResponse.json()
        setEntries((current) => current.map((entry) => entry.id === entryId ? { ...entry, ...update, polling_paused: false } : entry))
        if (update.status === 'COMPLETED' || update.status === 'FAILED') return
      }
      if (!controller.signal.aborted) {
        setEntries((current) => current.map((entry) => entry.id === entryId
          ? { ...entry, polling_paused: true, error: 'Se detuvo la consulta automática. Puedes revisar el estado nuevamente.' }
          : entry))
      }
    } catch (caught) {
      if (controller.signal.aborted) return
      const message = caught instanceof Error ? caught.message : 'No se pudo consultar el estado.'
      // A polling/network error does not prove that the backend ask failed.
      // Preserve its last known PENDING/PROCESSING state and offer a retry.
      setEntries((current) => current.map((entry) => entry.id === entryId
        ? { ...entry, polling_paused: true, error: message }
        : entry))
    } finally {
      activePolls.current.delete(entryId)
      if (activeRequests.current.get(requestKey) === controller) activeRequests.current.delete(requestKey)
      if (!controller.signal.aborted) {
        setPollingIds((current) => {
          const next = new Set(current)
          next.delete(entryId)
          return next
        })
      }
    }
  }

  async function submitQuestion(event: FormEvent) {
    event.preventDefault()
    const submittedQuestion = question.trim()
    if (!submittedQuestion || sending || disabled) return
    if (!apiBase) {
      setError('Configura VITE_API_BASE_URL para consultar el documento.')
      return
    }
    const controller = new AbortController()
    const requestKey = `create-${Date.now()}-${Math.random()}`
    activeRequests.current.set(requestKey, controller)
    setSending(true)
    setError('')
    try {
      const token = await bearerToken()
      const createResponse = await fetchQuestionRequest(`${apiBase}/jobs/${encodeURIComponent(jobId)}/ask`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: token },
        body: JSON.stringify({ question: submittedQuestion }),
      }, controller.signal, QUESTION_CREATE_TIMEOUT_MS)
      if (!createResponse.ok) throw new Error(await apiError(createResponse))
      const created: { job_id: string; ask_id: string; status: AskEntry['status'] } = await createResponse.json()
      const entryId = created.ask_id
      // Defend against an API response that does not match this card's document.
      if (created.job_id !== jobId || !created.ask_id) throw new Error('La API devolvió una consulta incompatible con este documento.')
      setEntries((current) => [{ id: entryId, question: submittedQuestion, status: created.status }, ...current])
      setQuestion('')
      await pollQuestion(entryId, created.ask_id, controller, requestKey)
    } catch (caught) {
      if (controller.signal.aborted) return
      const message = caught instanceof Error ? caught.message : 'No se pudo consultar el documento.'
      setError(message)
    } finally {
      if (!controller.signal.aborted) setSending(false)
      if (activeRequests.current.get(requestKey) === controller) activeRequests.current.delete(requestKey)
    }
  }

  function resumePolling(entry: AskEntry) {
    if (!entry.id || pollingIds.has(entry.id) || disabled) return
    const controller = new AbortController()
    void pollQuestion(entry.id, entry.id, controller, entry.id)
  }

  return (
    <Section title="Preguntar a la IA" icon={<QuestionAnswerOutlinedIcon color="action" fontSize="small" />}>
      <Stack spacing={2}>
        <Typography variant="body2" color="text.secondary">
          Las respuestas usan las extracciones guardadas de este documento; no vuelven a procesar el PDF.
        </Typography>
        <Box component="form" onSubmit={submitQuestion}>
          <Stack spacing={1.25}>
            <TextField
              label="Tu pregunta"
              multiline
              minRows={2}
              maxRows={6}
              fullWidth
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              slotProps={{ htmlInput: { maxLength: MAX_QUESTION_LENGTH } }}
              disabled={disabled || sending}
              helperText={`${question.length} / ${MAX_QUESTION_LENGTH}`}
            />
            <Button type="submit" variant="contained" disabled={disabled || sending || !question.trim()}>
              {sending ? <><CircularProgress size={18} sx={{ mr: 1 }} /> Consultando…</> : 'Preguntar'}
            </Button>
          </Stack>
        </Box>
        {disabled && <Alert severity="info">La consulta no está disponible mientras se elimina este documento.</Alert>}
        {error && <Alert severity="error" role="alert">{error}</Alert>}
        {entries.map((entry) => (
          <Paper key={entry.id} variant="outlined" sx={{ p: 2 }}>
            <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 0.75 }}>Pregunta</Typography>
            <Typography sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', mb: 1.5 }}>{entry.question}</Typography>
            {(entry.status === 'PENDING' || entry.status === 'PROCESSING') && (
              <Stack spacing={1}>
                <Stack direction="row" spacing={1} role="status" sx={{ alignItems: 'center' }}>
                  {pollingIds.has(entry.id) && <CircularProgress size={16} />}
                  <Typography variant="body2" color="text.secondary">{entry.status === 'PENDING' ? 'En espera' : 'Buscando en el documento…'}</Typography>
                </Stack>
                {entry.polling_paused && <Alert severity="info" action={
                  <Button color="inherit" size="small" disabled={pollingIds.has(entry.id)} onClick={() => resumePolling(entry)}>
                    Revisar estado
                  </Button>
                }>{entry.error || 'La consulta continúa en segundo plano.'}</Alert>}
              </Stack>
            )}
            {entry.status === 'COMPLETED' && <>
              <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 0.75 }}>Respuesta</Typography>
              <Typography sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', lineHeight: 1.7 }}>{entry.answer}</Typography>
              {!!entry.source_pages?.length && (
                <Stack direction="row" spacing={0.75} sx={{ mt: 1.25, flexWrap: 'wrap', gap: 0.75 }}>
                  {entry.source_pages.map((page) => <Chip key={page} size="small" variant="outlined" label={`Página ${page}`} />)}
                </Stack>
              )}
              {entry.context_limited && <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: 'block' }}>
                Se consultaron fragmentos seleccionados; la respuesta puede no incluir información fuera de ellos.
              </Typography>}
            </>}
            {entry.status === 'FAILED' && <Alert severity="error">{entry.error || 'No se pudo responder la pregunta.'}</Alert>}
          </Paper>
        ))}
      </Stack>
    </Section>
  )
}

async function fetchQuestionRequest(url: string, init: RequestInit, parentSignal: AbortSignal, timeoutMs = QUESTION_STATUS_TIMEOUT_MS) {
  const requestController = new AbortController()
  const abortFromParent = () => requestController.abort()
  parentSignal.addEventListener('abort', abortFromParent, { once: true })
  const timer = window.setTimeout(() => requestController.abort(), timeoutMs)
  try {
    return await fetch(url, { ...init, signal: requestController.signal })
  } catch (caught) {
    if (!parentSignal.aborted && requestController.signal.aborted) {
      throw new Error('La solicitud tardó demasiado. Puedes revisar el estado de la consulta nuevamente.')
    }
    throw caught
  } finally {
    window.clearTimeout(timer)
    parentSignal.removeEventListener('abort', abortFromParent)
  }
}

async function apiError(response: Response): Promise<string> {
  try {
    const body = await response.json()
    return body.error || body.detail || `Error HTTP ${response.status}`
  } catch {
    return `Error HTTP ${response.status}`
  }
}

function waitForNextPoll(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException('Aborted', 'AbortError'))
      return
    }
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', abort)
      resolve()
    }, milliseconds)
    function abort() {
      window.clearTimeout(timer)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    signal.addEventListener('abort', abort, { once: true })
  })
}
