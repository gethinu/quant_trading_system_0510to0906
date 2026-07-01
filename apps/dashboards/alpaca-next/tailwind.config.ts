import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './lib/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        card: '#1e2130',
        cardfg: '#e6e9f0',
        ok: '#4ade80',
        warn: '#facc15',
        fail: '#f87171',
        muted: '#94a3b8',
      },
    },
  },
  plugins: [],
};
export default config;
