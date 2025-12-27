/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // Task state colors
        pending: '#6b7280',
        started: '#3b82f6',
        success: '#22c55e',
        failure: '#ef4444',
        retry: '#f59e0b',
        revoked: '#8b5cf6',
      },
      fontFamily: {
        sans: ['JetBrains Mono', 'ui-monospace', 'monospace'],
      },
    },
  },
  plugins: [],
}
