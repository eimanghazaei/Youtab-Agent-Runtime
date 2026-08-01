import { useQuery } from '@tanstack/react-query'

import { queryClient, writeCache } from '@/lib/query-client'
import type { YoutabConfigRecord } from '@/types/youtab'
import { getYoutabConfigRecord } from '@/youtab'

// One shared cache for the whole profile config record (`GET /api/config`).
// Every settings surface (MCP, model, config) reads and writes through this key
// so a save in one shows in the others, and revisiting a tab paints the cache
// instead of blanking on a fresh fetch.
//
// Distinct from session/hooks/use-youtab-config.ts, which is side-effecting —
// it pushes personality/cwd/voice/… into the session stores for live chat.
export const YOUTAB_AGENT_CONFIG_KEY = ['youtab-config-record'] as const

// staleTime 0 → serve cache instantly, background-revalidate on every mount.
export const useYoutabConfigRecord = () =>
  useQuery({ queryKey: YOUTAB_AGENT_CONFIG_KEY, queryFn: getYoutabConfigRecord, staleTime: 0 })

export const setYoutabConfigCache = writeCache<YoutabConfigRecord>(YOUTAB_AGENT_CONFIG_KEY)

export const invalidateYoutabConfig = () => queryClient.invalidateQueries({ queryKey: YOUTAB_AGENT_CONFIG_KEY })
