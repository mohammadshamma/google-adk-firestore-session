[![Python Tests](https://github.com/mohammadshamma/google-adk-firestore-session/actions/workflows/python-tests.yml/badge.svg)](https://github.com/mohammadshamma/google-adk-firestore-session/actions/workflows/python-tests.yml)
[![Python 3.11+](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fmohammadshamma%2Fgoogle-adk-firestore-session%2Fmain%2Fpyproject.toml)](https://pypi.org/project/google-adk-firestore-session/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

# Google ADK Firestore Session

A specialized implementation of the `BaseSessionService` that uses Google Cloud Firestore for persisting application sessions, user states, and event logs.

## Overview

This package provides the `FirestoreSessionService` class, which handles the lifecycle of sessions within the Google ADK (Agent Development Kit) framework. It organizes data in a hierarchical Firestore structure:

- `apps/{app_name}`: Global application state.
- `apps/{app_name}/users/{user_id}`: Persistent user-specific state.
- `apps/{app_name}/users/{user_id}/sessions/{session_id}`: Individual session data and state.
- `apps/{app_name}/users/{user_id}/sessions/{session_id}/events/{event_id}`: Historical event logs for the session.

## Installation

### Using `pip`
```bash
pip install google-adk-firestore-session
```

### Using `uv`
```bash
uv add google-adk-firestore-session
```

## Configuration

The `FirestoreSessionService` can be configured via constructor arguments, environment variables, or through the built-in ADK factory.

### Precedence
1. **Constructor Arguments**: Explicit `project` and `database` passed to the class.
2. **Environment Variables**: `GOOGLE_CLOUD_PROJECT` and `GOOGLE_DATABASE`.
3. **Default Credentials**: Standard Google Cloud application default credentials.

## Usage with Google ADK

To use the `FirestoreSessionService` with Google ADK, register the provided factory with the ADK `ServiceRegistry`. This allows you to point your agents to Firestore using a simple URI.

### 1. Register the Service

In your main entry point (e.g., `serving.py`), import and register the factory.

```python
from google.adk.cli.service_registry import get_service_registry
from firestore_session import firestore_session_service_factory

# Register the "firestore" scheme
get_service_registry().register_session_service(
    "firestore", 
    firestore_session_service_factory
)
```

### 2. Configure the ADK App

Pass the URI to `get_fast_api_app`. The URI format is `firestore://[project-id]/[database-name]`.

```python
from google.adk.cli.fast_api import get_fast_api_app

app = get_fast_api_app(
    agents_dir="path/to/agents",
    # Connect to a specific project and database instance
    session_service_uri="firestore://my-project-id/my-database", 
    web=True
)
```

## Standalone Usage

```python
from firestore_session import FirestoreSessionService

# Explicitly passing project/database
session_service = FirestoreSessionService(
    project="my-project-id",
    database="my-database"
)

# Or use environment variables (GOOGLE_CLOUD_PROJECT, etc.)
session_service = FirestoreSessionService()
```

## Dependencies

- `google-adk`: Core agent framework. Compatible with both the 1.x and 2.x
  release lines (`>=1.26.0,<3.0`).
- `google-cloud-firestore`: Official Firestore client.

### Compatibility note

Events are persisted as self-describing JSON documents (via `Event.model_dump`
/ `Event.model_validate`), so the new Event fields introduced in ADK 2.x
(e.g. `output`, `environment_id`, `node_info`) round-trip automatically with no
schema change. Sessions and events written by ADK 1.x remain readable under 2.x
and vice versa; no data migration is required when upgrading.

## Development

### Running Tests
Tests use `pytest`. Ensure you have a Firestore emulator or a project configured for testing.

```bash
pytest tests/
```
