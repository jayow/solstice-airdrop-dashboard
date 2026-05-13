import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Apple-like neutral scale (cool near-blacks, precise grays)
        ink: {
          950: '#0a0a0c',    // page background
          900: '#111114',    // primary surface
          850: '#17171b',    // elevated surface
          800: '#1d1d22',    // hover state
          700: '#2a2a31',    // separator strong
          600: '#3a3a43',
          500: '#54545c',
          400: '#7a7a83',    // muted body text
          300: '#9b9ba3',    // label text
          200: '#c9c9d0',    // primary text (secondary)
          100: '#e8e8ed',    // primary text
          50:  '#f5f5f7',    // highest emphasis
        },
        // Single warm accent, Solstice-lineage but restrained
        accent: {
          600: '#d97706',
          500: '#f59e0b',
          400: '#fbbf24',
          300: '#fcd34d',
        },
        // Semantic
        good: '#30d158',
        warn: '#ffd60a',
        bad:  '#ff453a',
      },
      fontFamily: {
        sans: ['-apple-system', 'BlinkMacSystemFont', 'SF Pro Display', 'SF Pro Text', 'Inter', 'system-ui', 'sans-serif'],
        mono: ['SF Mono', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      letterSpacing: {
        tight2: '-0.022em',
        tight3: '-0.03em',
      },
      borderColor: {
        DEFAULT: 'rgba(255,255,255,0.06)',
      },
    },
  },
  plugins: [],
};
export default config;
