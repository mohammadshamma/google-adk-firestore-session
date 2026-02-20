# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`firestore-session` is a Firestore-based implementation of Google ADK's `BaseSessionService`. It persists agent sessions, events, and hierarchical state (app/user/session scopes) to Cloud Firestore.

## Commands

**Run all tests:**
```bash
uv run pytest
```

**Run a single test:**
```bash
uv run pytest tests/factory_test.py::test_factory_uri_parsing
```

Tests require the Firestore Emulator (needs Java 21 and `gcloud` CLI). The test fixtures automatically start/stop the emulator.

**Build package:**
```bash
uv run python -m build
```

**Release (from main, clean tree):**
```bash
./release.sh 0.2.0
```

No linter or formatter is configured.

## Architecture

### Firestore Data Model

```
apps/{app_name}                          → app-level state (prefixed app: in code)
  └─ users/{user_id}                     → user-level state (prefixed user: in code)
       └─ sessions/{session_id}          → session data + session-scoped state
            └─ events/{event_id}         → individual event documents
```

### State Hierarchy

State is stored at three levels using key prefixes defined in `google.adk.sessions.state.State`:
- `app:` keys → persisted in the app document, shared across all users
- `user:` keys → persisted in the user document, shared across sessions
- `temp:` keys → excluded from persistence entirely
- Unprefixed keys → persisted in the session document

`append_event` writes state deltas to the correct Firestore documents via batch writes. `_merge_state` reconstructs the full merged state when reading sessions.

### Source Layout

- `src/firestore_session/firestore_session_service.py` — Core `FirestoreSessionService` class (all async methods)
- `src/firestore_session/factory.py` — URI factory: parses `firestore://[project]/[database]` for ADK registration
- `tests/firestore_session_service_test.py` — Integration tests against Firestore Emulator
- `tests/factory_test.py` — Unit tests for URI parsing

### Key Conventions

- All public service methods are async (uses `firestore.AsyncClient`)
- Batch writes for atomic multi-document updates in `append_event`
- Events are stored in a subcollection, not in the session document
- Tests use UUID-based identifiers to avoid collisions between test runs
- Build backend is Hatchling (`pyproject.toml`), Python 3.11+
