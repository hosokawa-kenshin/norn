import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './norn.css'
import App from './NornApp.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
