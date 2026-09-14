# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DeerFlow Frontend is a Next.js 16 web interface for an AI agent system. It communicates with a LangGraph-based backend to provide thread-based AI conversations with streaming responses, artifacts, and a skills/tools system.

**Stack**: Next.js 16, React 19, TypeScript 5.8, Tailwind CSS 4, pnpm 10.26.2

## Commands

| Command          | Purpose                                           |
| ---------------- | ------------------------------------------------- |
| `pnpm dev`       | Dev server with Turbopack (http://localhost:3000) |
| `pnpm build`     | Production build                                  |
| `pnpm check`     | Lint + type check (run before committing)         |
| `pnpm lint`      | ESLint only                                       |
| `pnpm lint:fix`  | ESLint with auto-fix                              |
| `pnpm test`      | Run unit tests with Vitest                        |
| `pnpm test:e2e`  | Run E2E tests with Playwright (Chromium)          |
| `pnpm typecheck` | TypeScript type check (`tsc --noEmit`)            |
| `pnpm start`     | Start production server                           |

Unit tests live under `tests/unit/` and mirror the `src/` layout (e.g., `tests/unit/core/api/stream-mode.test.ts` tests `src/core/api/stream-mode.ts`). Powered by Vitest; import source modules via the `@/` path alias.

E2E tests live under `tests/e2e/` and use Playwright with Chromium. They mock all backend APIs via `page.route()` network interception and test real page interactions (navigation, chat input, streaming responses). Config: `playwright.config.ts`.

## Architecture

```
Frontend (Next.js) ──▶ LangGraph SDK ──▶ LangGraph Backend (lead_agent)
                                              ├── Sub-Agents
                                              └── Tools & Skills
```

The frontend is a stateful chat application. Users create **threads** (conversations), send messages, and receive streamed AI responses. The backend orchestrates agents that can produce **artifacts** (files/code) and **todos**.

### Source Layout (`src/`)

- **`app/`** — Next.js App Router. Routes: `/` (redirects to `/workspace`), `/login` + `/setup` (auth), `/workspace/chats/[thread_id]` (chat). Marketing landing/blog/docs pages were removed — only the post-login harness remains.
- **Tender agent** — the “招投标智能体” sidebar entry opens the standard custom-agent chat at `/workspace/agents/tender-review/chats/new`; `/workspace/tender-review` is a compatibility redirect. The repository-managed agent definition lives at `../agents/tender-review/`, selects its own Lead prompt through `system_prompt_path`, and forces `subagent_enabled: false` while retaining the standard upload/history/artifact UI.
- **Long upload proxying** — Next rewrites default to a 30-second upstream timeout, which is shorter than MinerU parsing for large PDFs. `next.config.js` sets `experimental.proxyTimeout` to one hour by default; override it with `DEER_FLOW_PROXY_TIMEOUT_MS`. Do not remove this while tender upload parsing is synchronous.
- **Attachment reader** — uploaded file cards open `DocumentPreviewPanel` in the resizable right-hand chat panel. PDF and images use their original authenticated artifact URL; text is fetched through the authenticated API wrapper; Office files use `markdown_virtual_path` from the upload response. Keep original and preview paths separate so tender PDFs remain visually faithful while Agent text extraction continues to use MinerU Markdown.
- **Tender evidence cards** — `core/tender-review/findings.ts` validates the fenced `tender-review` JSON contract before `TenderReviewFindings` renders it. Never execute or trust paths from model output: only `/mnt/user-data/...` artifact paths and HTTP(S) source URLs are accepted. Evidence clicks reuse `DocumentPreviewPanel` with physical page and exact-quote anchors.
- **`components/`** — React components split into:
  - `ui/` — Shadcn UI primitives (auto-generated, ESLint-ignored)
  - `ai-elements/` — Vercel AI SDK elements (auto-generated, ESLint-ignored)
  - `workspace/` — Chat page components (messages, artifacts, settings)
- **`core/`** — Business logic, the heart of the app:
  - `threads/` — Thread creation, streaming, state management (hooks + types)
  - `api/` — LangGraph client singleton
  - `artifacts/` — Artifact loading and caching
  - `i18n/` — Internationalization (en-US, zh-CN)
  - `settings/` — User preferences in localStorage
  - `memory/` — Persistent user memory system
  - `skills/` — Skills installation and management
  - `messages/` — Message processing and transformation
  - `mcp/` — Model Context Protocol integration
  - `models/` — TypeScript types and data models
- **`hooks/`** — Shared React hooks
- **`lib/`** — Utilities (`cn()` from clsx + tailwind-merge)
- **`server/`** — Server-side code (better-auth, not yet active)
- **`styles/`** — Global CSS with Tailwind v4 `@import` syntax and CSS variables for theming

### Data Flow

1. User input → thread hooks (`core/threads/hooks.ts`) → LangGraph SDK streaming
2. Stream events update thread state (messages, artifacts, todos)
3. TanStack Query manages server state; localStorage stores user settings
4. Components subscribe to thread state and render updates

Tender review uses the normal LangGraph conversation stream. Its separate task REST/SSE contract remains a backend capability for future structured cards; do not build a second full-page workbench that duplicates the standard chat UI.

### Key Patterns

- **Server Components by default**, `"use client"` only for interactive components
- **Thread hooks** (`useThreadStream`, `useSubmitThread`, `useThreads`) are the primary API interface
- **LangGraph client** is a singleton obtained via `getAPIClient()` in `core/api/`
- **Environment validation** uses `@t3-oss/env-nextjs` with Zod schemas (`src/env.js`). Skip with `SKIP_ENV_VALIDATION=1`

## Code Style

- **Imports**: Enforced ordering (builtin → external → internal → parent → sibling), alphabetized, newlines between groups. Use inline type imports: `import { type Foo }`.
- **Unused variables**: Prefix with `_`.
- **Class names**: Use `cn()` from `@/lib/utils` for conditional Tailwind classes.
- **Path alias**: `@/*` maps to `src/*`.
- **Components**: `ui/` and `ai-elements/` are generated from registries (Shadcn, MagicUI, React Bits, Vercel AI SDK) — don't manually edit these.

## Environment

Backend API URLs are optional; an nginx proxy is used by default:

```
NEXT_PUBLIC_BACKEND_BASE_URL=http://localhost:8001
NEXT_PUBLIC_LANGGRAPH_BASE_URL=http://localhost:2024
```

Requires Node.js 22+ and pnpm 10.26.2+.
