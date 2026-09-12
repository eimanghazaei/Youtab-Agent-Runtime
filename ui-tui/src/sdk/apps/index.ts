/** Reference apps. Importing this module registers them (defineWidgetApp
 *  runs at module load) — appLayout imports it once at startup. User widgets
 *  from $YOUTAB_AGENT_HOME/tui-widgets ride the same import (async, non-fatal). */
import { loadUserWidgets, watchUserWidgets } from '../userWidgets.js'

// Test workers import this registry many times. They must not read or watch the
// operator's real widget directory; watcher accumulation can also exhaust the
// process file-descriptor budget before the security suite completes.
if (!process.env.VITEST) {
  void loadUserWidgets()
  watchUserWidgets()
}

export { dialogTestApp } from './dialogTest.js'
export { gridTestApp } from './gridTest.js'
export { GRID_STREAM_COUNT, type GridTestState } from './gridTestState.js'
export { tickerApp, type TickerState } from './ticker.js'
export { weatherApp, type WeatherState } from './weather.js'
