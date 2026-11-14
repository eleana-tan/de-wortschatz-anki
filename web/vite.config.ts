import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Root SPA: assets resolve at /assets/* when nginx serves web/dist at the site root.
// If you ever host under a subpath (e.g. /app/), set base: '/app/' and adjust nginx.
export default defineConfig({
  base: '/',
  plugins: [react()],
})
