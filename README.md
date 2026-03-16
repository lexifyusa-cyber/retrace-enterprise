# ReTrace

**by Lumena Technologies — [lumenatech.io](https://lumenatech.io)**

ReTrace is a distributed knowledge management system that deploys lightweight agents (PODs) to remote machines, processes multi-modal content (code, docs, diagrams, tickets), builds unified knowledge graphs, and enables natural language Q&A with deep contextual understanding.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    MAIN APPLICATION                          │
│               (Electron + FastAPI Backend)                   │
│                                                              │
│  • React/TypeScript UI                                       │
│  • POD orchestration engine                                  │
│  • Query processing pipeline                                 │
│  • LLM integration (OpenAI/Claude)                          │
└─────────────────────────────────────────────────────────────┘
                             │
                    WebSocket (mTLS)
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
   ┌─────────┐         ┌─────────┐         ┌─────────┐
   │   POD   │         │   POD   │         │   POD   │
   │ Agent A │         │ Agent B │         │ Agent C │
   └─────────┘         └─────────┘         └─────────┘
```

## Project Structure

```
retrace-agent/
├── main-app/
│   ├── frontend/          # Electron + React UI
│   │   ├── src/
│   │   │   ├── components/
│   │   │   ├── pages/
│   │   │   ├── hooks/
│   │   │   ├── stores/
│   │   │   └── types/
│   │   └── public/
│   ├── backend/           # FastAPI Python backend
│   │   ├── app/
│   │   │   ├── api/       # REST endpoints
│   │   │   ├── core/      # Core logic
│   │   │   ├── models/    # Database models
│   │   │   ├── services/  # Business logic
│   │   │   └── db/        # Database connections
│   │   └── tests/
│   └── config/
├── pod-agent/             # Go POD agent
│   ├── cmd/               # Entry points
│   ├── internal/
│   │   ├── api/           # POD API handlers
│   │   ├── communication/ # WebSocket client
│   │   ├── storage/       # Vector/Graph/Metadata DBs
│   │   ├── filewatch/     # File system watcher
│   │   └── processor/     # File processing
│   └── configs/
├── shared/                # Shared protocols
│   ├── protocols/
│   └── types/
└── docs/
```

## Quick Start

### Prerequisites

- Node.js 18+
- Python 3.11+
- Go 1.21+
- PostgreSQL 15+ (or SQLite for development)

### Main App Setup

```bash
# Backend
cd main-app/backend
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -r requirements.txt
cp .env.example .env  # Configure your settings

# Start backend
uvicorn app.main:app --reload --port 8000

# Frontend (in another terminal)
cd main-app/frontend
npm install
npm run dev
```

### POD Agent Setup

```bash
cd pod-agent
go mod download
go build -o retrace-agent ./cmd/agent

# Run agent
./retrace-agent --config configs/config.yaml
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string | `sqlite:///./retrace.db` |
| `OPENAI_API_KEY` | OpenAI API key for embeddings/LLM | Required |
| `ANTHROPIC_API_KEY` | Claude API key (optional) | - |
| `JWT_SECRET` | Secret for JWT tokens | Generated |
| `WEBSOCKET_PORT` | WebSocket server port | `8001` |

## Development

### Running Tests

```bash
# Backend tests
cd main-app/backend
pytest

# Frontend tests
cd main-app/frontend
npm test

# POD Agent tests
cd pod-agent
go test ./...
```

### Building for Production

```bash
# Build frontend
cd main-app/frontend
npm run build

# Build POD agent for multiple platforms
cd pod-agent
./scripts/build-all.sh
```

## License

MIT License - See LICENSE file for details.

---

*Built with care by Lumena Technologies*
