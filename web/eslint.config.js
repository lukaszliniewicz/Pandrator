import js from '@eslint/js';
import svelte from 'eslint-plugin-svelte';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    ignores: [
      '.svelte-kit/**',
      'build/**',
      'graphify-out/**',
      'node_modules/**',
      'playwright-report/**',
      'src/lib/api.generated.ts',
      'test-results/**'
    ]
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...svelte.configs['flat/recommended'],
  {
    files: ['**/*.{ts,svelte}'],
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.es2024
      },
      parserOptions: {
        parser: tseslint.parser
      }
    },
    rules: {
      '@typescript-eslint/consistent-type-imports': [
        'error',
        { fixStyle: 'inline-type-imports' }
      ],
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
          varsIgnorePattern: '^_'
        }
      ],
      'no-undef': 'off',
      'svelte/no-at-html-tags': 'error',
      'svelte/no-navigation-without-resolve': 'off',
      // The rule also reports short-lived local Set and URLSearchParams values,
      // which never participate in Svelte reactivity. Reactive collections are
      // reviewed explicitly instead of forcing Svelte wrappers everywhere.
      'svelte/prefer-svelte-reactivity': 'off',
      'svelte/require-each-key': 'off'
    }
  },
  {
    files: ['*.ts', 'tests/**/*.ts'],
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node
      }
    }
  },
  {
    files: ['**/*.js'],
    ...tseslint.configs.disableTypeChecked,
    languageOptions: {
      globals: globals.node
    }
  }
);
