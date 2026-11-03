import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html', 'lcov'],
      exclude: [
        'node_modules/',
        'src/test/',
        '**/*.config.*',
        '**/*.spec.*',
        '**/*.test.*',
        '**/dist/',
        '**/build/',
        'src/main.jsx',
        'src/App.jsx',
        'src/pages/**/*',
        'src/contexts/**/*',
        'src/extensions/**/*',
        'src/theme.js',
        'src/config.js',
      ],
      // Recalibrated for vitest 5. Coverage now counts only files imported
      // by tests, where v2 counted every matching file, and v8 branch
      // counting is AST-aware rather than approximate. The old figures
      // (lines/statements 15, branches 70) described a different
      // population and were not a meaningful gate.
      //
      // Set just under the measured values: statements 82.7, lines 85.1,
      // functions 81.6, branches 62.0. Weakest areas are ProfileMenu
      // (22% branch), useRun (35%) and userService (0%).
      thresholds: {
        lines: 80,
        functions: 75,
        branches: 58,
        statements: 78,
      },
    },
    include: ['**/*.{test,spec}.{js,jsx,ts,tsx}'],
    exclude: ['node_modules', 'dist', 'build', '.idea', '.git', '.cache'],
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
});
