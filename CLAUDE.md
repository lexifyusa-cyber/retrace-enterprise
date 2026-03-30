# Project Rules

## Package Manager
- Always use npm for the frontend

## Frontend (React + TypeScript + Vite + Tailwind)
- Run typecheck (`npm run typecheck`) before running tests
- Run lint (`npm run lint`) before committing frontend changes
- Use Zustand for state management, not React context
- Use TanStack Query for server state, not manual useEffect fetching
- Use Tailwind classes, not inline styles or separate CSS files

## Backend (Python)
- Always activate venv before running Python commands
- Use the existing requirements.txt — do not add packages without updating it

## General
- Start the app using scripts/start-dev.sh, not manual setup
