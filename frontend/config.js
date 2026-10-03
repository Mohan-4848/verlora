// Where the portal finds its backend API.
// • Served by the backend itself (http://localhost:8000 or the ngrok URL) → same origin, nothing to set.
// • Published as a static site (GitHub Pages) → the backend's public tunnel URL below.
window.VYAPAR_API_BASE = location.hostname.endsWith('github.io')
  ? 'https://enviable-hull-stiffen.ngrok-free.dev'
  : '';
