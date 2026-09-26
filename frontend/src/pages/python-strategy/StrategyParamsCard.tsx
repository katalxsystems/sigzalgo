import { Save } from 'lucide-react'
import { useEffect, useState } from 'react'
import { pythonStrategyApi } from '@/api/python-strategy'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import { showToast } from '@/utils/toast'

/**
 * Per-strategy settings, passed to the script at start as the STRATEGY_PARAMS
 * environment variable (JSON). Lets one script run with different quantities,
 * symbols or thresholds without editing its code.
 */
export function StrategyParamsCard({ strategyId }: { strategyId: string }) {
  const [text, setText] = useState('{}')
  const [saved, setSaved] = useState('{}')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    pythonStrategyApi
      .getParams(strategyId)
      .then((params) => {
        const formatted = JSON.stringify(params, null, 2)
        setText(formatted)
        setSaved(formatted)
      })
      .catch(() => setError('Could not load parameters'))
  }, [strategyId])

  const handleSave = async () => {
    let params: unknown
    try {
      params = JSON.parse(text)
    } catch {
      setError('Not valid JSON')
      return
    }
    if (params === null || typeof params !== 'object' || Array.isArray(params)) {
      setError('Parameters must be a JSON object, e.g. {"quantity": 1}')
      return
    }
    setError(null)
    setSaving(true)
    try {
      const response = await pythonStrategyApi.saveParams(
        strategyId,
        params as Record<string, unknown>
      )
      if (response.status === 'success') {
        const formatted = JSON.stringify(params, null, 2)
        setText(formatted)
        setSaved(formatted)
        showToast.success(response.message || 'Parameters saved', 'pythonStrategy')
      } else {
        setError(response.message || 'Could not save parameters')
      }
    } catch (e) {
      const err = e as { response?: { data?: { message?: string } } }
      setError(err.response?.data?.message || 'Could not save parameters')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Parameters</CardTitle>
        <CardDescription className="text-xs">
          A JSON object passed to the script as <code>STRATEGY_PARAMS</code>. Read it with{' '}
          <code>json.loads(os.getenv("STRATEGY_PARAMS", "{'{}'}"))</code>. Applies from the next
          start.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <label htmlFor="strategy-params" className="sr-only">
          Strategy parameters (JSON)
        </label>
        <Textarea
          id="strategy-params"
          value={text}
          onChange={(e) => setText(e.target.value)}
          className="font-mono text-xs min-h-[120px]"
          spellCheck={false}
        />
        {error && <p className="text-xs text-red-500">{error}</p>}
        <div className="flex justify-end">
          <Button size="sm" onClick={handleSave} disabled={saving || text === saved}>
            <Save className="h-4 w-4 mr-2" />
            {saving ? 'Saving...' : 'Save parameters'}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
