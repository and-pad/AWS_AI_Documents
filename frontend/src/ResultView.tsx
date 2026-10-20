import type { ReactNode } from 'react'
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Box,
  Card,
  CardContent,
  Chip,
  Divider,
  List,
  ListItem,
  ListItemText,
  Paper,
  Stack,
  Typography,
} from '@mui/material'
import AssignmentOutlinedIcon from '@mui/icons-material/AssignmentOutlined'
import DescriptionOutlinedIcon from '@mui/icons-material/DescriptionOutlined'
import DrawOutlinedIcon from '@mui/icons-material/DrawOutlined'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import FiberManualRecordIcon from '@mui/icons-material/FiberManualRecord'
import TextSnippetOutlinedIcon from '@mui/icons-material/TextSnippetOutlined'

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

export function ResultView({ result, status = 'COMPLETED' }: { result: DocumentResult; status?: string }) {
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
      </CardContent>
    </Card>
  )
}
