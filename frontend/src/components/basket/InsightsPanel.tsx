/**
 * SWOT-style insights for a basket's backtest: auto-generated findings,
 * each carrying the number that produced it (`evidence`) rather than a bare
 * label like "High Concentration Risk" -- see portfolio/insights.py.
 *
 * Nothing here is computed client-side; this only arranges what the backend
 * already derived from the report, so it can never disagree with the numbers
 * shown on the Overview tab.
 */
import { AlertTriangle, CheckCircle2, Lightbulb, ShieldAlert } from 'lucide-react'
import type { BasketInsights, Finding } from '@/api/basket'
import type { PortfolioHealth } from '@/api/portfolio'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'

const KIND_META: Record<
  Finding['kind'],
  { label: string; icon: typeof CheckCircle2; className: string }
> = {
  strength: { label: 'Strengths', icon: CheckCircle2, className: 'text-emerald-500' },
  weakness: { label: 'Weaknesses', icon: AlertTriangle, className: 'text-rose-500' },
  opportunity: { label: 'Opportunities', icon: Lightbulb, className: 'text-sky-500' },
  threat: { label: 'Threats', icon: ShieldAlert, className: 'text-amber-500' },
}

function FindingCard({ finding }: { finding: Finding }) {
  const meta = KIND_META[finding.kind]
  return (
    <div className="rounded-md border p-3">
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-medium">{finding.title}</p>
        <span
          className="mt-0.5 h-1.5 w-1.5 shrink-0 rounded-full"
          style={{ opacity: 0.3 + finding.severity * 0.7 }}
          title={`severity ${(finding.severity * 100).toFixed(0)}%`}
        >
          <span className={cn('block h-full w-full rounded-full', meta.className, 'bg-current')} />
        </span>
      </div>
      <p className="mt-1 text-sm text-muted-foreground">{finding.detail}</p>
    </div>
  )
}

function FindingGroup({ kind, findings }: { kind: Finding['kind']; findings: Finding[] }) {
  if (findings.length === 0) return null
  const meta = KIND_META[kind]
  const Icon = meta.icon
  return (
    <div className="space-y-2">
      <div className={cn('flex items-center gap-1.5 text-sm font-semibold', meta.className)}>
        <Icon className="h-4 w-4" />
        {meta.label}
        <span className="font-normal text-muted-foreground">({findings.length})</span>
      </div>
      <div className="space-y-2">
        {findings.map((f) => (
          <FindingCard key={f.title} finding={f} />
        ))}
      </div>
    </div>
  )
}

const gradeTone = (grade: string | null) => {
  if (!grade) return 'text-muted-foreground'
  if (grade.startsWith('A')) return 'text-emerald-500'
  if (grade.startsWith('B')) return 'text-sky-500'
  if (grade.startsWith('C')) return 'text-amber-500'
  return 'text-rose-500'
}

interface Props {
  insights: BasketInsights
  health: PortfolioHealth
}

export function InsightsPanel({ insights, health }: Props) {
  const nothingFound =
    !insights.headline &&
    insights.strengths.length === 0 &&
    insights.weaknesses.length === 0 &&
    insights.opportunities.length === 0 &&
    insights.threats.length === 0

  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-3">
        <Card className="md:col-span-2">
          <CardContent className="p-4">
            {insights.headline ? (
              <div className="flex items-start gap-3">
                {(() => {
                  const meta = KIND_META[insights.headline.kind]
                  const Icon = meta.icon
                  return <Icon className={cn('mt-0.5 h-5 w-5 shrink-0', meta.className)} />
                })()}
                <div>
                  <p className="text-sm font-semibold">{insights.headline.title}</p>
                  <p className="mt-1 text-sm text-muted-foreground">{insights.headline.detail}</p>
                </div>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                Nothing crossed a threshold worth flagging -- a quiet result is a fine one.
              </p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="flex items-center justify-between p-4">
            <div>
              <div className="text-xs font-medium text-muted-foreground">Portfolio Health</div>
              <div
                className={cn('mt-1 text-2xl font-semibold tabular-nums', gradeTone(health.grade))}
              >
                {health.score !== null ? health.score.toFixed(0) : '-'}
                <span className="ml-1 text-sm font-normal text-muted-foreground">/100</span>
              </div>
            </div>
            {health.grade && (
              <Badge variant="outline" className={cn('text-base', gradeTone(health.grade))}>
                {health.grade}
              </Badge>
            )}
          </CardContent>
        </Card>
      </div>

      {nothingFound ? (
        <p className="text-sm text-muted-foreground">No findings beyond the headline above.</p>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          <FindingGroup kind="strength" findings={insights.strengths} />
          <FindingGroup kind="weakness" findings={insights.weaknesses} />
          <FindingGroup kind="opportunity" findings={insights.opportunities} />
          <FindingGroup kind="threat" findings={insights.threats} />
        </div>
      )}
    </div>
  )
}
