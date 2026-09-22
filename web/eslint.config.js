import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs['flat/recommended'],
      reactRefresh.configs.vite
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser
    },
    rules: {
      // Context providers and hook files commonly export both a component
      // (the Provider) and a hook (useContext). Allow constant exports so
      // these don't need to be split into separate files.
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // TODO: upgrade these react-hooks v7 rules from 'warn' to 'error' after
      // refactoring set-state-in-effect, ref-as-instance-var, and manual
      // memoization patterns in the web codebase.
      'react-hooks/set-state-in-effect': 'warn',
      'react-hooks/refs': 'warn',
      'react-hooks/preserve-manual-memoization': 'warn',
      'react-hooks/static-components': 'warn'
    }
  },
  // ── Design-system guard for Agent Runtime feature surfaces ────────────────
  // Mechanically prevents visual fragmentation: any option/field a future
  // session adds under the runtime feature dir must use DS semantic tokens and
  // the shared icon registry — never a raw hex color or a direct icon import.
  {
    files: ['src/pages/runtime/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          paths: [
            {
              name: 'lucide-react',
              message: 'Import icons from @/lib/runtime-icons (the shared registry), not lucide-react directly.'
            },
            {
              name: '@tabler/icons-react',
              message: 'Import icons from @/lib/runtime-icons (the shared registry).'
            }
          ]
        }
      ],
      'no-restricted-syntax': [
        'error',
        {
          selector: 'Literal[value=/#[0-9a-fA-F]{3,8}/]',
          message:
            'No raw hex colors in feature code — use design-system semantic tokens (bg-card, text-foreground, text-muted-foreground, border-border, --ui-*).'
        },
        {
          selector: 'TemplateElement[value.raw=/#[0-9a-fA-F]{3,8}/]',
          message: 'No raw hex colors in feature code — use design-system semantic tokens.'
        }
      ]
    }
  }
])
