import os
import pytest
import uuid
import time
from google.cloud import firestore
from firestore_session.firestore_session_service import FirestoreSessionService
from google.adk.events.event import Event
from google.adk.sessions.base_session_service import GetSessionConfig
from google.adk.sessions.state import State

import subprocess
import socket
import time
import requests
import pytest_asyncio

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]

@pytest_asyncio.fixture(scope="session")
def firestore_emulator():
    """Starts the Firestore emulator for the test session."""
    host = "localhost"
    port = get_free_port()
    host_port = f"{host}:{port}"
    
    # Start emulator
    cmd = [
        "gcloud", 
        "emulators", 
        "firestore", 
        "start", 
        f"--host-port={host_port}"
    ]
    
    # Start process
    proc = subprocess.Popen(
        cmd, 
        stdout=subprocess.PIPE, 
        stderr=subprocess.PIPE
    )
    
    # Wait for emulator to be ready
    start_time = time.time()
    ready = False
    while time.time() - start_time < 20: # 20s timeout
        try:
            # Check if port is listening/responding
            response = requests.get(f"http://{host_port}/", timeout=1)
            if response.status_code == 200 or response.status_code == 404: # 404 is fine, server is up
                ready = True
                break
        except requests.exceptions.ConnectionError:
            time.sleep(0.5)
            
    if not ready:
        proc.kill()
        raise RuntimeError(f"Firestore emulator failed to start on {host_port}")

    # Set env vars for the session
    os.environ["FIRESTORE_EMULATOR_HOST"] = host_port
    os.environ["GCLOUD_PROJECT"] = "test-project"

    yield host_port
    
    # Cleanup
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    
    # Unset env vars
    del os.environ["FIRESTORE_EMULATOR_HOST"]
    del os.environ["GCLOUD_PROJECT"]

@pytest_asyncio.fixture
async def firestore_client(firestore_emulator):
    # Connect to emulator
    client = firestore.AsyncClient(project="test-project")
    yield client


@pytest_asyncio.fixture
async def service(firestore_emulator):
    return FirestoreSessionService(project="test-project")

@pytest.mark.asyncio
async def test_create_session(service):
    app_name = "test-app"
    user_id = f"user-{uuid.uuid4()}"
    
    session = await service.create_session(app_name=app_name, user_id=user_id)
    
    assert session.app_name == app_name
    assert session.user_id == user_id
    assert session.id is not None
    # Check persistence
    fetched = await service.get_session(app_name=app_name, user_id=user_id, session_id=session.id)
    assert fetched is not None
    assert fetched.id == session.id

@pytest.mark.asyncio
async def test_state_management(service, firestore_client):
    app_name = "test-app-state"
    user_id = f"user-{uuid.uuid4()}"
    
    # Pre-seed App State
    await firestore_client.collection("apps").document(app_name).set({"global_config": "true"})
    
    # Pre-seed User State
    await firestore_client.collection("apps").document(app_name).collection("users").document(user_id).set({"user_pref": "dark_mode"})

    session = await service.create_session(app_name=app_name, user_id=user_id, state={"session_var": 123})
    
    # Verify merged state
    assert session.state.get("app:global_config") == "true"
    assert session.state.get("user:user_pref") == "dark_mode"
    assert session.state.get("session_var") == 123

@pytest.mark.asyncio
async def test_append_event_and_state_updates(service):
    app_name = "test-app-events"
    user_id = f"user-{uuid.uuid4()}"
    session = await service.create_session(app_name=app_name, user_id=user_id)
    
    # Create an event with state updates
    event = Event(
        author="agent",
        actions={
            "state_delta": {
                "app:seen_count": 1,
                "user:last_visit": "today",
                "session_step": 2
            }
        }
    )
    
    await service.append_event(session, event)
    
    # Verify Event persistence
    fetched = await service.get_session(app_name=app_name, user_id=user_id, session_id=session.id)
    assert len(fetched.events) == 1
    assert fetched.events[0].id == event.id
    
    # Verify State Persistence
    # 1. Session Doc
    session_doc = await service._client.collection("apps").document(app_name)\
        .collection("users").document(user_id)\
        .collection("sessions").document(session.id).get()
    session_data = session_doc.to_dict()
    assert session_data["state"]["session_step"] == 2
    
    # 2. App Doc
    app_doc = await service._client.collection("apps").document(app_name).get()
    assert app_doc.to_dict()["seen_count"] == 1
    
    # 3. User Doc
    user_doc = await service._client.collection("apps").document(app_name)\
        .collection("users").document(user_id).get()
    assert user_doc.to_dict()["last_visit"] == "today"

@pytest.mark.asyncio
async def test_event_fields_round_trip(service):
    """Events are persisted as JSON blobs, so all Event fields must survive a
    write/read cycle. This guards ADK 2.x, which added new Event fields
    (e.g. output, environment_id, node_info) that older column-oriented
    storage would have dropped."""
    app_name = "test-app-event-fields"
    user_id = f"user-{uuid.uuid4()}"
    session = await service.create_session(app_name=app_name, user_id=user_id)

    # Build an event populating whichever fields the installed ADK exposes.
    # ADK 2.x introduced these; on 1.x they are simply skipped.
    event_kwargs = {"author": "agent", "invocation_id": "inv-xyz"}
    expected = {}
    if "output" in Event.model_fields:
        event_kwargs["output"] = {"result": "done", "score": 0.9}
        expected["output"] = {"result": "done", "score": 0.9}
    if "environment_id" in Event.model_fields:
        event_kwargs["environment_id"] = "env-42"
        expected["environment_id"] = "env-42"

    event = Event(**event_kwargs)
    await service.append_event(session, event)

    fetched = await service.get_session(
        app_name=app_name, user_id=user_id, session_id=session.id
    )
    assert len(fetched.events) == 1
    loaded = fetched.events[0]
    assert loaded.id == event.id
    assert loaded.invocation_id == "inv-xyz"
    for field, value in expected.items():
        assert getattr(loaded, field) == value

@pytest.mark.asyncio
async def test_list_sessions(service):
    app_name = "test-app-list"
    user_id = f"user-{uuid.uuid4()}"
    
    s1 = await service.create_session(app_name=app_name, user_id=user_id)
    time.sleep(0.01)
    s2 = await service.create_session(app_name=app_name, user_id=user_id)
    
    response = await service.list_sessions(app_name=app_name, user_id=user_id)
    ids = [s.id for s in response.sessions]
    assert ids == [s1.id, s2.id]

@pytest.mark.asyncio
async def test_temp_state_in_memory_and_trimmed_from_persistence(service):
    app_name = "test-app-temp-state"
    user_id = f"user-{uuid.uuid4()}"
    session = await service.create_session(app_name=app_name, user_id=user_id)

    event = Event(
        author="agent",
        actions={
            "state_delta": {
                "temp:scratch": "ephemeral",
                "session_step": 1,
            }
        },
    )
    returned_event = await service.append_event(session, event)

    # In-memory session state must retain temp: keys for downstream agents
    assert session.state.get("temp:scratch") == "ephemeral"
    assert session.state.get("session_step") == 1

    # Returned event and in-memory session.events must have temp: keys trimmed from state_delta
    assert "temp:scratch" not in (returned_event.actions.state_delta or {})
    assert "temp:scratch" not in (session.events[0].actions.state_delta or {})

    # Persisted session document and event document in Firestore must not contain temp: keys
    session_doc = await (
        service._client.collection("apps")
        .document(app_name)
        .collection("users")
        .document(user_id)
        .collection("sessions")
        .document(session.id)
        .get()
    )
    assert "temp:scratch" not in session_doc.to_dict().get("state", {})

    event_doc = await (
        service._client.collection("apps")
        .document(app_name)
        .collection("users")
        .document(user_id)
        .collection("sessions")
        .document(session.id)
        .collection("events")
        .document(returned_event.id)
        .get()
    )
    persisted_delta = event_doc.to_dict().get("actions", {}).get("state_delta", {})
    assert "temp:scratch" not in persisted_delta
    assert persisted_delta.get("session_step") == 1

    # Re-fetching the session from Firestore must not include temp: keys
    fetched = await service.get_session(
        app_name=app_name, user_id=user_id, session_id=session.id
    )
    assert "temp:scratch" not in fetched.state
    assert fetched.state.get("session_step") == 1

@pytest.mark.asyncio
async def test_get_session_config_semantics(service):
    app_name = "test-app-config"
    user_id = f"user-{uuid.uuid4()}"
    session = await service.create_session(app_name=app_name, user_id=user_id)

    e1 = Event(author="user", timestamp=100.0)
    e2 = Event(author="agent", timestamp=200.0)
    e3 = Event(author="user", timestamp=300.0)
    for ev in (e1, e2, e3):
        await service.append_event(session, ev)

    # num_recent_events < 0 raises ValueError (via GetSessionConfig or get_session)
    with pytest.raises(ValueError):
        await service.get_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session.id,
            config=GetSessionConfig(num_recent_events=-1),
        )
    with pytest.raises(ValueError):
        await service.get_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session.id,
            config=GetSessionConfig.model_construct(num_recent_events=-1),
        )

    # num_recent_events == 0 returns no events
    zero_events = await service.get_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session.id,
        config=GetSessionConfig(num_recent_events=0),
    )
    assert zero_events.events == []

    # after_timestamp is inclusive (>=)
    after_200 = await service.get_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session.id,
        config=GetSessionConfig(after_timestamp=200.0),
    )
    assert [e.id for e in after_200.events] == [e2.id, e3.id]

    # Combined after_timestamp and num_recent_events preserves both filters
    combined = await service.get_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session.id,
        config=GetSessionConfig(after_timestamp=200.0, num_recent_events=1),
    )
    assert [e.id for e in combined.events] == [e3.id]

@pytest.mark.asyncio
async def test_unknown_event_fields_tolerated(service):
    """Events written by a newer ADK version with extra fields (top-level or
    inside actions) must still deserialize cleanly rather than being dropped."""
    app_name = "test-app-extra-fields"
    user_id = f"user-{uuid.uuid4()}"
    session = await service.create_session(app_name=app_name, user_id=user_id)

    event = Event(author="agent", timestamp=123.0, actions={"state_delta": {"k": "v"}})
    event_dict = event.model_dump(mode="json", exclude_none=True)
    event_dict["future_adk_field"] = {"nested": "value"}
    event_dict.setdefault("actions", {})["future_action_field"] = True

    await (
        service._client.collection("apps")
        .document(app_name)
        .collection("users")
        .document(user_id)
        .collection("sessions")
        .document(session.id)
        .collection("events")
        .document(event.id)
        .set(event_dict)
    )

    fetched = await service.get_session(
        app_name=app_name, user_id=user_id, session_id=session.id
    )
    assert len(fetched.events) == 1
    assert fetched.events[0].id == event.id
    assert fetched.events[0].actions.state_delta == {"k": "v"}

@pytest.mark.asyncio
async def test_get_user_state(service):
    app_name = "test-app-user-state"
    user_id = f"user-{uuid.uuid4()}"

    # Initially empty
    assert await service.get_user_state(app_name=app_name, user_id=user_id) == {}

    session = await service.create_session(app_name=app_name, user_id=user_id)
    event = Event(
        author="agent",
        actions={"state_delta": {"user:theme": "dark", "session_only": 42}},
    )
    await service.append_event(session, event)

    user_state = await service.get_user_state(app_name, user_id)
    assert user_state == {"theme": "dark"}

@pytest.mark.asyncio
async def test_delete_session(service):
    app_name = "test-app-delete"
    user_id = f"user-{uuid.uuid4()}"
    session = await service.create_session(app_name=app_name, user_id=user_id)
    
    event = Event(author="user")
    await service.append_event(session, event)
    
    await service.delete_session(app_name=app_name, user_id=user_id, session_id=session.id)
    
    fetched = await service.get_session(app_name=app_name, user_id=user_id, session_id=session.id)
    assert fetched is None
    
    # Check events deleted
    events_ref = service._client.collection("apps").document(app_name)\
        .collection("users").document(user_id)\
        .collection("sessions").document(session.id).collection("events")
    snapshots = await events_ref.get()
    assert len(snapshots) == 0
