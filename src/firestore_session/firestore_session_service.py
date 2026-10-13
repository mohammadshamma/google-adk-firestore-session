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

from typing import Any, Optional, TypeVar
import logging
import uuid
import time
import copy
from google.cloud import firestore
from google.adk.sessions.base_session_service import BaseSessionService
from google.adk.sessions.base_session_service import GetSessionConfig
from google.adk.sessions.base_session_service import ListSessionsResponse
from google.adk.sessions.session import Session
from google.adk.events.event import Event
from google.adk.sessions.state import State
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

_ModelT = TypeVar("_ModelT", bound=BaseModel)

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
        
        session = self._validate_model(
            Session, session_data, doc_snapshot.reference.path
        )
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
            event = self._validate_model(Event, event_data, snap.reference.path)
            if event is not None:
                loaded_events.append(event)

        if config and config.num_recent_events is not None:
            loaded_events.sort(key=lambda e: e.timestamp)

        session.events = loaded_events
        
        return await self._merge_state(app_name, user_id, session)

    async def list_sessions(
        self, *, app_name: str, user_id: Optional[str] = None
    ) -> ListSessionsResponse:
        if user_id is not None:
            sessions_ref = (
                self._client.collection("apps")
                .document(app_name)
                .collection("users")
                .document(user_id)
                .collection("sessions")
            )
            query = sessions_ref.order_by(
                "last_update_time", direction=firestore.Query.ASCENDING
            )
            snapshots = await query.get()
        else:
            query = self._client.collection_group("sessions").where(
                filter=firestore.FieldFilter("app_name", "==", app_name)
            )
            snapshots = await query.get()

        if not snapshots:
            return ListSessionsResponse(sessions=[])

        app_data = await self._fetch_app_state(app_name)
        user_data_cache: dict[str, dict[str, Any]] = {}
        if user_id is not None:
            user_data_cache[user_id] = await self.get_user_state(
                app_name=app_name, user_id=user_id
            )

        sessions_list = []
        for snap in snapshots:
            data = snap.to_dict()
            data["events"] = []  # Don't load events for listing
            session = self._validate_model(Session, data, snap.reference.path)
            if session is not None:
                if session.user_id not in user_data_cache:
                    user_data_cache[session.user_id] = await self.get_user_state(
                        app_name=app_name, user_id=session.user_id
                    )
                self._apply_merged_state(
                    session, app_data, user_data_cache[session.user_id]
                )
                sessions_list.append(session)

        if user_id is None:
            sessions_list.sort(key=lambda s: s.last_update_time)

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

    async def _fetch_app_state(self, app_name: str) -> dict[str, Any]:
        app_ref = self._client.collection("apps").document(app_name)
        app_snap = await app_ref.get()
        if not app_snap.exists:
            return {}
        return app_snap.to_dict() or {}

    @staticmethod
    def _apply_merged_state(
        session: Session,
        app_data: dict[str, Any],
        user_data: dict[str, Any],
    ) -> Session:
        for k, v in app_data.items():
            session.state[State.APP_PREFIX + k] = v
        for k, v in user_data.items():
            session.state[State.USER_PREFIX + k] = v
        return session

    async def _merge_state(self, app_name: str, user_id: str, session: Session) -> Session:
        app_data = await self._fetch_app_state(app_name)
        user_data = await self.get_user_state(app_name=app_name, user_id=user_id)
        return self._apply_merged_state(session, app_data, user_data)

    async def _delete_collection(self, coll_ref, batch_size):
        docs = await coll_ref.limit(batch_size).get()
        deleted = 0

        for doc in docs:
            await doc.reference.delete()
            deleted += 1

        if deleted >= batch_size:
            return await self._delete_collection(coll_ref, batch_size)

    @staticmethod
    def _pop_at_loc(data: Any, loc: tuple[Any, ...]) -> bool:
        if not loc:
            return False
        curr = data
        for part in loc[:-1]:
            if isinstance(curr, dict) and part in curr:
                curr = curr[part]
            elif isinstance(curr, list) and isinstance(part, int) and 0 <= part < len(curr):
                curr = curr[part]
            else:
                return False
        last = loc[-1]
        if isinstance(curr, dict) and last in curr:
            curr.pop(last)
            return True
        return False

    @classmethod
    def _validate_model(
        cls,
        model_cls: type[_ModelT],
        data: dict[str, Any],
        doc_path: str = "<unknown>",
    ) -> Optional[_ModelT]:
        try:
            return model_cls.model_validate(data)
        except ValidationError as exc:
            cleaned = copy.deepcopy(data)
            last_exc: Exception = exc
            for _ in range(5):
                extra_locs = [
                    tuple(err["loc"])
                    for err in last_exc.errors()
                    if err.get("type") == "extra_forbidden" and err.get("loc")
                ]
                if not extra_locs:
                    break
                if not any(cls._pop_at_loc(cleaned, loc) for loc in extra_locs):
                    break
                try:
                    return model_cls.model_validate(cleaned)
                except ValidationError as retry_exc:
                    last_exc = retry_exc
                except Exception as retry_exc:
                    last_exc = retry_exc
                    break
            logger.warning(
                "Dropping invalid %s document at %s: %s",
                model_cls.__name__,
                doc_path,
                last_exc,
            )
            return None
        except Exception as exc:
            logger.warning(
                "Dropping invalid %s document at %s: %s",
                model_cls.__name__,
                doc_path,
                exc,
            )
            return None