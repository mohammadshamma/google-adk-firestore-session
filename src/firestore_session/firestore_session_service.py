# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Any, Optional, AsyncGenerator
import uuid
import time
import copy
from google.cloud import firestore
from google.adk.sessions.base_session_service import BaseSessionService
from google.adk.sessions.base_session_service import GetSessionConfig
from google.adk.sessions.base_session_service import ListSessionsResponse
from google.adk.sessions.session import Session
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.sessions.state import State

class FirestoreSessionService(BaseSessionService):
    """
    A Firestore-based implementation of the SessionService.
    
    Persists sessions, events, and state to Google Cloud Firestore.
    structure:
    apps/{app_name} (App State)
    apps/{app_name}/users/{user_id} (User State)
    apps/{app_name}/users/{user_id}/sessions/{session_id} (Session Data + Session State)
    apps/{app_name}/users/{user_id}/sessions/{session_id}/events/{event_id} (Events)
    """

    def __init__(
        self, 
        project: Optional[str] = None,
        database: Optional[str] = None
    ):
        """
        Args:
            project: Optional Google Cloud Project ID.
            database: Optional Firestore database instance name.
        """
        # Initialize the client with provided project/database identifiers
        # If identifiers are None, AsyncClient uses GOOGLE_CLOUD_PROJECT
        # and GOOGLE_DATABASE env vars or default credentials.
        self._client = firestore.AsyncClient(project=project, database=database)

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        session_id = (
            session_id.strip() if session_id and session_id.strip() else str(uuid.uuid4())
        )
        
        session_ref = self._client.collection("apps").document(app_name)\
            .collection("users").document(user_id)\
            .collection("sessions").document(session_id)

        current_time = time.time()
        initial_session_state = state or {}
        
        # We don't store events in the session document, they go to a subcollection
        session_data = {
            "id": session_id,
            "app_name": app_name,
            "user_id": user_id,
            "state": initial_session_state,
            "last_update_time": current_time,
            # We don't persist 'events' list in the document, it's reconstructed
        }

        # Create the session document
        await session_ref.set(session_data)

        # Create the local Session object to return
        session = Session(
            app_name=app_name,
            user_id=user_id,
            id=session_id,
            state=initial_session_state,
            last_update_time=current_time,
            events=[]
        )

        return await self._merge_state(app_name, user_id, session)

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        if (
            config
            and config.num_recent_events is not None
            and config.num_recent_events < 0
        ):
            raise ValueError("num_recent_events must be non-negative")

        session_ref = self._client.collection("apps").document(app_name)\
            .collection("users").document(user_id)\
            .collection("sessions").document(session_id)

        doc_snapshot = await session_ref.get()
        if not doc_snapshot.exists:
            return None

        session_data = doc_snapshot.to_dict()
        # Events are not stored in the main doc
        session_data["events"] = [] 
        
        session = self._validate_session(session_data)
        if session is None:
            return None

        if config and config.num_recent_events == 0:
            session.events = []
            return await self._merge_state(app_name, user_id, session)

        # Fetch events
        events_ref = session_ref.collection("events")
        query = events_ref

        if config and config.after_timestamp is not None:
            query = query.where(
                filter=firestore.FieldFilter("timestamp", ">=", config.after_timestamp)
            )
        if config and config.num_recent_events is not None:
            query = query.order_by(
                "timestamp", direction=firestore.Query.DESCENDING
            ).limit(config.num_recent_events)
        else:
            query = query.order_by("timestamp")
        
        # Execute query
        events_snapshots = await query.get()
        
        loaded_events = []
        for snap in events_snapshots:
            event_data = snap.to_dict()
            event = self._validate_event(event_data)
            if event is not None:
                loaded_events.append(event)

        if config and config.num_recent_events is not None:
            loaded_events.sort(key=lambda e: e.timestamp)

        session.events = loaded_events
        
        return await self._merge_state(app_name, user_id, session)

    async def list_sessions(
        self, *, app_name: str, user_id: str
    ) -> ListSessionsResponse:
        sessions_ref = self._client.collection("apps").document(app_name)\
            .collection("users").document(user_id)\
            .collection("sessions")
        
        query = sessions_ref.order_by("last_update_time", direction=firestore.Query.ASCENDING)
        
        snapshots = await query.get()
        sessions_list = []
        
        for snap in snapshots:
            data = snap.to_dict()
            data["events"] = [] # Don't load events for listing
            session = self._validate_session(data)
            if session is not None:
                merged_session = await self._merge_state(app_name, user_id, session)
                sessions_list.append(merged_session)
                
        return ListSessionsResponse(sessions=sessions_list)

    async def get_user_state(
        self, app_name: str, user_id: str
    ) -> dict[str, Any]:
        user_ref = (
            self._client.collection("apps")
            .document(app_name)
            .collection("users")
            .document(user_id)
        )
        user_snap = await user_ref.get()
        if not user_snap.exists:
            return {}
        return user_snap.to_dict() or {}

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        session_ref = self._client.collection("apps").document(app_name)\
            .collection("users").document(user_id)\
            .collection("sessions").document(session_id)
            
        events_ref = session_ref.collection("events")
        await self._delete_collection(events_ref, batch_size=50)
        await session_ref.delete()

    async def append_event(self, session: Session, event: Event) -> Event:
        if event.partial:
            return event

        # Ensure temp: state is applied to in-memory session.state even on ADK 1.x
        # where BaseSessionService lacks _apply_temp_state.
        if not hasattr(self, "_apply_temp_state"):
            self._apply_temp_state_fallback(session, event)

        event = await super().append_event(session, event)
        session.last_update_time = event.timestamp

        batch = self._client.batch()
        
        app_name = session.app_name
        user_id = session.user_id
        session_id = session.id
        
        app_ref = self._client.collection("apps").document(app_name)
        user_ref = app_ref.collection("users").document(user_id)
        session_ref = user_ref.collection("sessions").document(session_id)
        event_ref = session_ref.collection("events").document(event.id)

        event_dict = event.model_dump(mode='json', exclude_none=True)
        batch.set(event_ref, event_dict)

        app_updates = {}
        user_updates = {}
        session_updates = {}

        if event.actions and event.actions.state_delta:
            for key, value in event.actions.state_delta.items():
                if not key or key.startswith(State.TEMP_PREFIX):
                    continue
                
                if key.startswith(State.APP_PREFIX):
                    clean_key = key.removeprefix(State.APP_PREFIX)
                    if clean_key:
                        app_updates[clean_key] = value
                elif key.startswith(State.USER_PREFIX):
                    clean_key = key.removeprefix(State.USER_PREFIX)
                    if clean_key:
                        user_updates[clean_key] = value
                else:
                    session_updates[f"state.{key}"] = value

        if app_updates:
            batch.set(app_ref, app_updates, merge=True)
        
        if user_updates:
            batch.set(user_ref, user_updates, merge=True)

        session_updates["last_update_time"] = session.last_update_time
        batch.update(session_ref, session_updates)

        await batch.commit()
        return event

    async def _merge_state(self, app_name: str, user_id: str, session: Session) -> Session:
        app_ref = self._client.collection("apps").document(app_name)
        app_snap = await app_ref.get()
        if app_snap.exists:
            app_data = app_snap.to_dict()
            for k, v in app_data.items():
                session.state[State.APP_PREFIX + k] = v

        user_ref = app_ref.collection("users").document(user_id)
        user_snap = await user_ref.get()
        if user_snap.exists:
            user_data = user_snap.to_dict()
            for k, v in user_data.items():
                session.state[State.USER_PREFIX + k] = v
                
        return session

    async def _delete_collection(self, coll_ref, batch_size):
        docs = await coll_ref.limit(batch_size).get()
        deleted = 0

        for doc in docs:
            await doc.reference.delete()
            deleted += 1

        if deleted >= batch_size:
            return await self._delete_collection(coll_ref, batch_size)

    @staticmethod
    def _apply_temp_state_fallback(session: Session, event: Event) -> None:
        if not event.actions or not event.actions.state_delta:
            return
        for key, value in event.actions.state_delta.items():
            if key.startswith(State.TEMP_PREFIX):
                session.state[key] = value

    @staticmethod
    def _allowed_keys(model_cls: Any) -> set[str]:
        keys = set(model_cls.model_fields.keys())
        for field in model_cls.model_fields.values():
            if field.alias:
                keys.add(field.alias)
        return keys

    @classmethod
    def _validate_event(cls, event_data: dict[str, Any]) -> Optional[Event]:
        try:
            return Event.model_validate(event_data)
        except Exception:
            try:
                allowed = cls._allowed_keys(Event)
                filtered = {k: v for k, v in event_data.items() if k in allowed}
                actions = filtered.get("actions")
                if isinstance(actions, dict):
                    allowed_actions = cls._allowed_keys(EventActions)
                    filtered["actions"] = {
                        k: v for k, v in actions.items() if k in allowed_actions
                    }
                return Event.model_validate(filtered)
            except Exception:
                return None

    @classmethod
    def _validate_session(cls, session_data: dict[str, Any]) -> Optional[Session]:
        try:
            return Session.model_validate(session_data)
        except Exception:
            try:
                allowed = cls._allowed_keys(Session)
                filtered = {
                    k: v for k, v in session_data.items() if k in allowed
                }
                return Session.model_validate(filtered)
            except Exception:
                try:
                    return Session(
                        id=session_data.get("id"),
                        app_name=session_data.get("app_name"),
                        user_id=session_data.get("user_id"),
                        state=session_data.get("state", {}),
                        last_update_time=session_data.get("last_update_time", 0.0),
                        events=[],
                    )
                except Exception:
                    return None