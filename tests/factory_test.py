from unittest.mock import patch
import pytest
from firestore_session import firestore_session_service_factory

@pytest.mark.parametrize("uri, expected_project, expected_database", [
    # Full URI
    ("firestore://my-project/my-database", "my-project", "my-database"),
    # Project only
    ("firestore://my-project", "my-project", None),
    # Project with default netloc
    ("firestore://default/my-database", None, "my-database"),
    # Default URI
    ("firestore://default", None, None),
    # Empty path handling
    ("firestore://my-project/", "my-project", None),
    # Minimal URI
    ("firestore://", None, None),
])
def test_factory_uri_parsing(uri, expected_project, expected_database):
    """Verifies that the factory correctly triggers Firestore client initialization."""
    # Mock the Firestore AsyncClient constructor used inside FirestoreSessionService
    with patch("firestore_session.firestore_session_service.firestore.AsyncClient") as mock_client:
        # Call the factory (which creates the real FirestoreSessionService)
        firestore_session_service_factory(uri)
        
        # Verify the Firestore AsyncClient was initialized with expected arguments
        mock_client.assert_called_once_with(
            project=expected_project,
            database=expected_database
        )
