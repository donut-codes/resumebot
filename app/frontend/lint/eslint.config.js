// ESLint for the frontend (app/frontend/src): the TypeScript, React hooks and accessibility rules the compiler does not check.
//
// Run it with `npm --prefix lint run lint` from app/frontend (`py -3 app/scripts/dev.py check --frontend` does).
//
// The findings that existed when the lint was added are recorded in eslint-suppressions.json (ESLint's own bulk
// suppressions), so the run fails only on a NEW finding. When you fix one the file is stale: `npm --prefix lint run
// lint:prune` drops the entries that no longer fire (the run says so too). Do not add to the file by hand; fix the code or
// write an `eslint-disable-next-line <rule> -- <why>` comment where the code is right.
import js from '@eslint/js'
import { defineConfig } from 'eslint/config'
import jsxA11y from 'eslint-plugin-jsx-a11y'
import reactHooks from 'eslint-plugin-react-hooks'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default defineConfig(
  { ignores: ['dist/**', 'node_modules/**', 'lint/**'] },
  js.configs.recommended,
  tseslint.configs.recommended,
  reactHooks.configs.flat.recommended,
  jsxA11y.flatConfigs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: { globals: { ...globals.browser } },
    // The React plugin ships this one as a warning, and a baseline (bulk suppressions) holds back errors only.
    rules: { 'react-hooks/exhaustive-deps': 'error' },
  },
  {
    // The seam a later redesign cuts along (the screens rebuild spec, "The data layer"): a component reads and
    // changes things only through its screen's store (src/state/), never the API client itself. Its types, ApiError
    // and errorMessage stay importable. The screens not yet moved onto a store are held in the suppressions file and
    // leave it as they move.
    files: ['src/components/**/*.{ts,tsx}'],
    ignores: ['src/components/**/*.test.{ts,tsx}'],
    rules: {
      'no-restricted-imports': ['error', {
        patterns: [{
          group: ['**/api/client'],
          importNames: ['api'],
          message: 'A component calls the API through its store (src/state/), not directly.',
        }],
      }],
    },
  },
)
