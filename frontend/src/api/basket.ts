import { apiClient } from './client'
import type {
  CurvePoint,
  PortfolioAllocation,
  PortfolioAttribution,
  PortfolioCosts,
  PortfolioCrisis,
  PortfolioHealth,
  PortfolioItem,
  PortfolioMetrics,
  PortfolioSeries,
  PortfolioStructure,
  PriceSource,
} from './portfolio'

/** One holding: a symbol, its exchange, and its weight in this version. */
export interface BasketHolding {
  symbol: string
  exchange: string
  /** Percentage or fraction — only the ratio between holdings matters. */
  weight: number
}

/** Added / removed / reweighted versus the previous version. */
export interface ChangeSummary {
  added: string[]
  removed: string[]
  reweighted: { symbol: string; from: number; to: number }[]
}

/** One rebalance: the basket's composition from `effective_date` on. */
export interface BasketVersion {
  id: number
  version_number: number
  effective_date: string
  holdings: BasketHolding[]
  change_summary: ChangeSummary
  note: string | null
  created_at: string | null
}

export interface Basket {
  id: number
  name: string
  benchmark: string | null
  benchmark_exchange: string | null
  initial_capital: number
  cost_config: Record<string, unknown>
  version_count: number
  created_at: string | null
  updated_at: string | null
  /** Present on getBasket; absent on listBaskets. */
  versions?: BasketVersion[]
  /** Present on listBaskets only. */
  latest_effective_date?: string | null
}

/** A version enriched with what the engine actually realised on that date. */
export interface BasketBacktestVersion extends BasketVersion {
  target_weights: Record<string, number>
  value_at_rebalance: number | null
  cost_at_rebalance: number | null
}

/**
 * One SWOT-style observation, derived from numbers the report already
 * computed -- never invented, and every one carries the figure that produced
 * it (`evidence`) so it can be argued with rather than merely believed.
 */
export interface Finding {
  kind: 'strength' | 'weakness' | 'opportunity' | 'threat'
  tag: string
  title: string
  detail: string
  /** 0-1. Drives ordering and which finding becomes the headline. */
  severity: number
  evidence: Record<string, unknown>
}

export interface BasketInsights {
  /** The single most severe finding of any kind -- as often a strength as a fault. */
  headline: Finding | null
  tags: { kind: Finding['kind']; label: string }[]
  strengths: Finding[]
  weaknesses: Finding[]
  opportunities: Finding[]
  threats: Finding[]
  counts: {
    strengths: number
    weaknesses: number
    opportunities: number
    threats: number
  }
}

export interface BasketBacktestResponse {
  status: 'success' | 'error'
  message?: string
  meta: {
    symbols: string[]
    target_weights: Record<string, number>
    rule: 'manual'
    initial_capital: number
    sessions: number
    source: PriceSource
    start: string
    end: string
    benchmark: string | null
    risk_free_rate: number
    total_return_basis: string
    data_warnings: Record<string, [string, number][]>
  }
  basket: Omit<Basket, 'versions'>
  versions: BasketBacktestVersion[]
  equity: CurvePoint[]
  benchmark_equity: CurvePoint[]
  metrics: PortfolioMetrics
  /** Per-holding P&L against the *latest* version's weights -- a versioned
   * basket has no single target, so this and every composition-shaped field
   * below (correlation/diversification/structure/allocation) is measured
   * against what the basket currently stands for, not its whole history. */
  items: PortfolioItem[]
  correlation: {
    symbols: string[]
    matrix: (number | null)[][]
    average_pairwise: number | null
  }
  diversification: {
    hhi: number
    effective_holdings: number
    largest_weight: number
    holdings: number
    diversification_ratio: number | null
  }
  series: PortfolioSeries
  structure: PortfolioStructure
  allocation: PortfolioAllocation
  costs: PortfolioCosts
  crisis: PortfolioCrisis
  health: PortfolioHealth
  attribution: PortfolioAttribution
  insights: BasketInsights
  rebalancing: {
    rule: 'manual'
    count: number
    cost_drag: number
    turnover_total: number
    dates: string[]
  }
}

export interface CostConfig {
  cost_model?: 'indian_equity' | 'flat_bps'
  brokerage_pct?: number
  cost_exchange?: string
  charges?: Record<string, { rate?: number; flat?: number; cap?: number }>
  gst_rate?: number
  cost_bps?: number
  slippage?: number
}

export interface CreateBasketRequest {
  apikey: string
  name: string
  holdings: BasketHolding[]
  effective_date: string
  benchmark?: string | null
  benchmark_exchange?: string
  initial_capital?: number
  note?: string | null
  cost_model?: CostConfig['cost_model']
  brokerage_pct?: number
  cost_exchange?: string
  charges?: CostConfig['charges']
  gst_rate?: number
  cost_bps?: number
  slippage?: number
}

export async function listBaskets(apikey: string): Promise<Basket[]> {
  const { data } = await apiClient.post<{ status: string; baskets: Basket[] }>('/basket/list', {
    apikey,
  })
  return data.baskets
}

export async function getBasket(apikey: string, basketId: number): Promise<Basket> {
  const { data } = await apiClient.post<{ status: string; basket: Basket }>('/basket/get', {
    apikey,
    basket_id: basketId,
  })
  return data.basket
}

export async function createBasket(req: CreateBasketRequest): Promise<Basket> {
  const { data } = await apiClient.post<{ status: string; basket: Basket }>('/basket/create', req)
  return data.basket
}

export async function addRebalance(req: {
  apikey: string
  basket_id: number
  holdings: BasketHolding[]
  effective_date: string
  note?: string | null
}): Promise<BasketVersion> {
  const { data } = await apiClient.post<{ status: string; version: BasketVersion }>(
    '/basket/rebalance',
    req
  )
  return data.version
}

export async function updateBasket(req: {
  apikey: string
  basket_id: number
  name?: string
  benchmark?: string | null
  benchmark_exchange?: string
  initial_capital?: number
}): Promise<Basket> {
  const { data } = await apiClient.post<{ status: string; basket: Basket }>('/basket/update', req)
  return data.basket
}

export async function deleteBasket(apikey: string, basketId: number): Promise<void> {
  await apiClient.post('/basket/delete', { apikey, basket_id: basketId })
}

export async function runBasketBacktest(req: {
  apikey: string
  basket_id: number
  end_date?: string | null
  risk_free_rate?: number
  source?: PriceSource
}): Promise<BasketBacktestResponse> {
  const { data } = await apiClient.post<BasketBacktestResponse>('/basket/backtest', req)
  return data
}

export const basketQueryKeys = {
  all: ['basket'] as const,
  list: () => [...basketQueryKeys.all, 'list'] as const,
  detail: (id: number) => [...basketQueryKeys.all, 'detail', id] as const,
  /** Matches every backtest query for this basket, regardless of source/end
   * date -- pass to invalidateQueries after a mutation that should refresh
   * whichever variant the user currently has open. */
  backtestAll: (id: number) => [...basketQueryKeys.all, 'backtest', id] as const,
  backtest: (id: number, source: PriceSource = 'api', endDate?: string | null) =>
    [...basketQueryKeys.backtestAll(id), source, endDate ?? 'latest'] as const,
}
