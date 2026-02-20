import os
import pytest
import uuid
import time
from google.cloud import firestore
from firestore_session.firestore_session_service import FirestoreSessionService
from google.adk.events.event import Event
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
async def test_list_sessions(service):
    app_name = "test-app-list"
    user_id = f"user-{uuid.uuid4()}"
    
    s1 = await service.create_session(app_name=app_name, user_id=user_id)
    s2 = await service.create_session(app_name=app_name, user_id=user_id)
    
    response = await service.list_sessions(app_name=app_name, user_id=user_id)
    ids = [s.id for s in response.sessions]
    assert s1.id in ids
    assert s2.id in ids

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
