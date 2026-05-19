/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  // Class-based dark mode — driven by `<html class="dark">` from useTheme.
  // The merchant explicitly toggles light/dark/system in the Header; setting
  // this to 'class' (instead of 'media') lets that choice override the OS
  // preference and stay consistent inside Salla's embedded iframe.
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        brand: {
          50:  '#fffbeb',
          100: '#fef3c7',
          200: '#fde68a',
          300: '#fcd34d',
          400: '#fbbf24',
          500: '#f59e0b',
          600: '#d97706',
          700: '#b45309',
          800: '#92400e',
          900: '#78350f',
        },
      },
      fontFamily: {
        sans:  ['var(--font-ui)', 'system-ui', 'sans-serif'],
        cairo: ['Cairo', 'system-ui', 'sans-serif'],
        inter: ['Inter', 'system-ui', 'sans-serif'],
      },
      // Dynamic viewport height (100dvh) — prevents iOS address-bar resize jump
      height: {
        dvh:    '100dvh',
        'svh':  '100svh',
        'lvh':  '100lvh',
      },
      minHeight: {
        dvh:   '100dvh',
        'svh': '100svh',
        'lvh': '100lvh',
      },
      screens: {
        xs: '375px',   // iPhone SE / small Android
      },
    },
  },
  plugins: [],
}
