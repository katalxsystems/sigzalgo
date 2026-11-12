/**
 * Portfolio Basket detail — info, rebalance history, and performance against
 * the benchmark for one saved basket.
 *
 * Two independent queries: the basket itself (fast, always fresh) and its
 * backtest (heavier — loads price history and simulates the whole version
 * timeline). The page renders info/history from the first the moment it
 * resolves rather than waiting on both, since a basket page is a check-in a
 * user returns to often and the info half should never be gated on the
 * slower half.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router'
import {
  addRebalance,
  type BasketBacktestVersion,
  type BasketHolding,
  basketQueryKeys,
  deleteBasket,
  getBasket,
  runBasketBacktest,
  updateBasket,
} from '@/api/basket'
import { listBenchmarks, type PriceSource } from '@/api/portfolio'
import { BasketHoldingsEditor } from '@/components/basket/BasketHoldingsEditor'
import { InsightsPanel } from '@/components/basket/InsightsPanel'
import { RebalanceHistory } from '@/components/basket/RebalanceHistory'
import { AllocationChart } from '@/components/portfolio/AllocationChart'
import { CrisisChart } from '@/components/portfolio/CrisisChart'
import { EoyChart } from '@/components/portfolio/EoyChart'
import { MonthlyReturnsHeatmap } from '@/components/portfolio/MonthlyReturnsHeatmap'
import { PortfolioLineChart } from '@/components/portfolio/PortfolioLineChart'
import { WeeklyReturnsHeatmap } from '@/components/portfolio/WeeklyReturnsHeatmap'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'
import { useAuthStore } from '@/stores/authStore'

const pct = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined ? '-' : `${(v * 100).toFixed(dp)}%`
const num = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined ? '-' : v.toFixed(dp)
const money = (v: number) => `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`

function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string
  value: string
  sub?: string
  tone?: 'good' | 'bad'
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-xs font-medium text-muted-foreground">{label}</div>
        <div
          className={cn(
            'mt-1 text-2xl font-semibold tabular-nums',
            tone === 'good' && 'text-emerald-500',
            tone === 'bad' && 'text-rose-500'
          )}
        >
          {value}
        </div>
        {sub && <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>}
      </CardContent>
    </Card>
  )
}

/** Correlation heatmap. Null cells (too little overlap to measure) draw empty. */
function CorrelationHeatmap({
  symbols,
  matrix,
}: {
  symbols: string[]
  matrix: (number | null)[][]
}) {
  const colour = (v: number | null) => {
    if (v === null) return 'transparent'
    const a = Math.min(Math.abs(v), 1) * 0.85
    return v >= 0 ? `rgba(239,68,68,${a})` : `rgba(59,130,246,${a})`
  }

  return (
    <div className="overflow-x-auto">
      <table className="border-separate border-spacing-0.5 text-xs">
        <thead>
          <tr>
            <th className="p-1" />
            {symbols.map((s) => (
              <th key={s} className="p-1 text-left font-medium text-muted-foreground">
                {s}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {symbols.map((row, i) => (
            <tr key={row}>
              <td className="whitespace-nowrap p-1 pr-2 font-medium text-muted-foreground">
                {row}
              </td>
              {symbols.map((col, j) => {
                const v = matrix[i]?.[j] ?? null
                return (
                  <td
                    key={col}
                    className="min-w-14 rounded border border-border/40 p-1.5 text-center tabular-nums"
                    style={{ background: colour(v) }}
                    title={v === null ? 'too little overlap to measure' : `${row} vs ${col}`}
                  >
                    {v === null ? '-' : v.toFixed(2)}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function extractErrorMessage(err: unknown): string {
  const e = err as { response?: { data?: { message?: unknown } }; message?: string }
  const msg = e.response?.data?.message ?? e.message ?? 'request failed'
  return typeof msg === 'string' ? msg : JSON.stringify(msg)
}

export default function BasketDetail() {
  const { basketId } = useParams<{ basketId: string }>()
  const id = Number(basketId)
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { apiKey } = useAuthStore()

  const [editOpen, setEditOpen] = useState(false)
  const [rebalanceOpen, setRebalanceOpen] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  // Matches the standalone Portfolio Backtester's own default: a live broker
  // session is more often available than downloaded Historify bars for every
  // symbol in an arbitrary basket.
  const [source, setSource] = useState<PriceSource>('api')

  const basketQuery = useQuery({
    queryKey: basketQueryKeys.detail(id),
    queryFn: () => getBasket(apiKey ?? '', id),
    enabled: !!apiKey && Number.isFinite(id),
  })

  const backtestQuery = useQuery({
    queryKey: basketQueryKeys.backtest(id, source),
    queryFn: () => runBasketBacktest({ apikey: apiKey ?? '', basket_id: id, source }),
    enabled: !!apiKey && Number.isFinite(id),
    retry: false,
  })

  const basket = basketQuery.data
  const versions = basket?.versions ?? []
  const latest = versions.length
    ? [...versions].sort((a, b) => b.version_number - a.version_number)[0]
    : null

  const backtest = backtestQuery.data
  // The backend returns a real HTTP error status (422 no data, 403 no broker
  // session, ...) for a failed backtest, not a 200 with `status: "error"` in
  // the body -- axios rejects those, so the failure shows up as
  // `backtestQuery.error`, not as data. Both are "the backtest failed" from
  // this page's point of view.
  const backtestErrorMessage = backtestQuery.isError
    ? extractErrorMessage(backtestQuery.error)
    : backtest?.status === 'error'
      ? (backtest.message ?? 'Backtest failed.')
      : null
  const backtestFailed = backtestErrorMessage !== null
  const backtestByVersion = useMemo(() => {
    const map = new Map<number, BasketBacktestVersion>()
    if (backtest && !backtestFailed) {
      for (const v of backtest.versions) map.set(v.version_number, v)
    }
    return map
  }, [backtest, backtestFailed])

  // ── Edit dialog ──────────────────────────────────────────────────────
  const [editName, setEditName] = useState('')
  const [editBenchmark, setEditBenchmark] = useState('none')
  const [editCapital, setEditCapital] = useState(100000)

  const { data: benchmarks = [] } = useQuery({
    queryKey: ['portfolio', 'benchmarks'],
    queryFn: () => listBenchmarks(apiKey ?? ''),
    enabled: !!apiKey && (editOpen || rebalanceOpen),
    staleTime: 5 * 60_000,
  })

  const openEdit = () => {
    if (!basket) return
    setEditName(basket.name)
    setEditBenchmark(basket.benchmark ?? 'none')
    setEditCapital(basket.initial_capital)
    setFormError(null)
    setEditOpen(true)
  }

  const updateMutation = useMutation({
    mutationFn: () =>
      updateBasket({
        apikey: apiKey ?? '',
        basket_id: id,
        name: editName.trim(),
        benchmark: editBenchmark === 'none' ? null : editBenchmark,
        benchmark_exchange:
          benchmarks.find((b) => b.symbol === editBenchmark)?.exchange ?? 'NSE_INDEX',
        initial_capital: editCapital,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: basketQueryKeys.detail(id) })
      queryClient.invalidateQueries({ queryKey: basketQueryKeys.list() })
      queryClient.invalidateQueries({ queryKey: basketQueryKeys.backtestAll(id) })
      setEditOpen(false)
    },
    onError: (err: unknown) => setFormError(extractErrorMessage(err)),
  })

  // ── Add rebalance dialog ─────────────────────────────────────────────
  const [rebalanceDate, setRebalanceDate] = useState('')
  const [rebalanceHoldings, setRebalanceHoldings] = useState<BasketHolding[]>([])
  const [rebalanceNote, setRebalanceNote] = useState('')

  const openRebalance = () => {
    if (!latest) return
    const minDate = new Date(`${latest.effective_date}T00:00:00Z`)
    minDate.setUTCDate(minDate.getUTCDate() + 1)
    setRebalanceDate(minDate.toISOString().slice(0, 10))
    setRebalanceHoldings(latest.holdings.map((h) => ({ ...h })))
    setRebalanceNote('')
    setFormError(null)
    setRebalanceOpen(true)
  }

  const rebalanceMutation = useMutation({
    mutationFn: () =>
      addRebalance({
        apikey: apiKey ?? '',
        basket_id: id,
        holdings: rebalanceHoldings.filter((h) => h.symbol.trim()),
        effective_date: rebalanceDate,
        note: rebalanceNote.trim() || null,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: basketQueryKeys.detail(id) })
      queryClient.invalidateQueries({ queryKey: basketQueryKeys.list() })
      queryClient.invalidateQueries({ queryKey: basketQueryKeys.backtestAll(id) })
      setRebalanceOpen(false)
    },
    onError: (err: unknown) => setFormError(extractErrorMessage(err)),
  })

  const submitRebalance = () => {
    if (rebalanceHoldings.filter((h) => h.symbol.trim()).length === 0) {
      setFormError('Add at least one holding.')
      return
    }
    setFormError(null)
    rebalanceMutation.mutate()
  }

  // ── Delete ────────────────────────────────────────────────────────────
  const deleteMutation = useMutation({
    mutationFn: () => deleteBasket(apiKey ?? '', id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: basketQueryKeys.list() })
      navigate('/baskets')
    },
  })

  if (basketQuery.isLoading) {
    return (
      <div className="container mx-auto space-y-4 p-4">
        <p className="text-sm text-muted-foreground">Loading basket…</p>
      </div>
    )
  }

  if (!basket) {
    return (
      <div className="container mx-auto space-y-4 p-4">
        <Card>
          <CardContent className="flex flex-col items-center gap-3 p-8 text-center">
            <p className="text-sm text-muted-foreground">
              Basket not found, or it does not belong to this account.
            </p>
            <Button onClick={() => navigate('/baskets')}>Back to Baskets</Button>
          </CardContent>
        </Card>
      </div>
    )
  }

  const m = backtest && !backtestFailed ? backtest.metrics : null

  return (
    <div className="container mx-auto space-y-4 p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-bold tracking-tight">{basket.name}</h1>
            {basket.benchmark && <Badge variant="outline">vs {basket.benchmark}</Badge>}
          </div>
          <p className="text-sm text-muted-foreground">
            {basket.version_count} version{basket.version_count === 1 ? '' : 's'} · initial capital{' '}
            {money(basket.initial_capital)}
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => navigate('/baskets')}>
            All Baskets
          </Button>
          <Button variant="outline" onClick={openEdit}>
            Edit
          </Button>
          <Button onClick={openRebalance}>Add Rebalance</Button>
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button variant="destructive">Delete</Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Delete "{basket.name}"?</AlertDialogTitle>
                <AlertDialogDescription>
                  This removes the basket and its whole rebalance history. This cannot be undone.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction onClick={() => deleteMutation.mutate()}>
                  Delete
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>
      </div>

      {latest && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">
              Current Composition{' '}
              <span className="text-muted-foreground">(v{latest.version_number})</span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Symbol</TableHead>
                  <TableHead>Exchange</TableHead>
                  <TableHead className="text-right">Weight</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {latest.holdings.map((h) => (
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
          </CardContent>
        </Card>
      )}

      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="insights">Insights</TabsTrigger>
          <TabsTrigger value="holdings">Holdings P&amp;L</TabsTrigger>
          <TabsTrigger value="correlation">Correlation</TabsTrigger>
          <TabsTrigger value="drawdown">Drawdown</TabsTrigger>
          <TabsTrigger value="returns">Period Returns</TabsTrigger>
          <TabsTrigger value="rolling">Rolling Stats</TabsTrigger>
          <TabsTrigger value="allocation">Allocation</TabsTrigger>
          <TabsTrigger value="structure">Structure</TabsTrigger>
          <TabsTrigger value="attribution">Attribution</TabsTrigger>
          <TabsTrigger value="history">Rebalance History</TabsTrigger>
          <TabsTrigger value="costs">Costs</TabsTrigger>
          <TabsTrigger value="crisis">Crisis</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="space-y-4">
          <div className="flex items-center justify-end gap-2">
            <Label className="text-xs text-muted-foreground">Data source</Label>
            <Select value={source} onValueChange={(v) => setSource(v as PriceSource)}>
              <SelectTrigger className="w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="api">Broker API (live)</SelectItem>
                <SelectItem value="db">Historify (local)</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {backtestQuery.isLoading && (
            <p className="text-sm text-muted-foreground">Running backtest…</p>
          )}

          {backtestFailed && (
            <Card className="border-rose-500/40">
              <CardContent className="p-4 text-sm text-rose-500">
                {backtestErrorMessage}
                {source === 'db' && (
                  <span className="mt-1 block text-xs text-rose-500/80">
                    Try "Broker API (live)" above, or download this basket's history via Historify
                    first.
                  </span>
                )}
              </CardContent>
            </Card>
          )}

          {backtest && !backtestFailed && m && (
            <>
              <div className="grid gap-3 md:grid-cols-4">
                <Stat
                  label="Total Return"
                  value={pct(
                    backtest.equity.length
                      ? backtest.equity[backtest.equity.length - 1].value /
                          backtest.meta.initial_capital -
                          1
                      : null
                  )}
                  sub={`${backtest.meta.sessions} sessions`}
                  tone="good"
                />
                <Stat label="CAGR" value={pct(m.cagr)} sub={`benchmark ${pct(m.benchmark_cagr)}`} />
                <Stat label="Max Drawdown" value={pct(m.max_drawdown)} tone="bad" />
                <Stat label="Sharpe" value={num(m.sharpe)} sub={`Sortino ${num(m.sortino)}`} />
              </div>
              <div className="grid gap-3 md:grid-cols-4">
                <Stat label="Alpha" value={pct(m.alpha)} />
                <Stat label="Beta" value={num(m.beta)} />
                <Stat
                  label="Cost Drag"
                  value={pct(backtest.rebalancing.cost_drag)}
                  sub={`${backtest.rebalancing.count} rebalances`}
                />
                <Stat label="Turnover" value={pct(backtest.rebalancing.turnover_total)} />
              </div>
              <div className="grid gap-3 md:grid-cols-4">
                <Stat label="Volatility" value={pct(m.volatility)} sub="annualized" />
                <Stat label="Calmar" value={num(m.calmar)} sub="CAGR / Max DD" />
                <Stat label="Win Rate" value={pct(m.win_rate, 0)} />
                <Stat label="Recovery Factor" value={num(m.recovery_factor)} />
              </div>
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-base">Value vs Benchmark</CardTitle>
                  <p className="text-sm text-muted-foreground">
                    Value of {money(backtest.meta.initial_capital)} since {backtest.meta.start}
                  </p>
                </CardHeader>
                <CardContent>
                  <PortfolioLineChart
                    height={340}
                    format={money}
                    series={[
                      { name: 'Basket', color: '#3b82f6', data: backtest.equity },
                      ...(backtest.benchmark_equity.length > 1
                        ? [
                            {
                              name: backtest.meta.benchmark ?? 'Benchmark',
                              color: '#22c55e',
                              data: backtest.benchmark_equity,
                            },
                          ]
                        : []),
                    ]}
                  />
                </CardContent>
              </Card>
            </>
          )}
        </TabsContent>

        <TabsContent value="insights">
          {backtest && !backtestFailed ? (
            <InsightsPanel insights={backtest.insights} health={backtest.health} />
          ) : (
            <p className="text-sm text-muted-foreground">
              Insights are available once the backtest has run.
            </p>
          )}
        </TabsContent>

        <TabsContent value="holdings">
          {backtest && !backtestFailed ? (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">Itemised P&amp;L</CardTitle>
                <p className="text-sm text-muted-foreground">
                  Against the current (latest-version) composition. Contribution is each holding's
                  share of the basket's return, so the column sums to the total -- it differs from
                  the holding's own return whenever its weight is not 100%.
                </p>
              </CardHeader>
              <CardContent className="p-0">
                <div className="overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Symbol</TableHead>
                        <TableHead className="text-right">Invested</TableHead>
                        <TableHead className="text-right">Net P&amp;L</TableHead>
                        <TableHead className="text-right">Costs</TableHead>
                        <TableHead className="text-right">Own Return</TableHead>
                        <TableHead className="text-right">Contribution</TableHead>
                        <TableHead className="text-right">Weight → Final</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {backtest.items.map((it) => (
                        <TableRow key={it.symbol}>
                          <TableCell className="font-medium">{it.symbol}</TableCell>
                          <TableCell className="text-right tabular-nums">
                            {money(it.invested)}
                          </TableCell>
                          <TableCell
                            className={cn(
                              'text-right tabular-nums',
                              it.net_pnl >= 0 ? 'text-emerald-500' : 'text-rose-500'
                            )}
                          >
                            {money(it.net_pnl)}
                          </TableCell>
                          <TableCell className="text-right tabular-nums text-muted-foreground">
                            {money(it.costs)}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {pct(it.symbol_return)}
                          </TableCell>
                          <TableCell className="text-right font-medium tabular-nums">
                            {pct(it.contribution_pct)}
                          </TableCell>
                          <TableCell className="text-right tabular-nums text-muted-foreground">
                            {pct(it.weight_target, 1)} → {pct(it.weight_final, 1)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </CardContent>
            </Card>
          ) : (
            <p className="text-sm text-muted-foreground">
              Itemised P&amp;L is available once the backtest has run.
            </p>
          )}
        </TabsContent>

        <TabsContent value="correlation" className="space-y-4">
          {backtest && !backtestFailed ? (
            <>
              <div className="grid gap-3 md:grid-cols-3">
                <Stat
                  label="Average Correlation"
                  value={num(backtest.correlation.average_pairwise)}
                  sub="between holdings"
                />
                <Stat
                  label="Diversification Ratio"
                  value={num(backtest.diversification.diversification_ratio)}
                  sub="1.0 means none at all"
                />
                <Stat
                  label="Largest Weight"
                  value={pct(backtest.diversification.largest_weight, 1)}
                  sub={`HHI ${num(backtest.diversification.hhi, 3)}`}
                />
              </div>
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-base">Correlation Heatmap</CardTitle>
                  <p className="text-sm text-muted-foreground">
                    Red moves together, blue moves apart. Holdings that all move together are one
                    bet wearing several names.
                  </p>
                </CardHeader>
                <CardContent>
                  <CorrelationHeatmap
                    symbols={backtest.correlation.symbols}
                    matrix={backtest.correlation.matrix}
                  />
                </CardContent>
              </Card>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              Correlation is available once the backtest has run.
            </p>
          )}
        </TabsContent>

        <TabsContent value="drawdown" className="space-y-4">
          {backtest && !backtestFailed ? (
            <>
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-base">Drawdown</CardTitle>
                  <p className="text-sm text-muted-foreground">
                    How far below the previous peak, and for how long. The shape matters more than
                    the worst number.
                  </p>
                </CardHeader>
                <CardContent>
                  <PortfolioLineChart
                    height={260}
                    format={(v) => `${v.toFixed(2)}%`}
                    series={[
                      {
                        name: 'Drawdown',
                        color: '#ef4444',
                        data: backtest.series.drawdown,
                        area: true,
                      },
                    ]}
                  />
                </CardContent>
              </Card>
              {backtest.series.drawdown_episodes &&
                backtest.series.drawdown_episodes.length > 0 && (
                  <Card>
                    <CardHeader className="pb-2">
                      <CardTitle className="text-base">Worst Episodes</CardTitle>
                    </CardHeader>
                    <CardContent className="p-0">
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead>Start</TableHead>
                            <TableHead>Valley</TableHead>
                            <TableHead>Recovered</TableHead>
                            <TableHead className="text-right">Days</TableHead>
                            <TableHead className="text-right">Depth</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {backtest.series.drawdown_episodes.map((d) => (
                            <TableRow key={`${d.start}-${d.valley}`}>
                              <TableCell>{d.start}</TableCell>
                              <TableCell>{d.valley}</TableCell>
                              <TableCell>{d.end}</TableCell>
                              <TableCell className="text-right tabular-nums">{d.days}</TableCell>
                              <TableCell className="text-right tabular-nums text-rose-500">
                                {pct(d.depth)}
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </CardContent>
                  </Card>
                )}
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              Drawdown is available once the backtest has run.
            </p>
          )}
        </TabsContent>

        <TabsContent value="returns" className="space-y-4">
          {backtest && !backtestFailed ? (
            <>
              {backtest.series.yearly_returns && backtest.series.yearly_returns.length > 0 && (
                <>
                  <Card>
                    <CardHeader className="pb-2">
                      <CardTitle className="text-base">EOY Returns vs Benchmark</CardTitle>
                      <p className="text-sm text-muted-foreground">
                        One pair of bars per calendar year. The dashed red line is the basket's own
                        average year.
                      </p>
                    </CardHeader>
                    <CardContent>
                      <EoyChart
                        rows={backtest.series.yearly_returns}
                        benchmarkLabel={backtest.meta.benchmark ?? 'Benchmark'}
                        height={300}
                      />
                    </CardContent>
                  </Card>
                  <Card>
                    <CardHeader className="pb-2">
                      <CardTitle className="text-base">Yearly Returns</CardTitle>
                    </CardHeader>
                    <CardContent className="p-0">
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead>Year</TableHead>
                            <TableHead className="text-right">Basket</TableHead>
                            <TableHead className="text-right">Benchmark</TableHead>
                            <TableHead className="text-right">Difference</TableHead>
                            <TableHead className="text-center">Won</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {backtest.series.yearly_returns.map((y) => (
                            <TableRow key={y.year}>
                              <TableCell className="font-medium">{y.year}</TableCell>
                              <TableCell
                                className={cn(
                                  'text-right tabular-nums',
                                  (y.portfolio ?? 0) >= 0 ? 'text-emerald-500' : 'text-rose-500'
                                )}
                              >
                                {pct(y.portfolio)}
                              </TableCell>
                              <TableCell className="text-right tabular-nums text-muted-foreground">
                                {pct(y.benchmark)}
                              </TableCell>
                              <TableCell
                                className={cn(
                                  'text-right font-medium tabular-nums',
                                  (y.difference ?? 0) >= 0 ? 'text-emerald-500' : 'text-rose-500'
                                )}
                              >
                                {y.difference === null || y.difference === undefined
                                  ? '-'
                                  : `${y.difference >= 0 ? '+' : ''}${(y.difference * 100).toFixed(2)}%`}
                              </TableCell>
                              <TableCell
                                className={cn(
                                  'text-center font-semibold',
                                  y.won ? 'text-emerald-500' : 'text-rose-500'
                                )}
                              >
                                {y.won === undefined ? '' : y.won ? '+' : '-'}
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </CardContent>
                  </Card>
                </>
              )}
              {backtest.series.monthly_returns && (
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-base">Monthly Returns</CardTitle>
                    <p className="text-sm text-muted-foreground">
                      A run of bad months is what makes people abandon a basket, and an annualised
                      number cannot show one.
                    </p>
                  </CardHeader>
                  <CardContent>
                    <MonthlyReturnsHeatmap
                      years={backtest.series.monthly_returns.years}
                      columns={backtest.series.monthly_returns.columns}
                      values={backtest.series.monthly_returns.values}
                    />
                  </CardContent>
                </Card>
              )}
              {backtest.series.weekly_returns.length > 0 && (
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-base">Weekly Returns</CardTitle>
                    <p className="text-sm text-muted-foreground">
                      Select a year to see every week.
                    </p>
                  </CardHeader>
                  <CardContent>
                    <WeeklyReturnsHeatmap series={backtest.series.weekly_returns} />
                  </CardContent>
                </Card>
              )}
              {backtest.series.seasonality && (
                <div className="grid gap-3 md:grid-cols-3 lg:grid-cols-6">
                  <Stat
                    label="Best Month"
                    value={pct(backtest.series.seasonality.best_month)}
                    tone="good"
                  />
                  <Stat
                    label="Worst Month"
                    value={pct(backtest.series.seasonality.worst_month)}
                    tone="bad"
                  />
                  <Stat
                    label="Avg Up Month"
                    value={pct(backtest.series.seasonality.avg_up_month)}
                    tone="good"
                  />
                  <Stat
                    label="Avg Down Month"
                    value={pct(backtest.series.seasonality.avg_down_month)}
                    tone="bad"
                  />
                  <Stat label="Win Months" value={pct(backtest.series.seasonality.win_months, 0)} />
                  <Stat
                    label="Win Quarters"
                    value={pct(backtest.series.seasonality.win_quarters, 0)}
                  />
                </div>
              )}
              {backtest.series.return_quantiles && backtest.series.return_quantiles.length > 0 && (
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-base">Return Quantiles</CardTitle>
                    <p className="text-sm text-muted-foreground">
                      How the spread narrows with holding period.
                    </p>
                  </CardHeader>
                  <CardContent className="p-0">
                    <div className="overflow-x-auto">
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead>Period</TableHead>
                            <TableHead className="text-right">n</TableHead>
                            <TableHead className="text-right">Worst</TableHead>
                            <TableHead className="text-right">25%</TableHead>
                            <TableHead className="text-right">Median</TableHead>
                            <TableHead className="text-right">75%</TableHead>
                            <TableHead className="text-right">Best</TableHead>
                            <TableHead className="text-right">Ended down</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {backtest.series.return_quantiles.map((q) => (
                            <TableRow key={q.period}>
                              <TableCell className="font-medium">{q.period}</TableCell>
                              <TableCell className="text-right tabular-nums text-muted-foreground">
                                {q.count}
                              </TableCell>
                              <TableCell className="text-right tabular-nums text-rose-500">
                                {pct(q.min)}
                              </TableCell>
                              <TableCell className="text-right tabular-nums">{pct(q.q1)}</TableCell>
                              <TableCell className="text-right font-medium tabular-nums">
                                {pct(q.median)}
                              </TableCell>
                              <TableCell className="text-right tabular-nums">{pct(q.q3)}</TableCell>
                              <TableCell className="text-right tabular-nums text-emerald-500">
                                {pct(q.max)}
                              </TableCell>
                              <TableCell className="text-right tabular-nums">
                                {pct(q.negative_share, 0)}
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </div>
                  </CardContent>
                </Card>
              )}
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              Period returns are available once the backtest has run.
            </p>
          )}
        </TabsContent>

        <TabsContent value="rolling" className="space-y-4">
          {backtest && !backtestFailed ? (
            backtest.series.rolling ? (
              <>
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-base">
                      Rolling Sharpe ({backtest.series.rolling.window} sessions)
                    </CardTitle>
                    <p className="text-sm text-muted-foreground">
                      A flattering full-period Sharpe can hide one that has been deteriorating for a
                      year.
                    </p>
                  </CardHeader>
                  <CardContent>
                    <PortfolioLineChart
                      height={240}
                      format={(v) => v.toFixed(2)}
                      series={[
                        {
                          name: 'Rolling Sharpe',
                          color: '#8b5cf6',
                          data: backtest.series.rolling.sharpe,
                        },
                      ]}
                    />
                  </CardContent>
                </Card>
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-base">Rolling Volatility</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <PortfolioLineChart
                      height={240}
                      format={(v) => `${(v * 100).toFixed(1)}%`}
                      series={[
                        {
                          name: 'Rolling Volatility',
                          color: '#f59e0b',
                          data: backtest.series.rolling.volatility,
                        },
                      ]}
                    />
                  </CardContent>
                </Card>
              </>
            ) : (
              <Card>
                <CardContent className="p-4 text-sm text-muted-foreground">
                  Not enough history for a rolling window over this period.
                </CardContent>
              </Card>
            )
          ) : (
            <p className="text-sm text-muted-foreground">
              Rolling stats are available once the backtest has run.
            </p>
          )}
        </TabsContent>

        <TabsContent value="allocation" className="space-y-4">
          {backtest && !backtestFailed ? (
            <>
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-base">Composition Over Time</CardTitle>
                  <p className="text-sm text-muted-foreground">
                    What the basket actually held, session by session. Bands widen as winners run
                    and snap back on each rebalance.
                  </p>
                </CardHeader>
                <CardContent className="pl-12">
                  <AllocationChart
                    dates={backtest.allocation.dates}
                    symbols={backtest.allocation.symbols}
                    series={backtest.allocation.series}
                    average={backtest.allocation.average}
                    height={320}
                  />
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-base">Target versus Realised</CardTitle>
                  <p className="text-sm text-muted-foreground">
                    Target is the latest version's weights; average held differs whenever the basket
                    was left to drift between rebalances.
                  </p>
                </CardHeader>
                <CardContent className="p-0">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Symbol</TableHead>
                        <TableHead className="text-right">Target</TableHead>
                        <TableHead className="text-right">Average held</TableHead>
                        <TableHead className="text-right">Final</TableHead>
                        <TableHead className="text-right">Drift</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {backtest.items.map((it) => {
                        const avg = backtest.allocation.average[it.symbol] ?? 0
                        return (
                          <TableRow key={it.symbol}>
                            <TableCell className="font-medium">{it.symbol}</TableCell>
                            <TableCell className="text-right tabular-nums text-muted-foreground">
                              {pct(it.weight_target, 1)}
                            </TableCell>
                            <TableCell className="text-right tabular-nums">{pct(avg, 1)}</TableCell>
                            <TableCell className="text-right tabular-nums">
                              {pct(it.weight_final, 1)}
                            </TableCell>
                            <TableCell
                              className={cn(
                                'text-right font-medium tabular-nums',
                                Math.abs(it.weight_final - it.weight_target) > 0.05
                                  ? 'text-amber-500'
                                  : 'text-muted-foreground'
                              )}
                            >
                              {`${it.weight_final - it.weight_target >= 0 ? '+' : ''}${((it.weight_final - it.weight_target) * 100).toFixed(1)}pp`}
                            </TableCell>
                          </TableRow>
                        )
                      })}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              Allocation is available once the backtest has run.
            </p>
          )}
        </TabsContent>

        <TabsContent value="structure" className="space-y-4">
          {backtest && !backtestFailed ? (
            <>
              <div className="grid gap-3 md:grid-cols-3">
                <Stat
                  label="Effective Bets"
                  value={String(backtest.structure.effective_bets)}
                  sub={`from ${backtest.items.length} holdings`}
                />
                <Stat
                  label="Largest Cluster"
                  value={pct(backtest.structure.largest_cluster_weight, 1)}
                  sub="moves as one position"
                  tone={backtest.structure.largest_cluster_weight > 0.5 ? 'bad' : undefined}
                />
                <Stat
                  label="Funds vs Stocks"
                  value={`${pct(backtest.structure.instrument_classes.fund ?? 0, 0)} / ${pct(
                    backtest.structure.instrument_classes.stock ?? 0,
                    0
                  )}`}
                  sub={backtest.structure.instrument_class_basis}
                />
              </div>
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-base">Co-movement Clusters</CardTitle>
                  <p className="text-sm text-muted-foreground">
                    Holdings correlated above {backtest.structure.threshold} are grouped: they
                    behave as one position, whatever their names say.
                  </p>
                </CardHeader>
                <CardContent className="space-y-2">
                  {backtest.structure.clusters.map((c) => (
                    <div key={c.id} className="rounded-md border p-3">
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-medium">
                          {c.independent ? 'Independent holding' : `Cluster ${c.id}`}
                        </span>
                        <span className="tabular-nums text-sm">{pct(c.weight, 1)}</span>
                      </div>
                      <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded bg-muted">
                        <div
                          className={cn(
                            'h-full rounded',
                            c.independent ? 'bg-emerald-500' : 'bg-amber-500'
                          )}
                          style={{ width: `${Math.min(c.weight * 100, 100)}%` }}
                        />
                      </div>
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {c.members.map((mem) => (
                          <span key={mem} className="rounded bg-muted px-1.5 py-0.5 text-xs">
                            {mem}
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
                </CardContent>
              </Card>
              <Card>
                <CardContent className="p-4 text-sm text-muted-foreground">
                  {backtest.structure.sector_note}
                </CardContent>
              </Card>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              Structure is available once the backtest has run.
            </p>
          )}
        </TabsContent>

        <TabsContent value="attribution" className="space-y-4">
          {backtest && !backtestFailed ? (
            backtest.attribution.available ? (
              <>
                <div className="grid gap-3 md:grid-cols-4">
                  <Stat
                    label="Excess vs Benchmark"
                    value={pct(backtest.attribution.excess_return)}
                    sub="what has to be explained"
                    tone={(backtest.attribution.excess_return ?? 0) >= 0 ? 'good' : 'bad'}
                  />
                  <Stat
                    label="Selection"
                    value={pct(backtest.attribution.selection_effect)}
                    sub="were these the right things to own"
                  />
                  <Stat
                    label="Allocation"
                    value={pct(backtest.attribution.allocation_effect)}
                    sub="did the weighting help"
                  />
                  <Stat
                    label="Trading costs"
                    value={pct(backtest.attribution.cost_effect)}
                    sub="net return versus gross"
                  />
                </div>
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-base">Contribution by Holding</CardTitle>
                    <p className="text-sm text-muted-foreground">
                      Each holding's share of the out- or under-performance, not its standalone
                      result -- the column sums to the excess.
                    </p>
                  </CardHeader>
                  <CardContent className="p-0">
                    {(backtest.attribution.holdings ?? []).length > 0 ? (
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead>Symbol</TableHead>
                            <TableHead className="text-right">Weight</TableHead>
                            <TableHead className="text-right">Own return</TableHead>
                            <TableHead className="text-right">vs benchmark</TableHead>
                            <TableHead className="text-right">Contribution</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {(backtest.attribution.holdings ?? []).map((h) => (
                            <TableRow key={h.symbol}>
                              <TableCell className="font-medium">{h.symbol}</TableCell>
                              <TableCell className="text-right tabular-nums text-muted-foreground">
                                {pct(h.weight, 1)}
                              </TableCell>
                              <TableCell className="text-right tabular-nums">
                                {pct(h.return)}
                              </TableCell>
                              <TableCell
                                className={cn(
                                  'text-right tabular-nums',
                                  h.vs_benchmark >= 0 ? 'text-emerald-500' : 'text-rose-500'
                                )}
                              >
                                {h.vs_benchmark >= 0 ? '+' : ''}
                                {(h.vs_benchmark * 100).toFixed(2)}%
                              </TableCell>
                              <TableCell
                                className={cn(
                                  'text-right font-medium tabular-nums',
                                  h.contribution >= 0 ? 'text-emerald-500' : 'text-rose-500'
                                )}
                              >
                                {h.contribution >= 0 ? '+' : ''}
                                {(h.contribution * 100).toFixed(2)}%
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    ) : (
                      <p className="p-4 text-sm text-muted-foreground">
                        {backtest.attribution.holdings_reason}
                      </p>
                    )}
                  </CardContent>
                </Card>
                {backtest.attribution.method && (
                  <p className="text-xs text-muted-foreground">{backtest.attribution.method}</p>
                )}
              </>
            ) : (
              <Card>
                <CardContent className="p-4 text-sm text-muted-foreground">
                  {backtest.attribution.reason ?? 'Attribution needs a benchmark.'}
                </CardContent>
              </Card>
            )
          ) : (
            <p className="text-sm text-muted-foreground">
              Attribution is available once the backtest has run.
            </p>
          )}
        </TabsContent>

        <TabsContent value="history">
          <RebalanceHistory versions={versions} backtestByVersion={backtestByVersion} />
        </TabsContent>

        <TabsContent value="costs">
          {backtest && !backtestFailed ? (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">What the rebalancing cost</CardTitle>
              </CardHeader>
              <CardContent>
                <Table>
                  <TableBody>
                    {Object.entries(backtest.costs)
                      .filter(
                        ([key]) => !['model', 'total', 'drag', 'turnover', 'orders'].includes(key)
                      )
                      .map(([key, value]) => (
                        <TableRow key={key}>
                          <TableCell className="capitalize text-muted-foreground">
                            {key.replace(/_/g, ' ')}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {money(Number(value))}
                          </TableCell>
                        </TableRow>
                      ))}
                    <TableRow>
                      <TableCell className="font-medium">Total</TableCell>
                      <TableCell className="text-right font-medium tabular-nums">
                        {money(backtest.costs.total)}
                      </TableCell>
                    </TableRow>
                  </TableBody>
                </Table>
              </CardContent>
            </Card>
          ) : (
            <p className="text-sm text-muted-foreground">
              Costs are available once the backtest has run.
            </p>
          )}
        </TabsContent>

        <TabsContent value="crisis">
          {backtest && !backtestFailed && backtest.crisis.periods.length > 0 ? (
            <CrisisChart periods={backtest.crisis.periods} />
          ) : (
            <p className="text-sm text-muted-foreground">
              No crisis-period coverage for this basket's window yet.
            </p>
          )}
        </TabsContent>
      </Tabs>

      {/* ── Edit dialog ─────────────────────────────────────────────── */}
      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit Basket</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <Label className="text-xs">Name</Label>
              <Input value={editName} onChange={(e) => setEditName(e.target.value)} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label className="text-xs">Benchmark</Label>
                <Select value={editBenchmark} onValueChange={setEditBenchmark}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">None</SelectItem>
                    {benchmarks.map((b) => (
                      <SelectItem key={`${b.exchange}-${b.symbol}`} value={b.symbol}>
                        {b.symbol}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label className="text-xs">Initial capital</Label>
                <Input
                  type="number"
                  value={editCapital}
                  onChange={(e) => setEditCapital(Number(e.target.value))}
                />
              </div>
            </div>
            {formError && <p className="text-sm text-rose-500">{formError}</p>}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditOpen(false)}>
              Cancel
            </Button>
            <Button onClick={() => updateMutation.mutate()} disabled={updateMutation.isPending}>
              {updateMutation.isPending ? 'Saving…' : 'Save'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Add rebalance dialog ────────────────────────────────────── */}
      <Dialog open={rebalanceOpen} onOpenChange={setRebalanceOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Add Rebalance</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label className="text-xs">Effective date</Label>
              <Input
                type="date"
                value={rebalanceDate}
                min={latest ? rebalanceDate : undefined}
                onChange={(e) => setRebalanceDate(e.target.value)}
              />
              {latest && (
                <p className="mt-1 text-xs text-muted-foreground">
                  Must be after v{latest.version_number}'s date ({latest.effective_date}).
                </p>
              )}
            </div>

            <BasketHoldingsEditor holdings={rebalanceHoldings} onChange={setRebalanceHoldings} />

            <div>
              <Label className="text-xs">Note</Label>
              <Textarea
                value={rebalanceNote}
                onChange={(e) => setRebalanceNote(e.target.value)}
                placeholder="What changed and why"
                rows={2}
              />
            </div>

            {formError && <p className="text-sm text-rose-500">{formError}</p>}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRebalanceOpen(false)}>
              Cancel
            </Button>
            <Button onClick={submitRebalance} disabled={rebalanceMutation.isPending}>
              {rebalanceMutation.isPending ? 'Saving…' : 'Add Rebalance'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
