/**
 * A basket's version timeline, newest first: what changed at each manual
 * rebalance, and -- once the backtest has run -- what the basket was worth
 * and what that rebalance cost.
 */
import { ChevronDown } from 'lucide-react'
import type { BasketBacktestVersion, BasketVersion } from '@/api/basket'
import { ChangeSummaryBadges } from '@/components/basket/ChangeSummaryBadges'
import { Card, CardContent } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

interface Props {
  versions: BasketVersion[]
  /** Keyed by version_number, once the backtest response has resolved. */
  backtestByVersion?: Map<number, BasketBacktestVersion>
}

const fmtDate = (iso: string) =>
  new Date(`${iso}T00:00:00Z`).toLocaleDateString('en-GB', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  })

const money = (v: number) => `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`

export function RebalanceHistory({ versions, backtestByVersion }: Props) {
  const ordered = [...versions].sort((a, b) => b.version_number - a.version_number)

  if (ordered.length === 0) {
    return <p className="text-sm text-muted-foreground">No versions yet.</p>
  }

  return (
    <div className="space-y-3">
      {ordered.map((version) => {
        const realised = backtestByVersion?.get(version.version_number)
        return (
          <Card key={version.id}>
            <CardContent className="p-4">
              <Collapsible>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="space-y-1.5">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-semibold">v{version.version_number}</span>
                      <span className="text-sm text-muted-foreground">
                        {fmtDate(version.effective_date)}
                      </span>
                    </div>
                    <ChangeSummaryBadges
                      summary={version.change_summary}
                      isFirstVersion={version.version_number === 1}
                    />
                    {version.note && (
                      <p className="text-sm text-muted-foreground">{version.note}</p>
                    )}
                  </div>

                  <div className="flex items-center gap-4">
                    {realised && (
                      <div className="text-right text-sm">
                        {realised.value_at_rebalance !== null && (
                          <div className="font-medium tabular-nums">
                            {money(realised.value_at_rebalance)}
                          </div>
                        )}
                        {realised.cost_at_rebalance !== null && realised.cost_at_rebalance > 0 && (
                          <div className="text-xs text-muted-foreground tabular-nums">
                            cost {money(realised.cost_at_rebalance)}
                          </div>
                        )}
                      </div>
                    )}
                    <CollapsibleTrigger asChild>
                      <button
                        type="button"
                        className="group flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                      >
                        Holdings
                        <ChevronDown className="h-3.5 w-3.5 transition-transform group-data-[state=open]:rotate-180" />
                      </button>
                    </CollapsibleTrigger>
                  </div>
                </div>

                <CollapsibleContent>
                  <Table className="mt-3">
                    <TableHeader>
                      <TableRow>
                        <TableHead>Symbol</TableHead>
                        <TableHead>Exchange</TableHead>
                        <TableHead className="text-right">Weight</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {version.holdings.map((h) => (
                        <TableRow key={h.symbol}>
                          <TableCell className="font-medium">{h.symbol}</TableCell>
                          <TableCell className="text-muted-foreground">{h.exchange}</TableCell>
                          <TableCell className="text-right tabular-nums">
                            {h.weight.toFixed(2)}%
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </CollapsibleContent>
              </Collapsible>
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}
