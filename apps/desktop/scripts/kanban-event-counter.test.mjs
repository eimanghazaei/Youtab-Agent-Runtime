import fs from 'node:fs';
import vm from 'node:vm';
import { expect, it } from 'vitest';

it('counts inherited property names as ordinary task IDs through the real WebSocket handler', async () => {
  const states = [], effects = [];
  let Page, socket;
  const SDK = {
    React: { Component: class {}, createElement: (type, props, ...children) => ({ type, props, children }) },
    components: {}, utils: { cn: () => '', timeAgo: () => '' },
    buildWsUrl: async () => 'ws://synthetic.invalid/events',
    fetchJSON: async () => ({ columns: [] }),
    hooks: {
      useState: initial => {
        const index = states.length;
        let value = typeof initial === 'function' ? initial() : initial;
        // Supply a loaded board to exercise the actual streaming effect.
        if (index === 4) value = { columns: [], tenants: [], assignees: [] };
        states.push(value);
        return [value, update => { states[index] = typeof update === 'function' ? update(states[index]) : update; }];
      },
      useRef: value => ({ current: value }),
      useMemo: callback => callback(),
      useCallback: callback => callback,
      useEffect: (callback, deps) => { effects.push({ callback, deps }); }
    }
  };
  const context = vm.createContext({
    window: {
      __YOUTAB_AGENT_PLUGIN_SDK__: SDK,
      __YOUTAB_AGENT_PLUGINS__: { register: (_name, component) => { Page = component; } },
      localStorage: { getItem: () => 'default' }
    },
    WebSocket: class { constructor() { socket = this; } close() {} },
    setTimeout: () => 1, clearTimeout: () => {}, URLSearchParams, Set, console
  });
  const source = fs.readFileSync(new URL('../../../plugins/kanban/dashboard/dist/index.js', import.meta.url), 'utf8');
  vm.runInContext(source, context);
  Page();
  const streamEffect = effects.find(effect => effect.deps?.length === 3 && effect.deps[0] === true);
  expect(streamEffect).toBeTruthy();
  const disconnect = streamEffect.callback();
  await Promise.resolve();
  for (let index = 0; index < 2; index++) {
    socket.onmessage({ data: JSON.stringify({ cursor: index + 1, events: [
      { task_id: '__proto__' }, { task_id: 'constructor' }, { task_id: 'toString' }, { task_id: 'ordinary-task' }
    ] }) });
  }
  const counters = states.find(value => value && Object.hasOwn(value, 'ordinary-task'));
  expect(counters).toBeTruthy();
  for (const key of ['__proto__', 'constructor', 'toString', 'ordinary-task']) expect(counters[key]).toBe(2);
  expect(Object.getPrototypeOf(counters)).toBeNull();
  disconnect();
});
