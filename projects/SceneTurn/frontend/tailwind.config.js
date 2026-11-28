/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: ['class'],
  content: [
    './app/**/*.{js,ts,jsx,tsx,mdx}',
    './pages/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        border: 'hsl(var(--border))',
        input: 'hsl(var(--input))',
        ring: 'hsl(var(--ring))',
        background: 'hsl(var(--background))',
        foreground: 'hsl(var(--foreground))',
        primary: {
          DEFAULT: 'hsl(var(--primary))',
          foreground: 'hsl(var(--primary-foreground))',
        },
        secondary: {
          DEFAULT: 'hsl(var(--secondary))',
          foreground: 'hsl(var(--secondary-foreground))',
        },
        destructive: {
          DEFAULT: 'hsl(var(--destructive))',
          foreground: 'hsl(var(--destructive-foreground))',
        },
        muted: {
          DEFAULT: 'hsl(var(--muted))',
          foreground: 'hsl(var(--muted-foreground))',
        },
        accent: {
          DEFAULT: 'hsl(var(--accent))',
          foreground: 'hsl(var(--accent-foreground))',
        },
        popover: {
          DEFAULT: 'hsl(var(--popover))',
          foreground: 'hsl(var(--popover-foreground))',
        },
        card: {
          DEFAULT: 'hsl(var(--card))',
          foreground: 'hsl(var(--card-foreground))',
        },
        sceneturn: {
          accent: 'rgb(var(--accent-primary-rgb) / <alpha-value>)',
          'accent-hover': 'rgb(var(--accent-hover-rgb) / <alpha-value>)',
          'accent-dim': 'rgb(var(--accent-dim-rgb) / <alpha-value>)',
          canvas: 'rgb(var(--bg-canvas-rgb) / <alpha-value>)',
          sidebar: 'rgb(var(--bg-sidebar-rgb) / <alpha-value>)',
          node: 'rgb(var(--bg-node-rgb) / <alpha-value>)',
          'node-header': 'rgb(var(--bg-node-header-rgb) / <alpha-value>)',
          border: 'rgb(var(--border-node-rgb) / <alpha-value>)',
          divider: 'rgb(var(--border-divider-rgb) / <alpha-value>)',
          text: 'rgb(var(--text-main-rgb) / <alpha-value>)',
          sub: 'rgb(var(--text-sub-rgb) / <alpha-value>)',
          muted: 'rgb(var(--text-muted-rgb) / <alpha-value>)',
        }
      },
      borderRadius: {
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 2px)',
        sm: 'calc(var(--radius) - 4px)',
        'sceneturn-tool': 'var(--radius-sceneturn-tool)',
        'sceneturn-control': 'var(--radius-sceneturn-control)',
        'sceneturn-card': 'var(--radius-sceneturn-card)',
        'sceneturn-panel': 'var(--radius-sceneturn-panel)',
        'sceneturn-pill': 'var(--radius-sceneturn-pill)',
      },
      height: {
        'sceneturn-tool': 'var(--density-sceneturn-tool)',
        'sceneturn-editor': 'var(--density-sceneturn-editor)',
        'sceneturn-control': 'var(--density-sceneturn-control)',
        'sceneturn-action': 'var(--density-sceneturn-action)',
      },
      minHeight: {
        'sceneturn-tool': 'var(--density-sceneturn-tool)',
        'sceneturn-editor': 'var(--density-sceneturn-editor)',
        'sceneturn-control': 'var(--density-sceneturn-control)',
        'sceneturn-action': 'var(--density-sceneturn-action)',
      },
      width: {
        'sceneturn-tool': 'var(--density-sceneturn-tool)',
        'sceneturn-editor': 'var(--density-sceneturn-editor)',
        'sceneturn-control': 'var(--density-sceneturn-control)',
        'sceneturn-action': 'var(--density-sceneturn-action)',
      },
      spacing: {
        'sceneturn-tool': 'var(--space-sceneturn-tool)',
        'sceneturn-compact': 'var(--space-sceneturn-compact)',
        'sceneturn-control': 'var(--space-sceneturn-control)',
        'sceneturn-workspace': 'var(--space-sceneturn-workspace)',
      }
    },
  },
  plugins: [require('tailwindcss-animate')],
}
