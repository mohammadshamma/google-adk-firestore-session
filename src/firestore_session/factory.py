from urllib.parse import urlparse
from .firestore_session_service import FirestoreSessionService

def firestore_session_service_factory(uri: str, **kwargs):
    """
    Factory function to create a FirestoreSessionService from an ADK URI.
    
    Expected URI format: firestore://[project-id]/[database-id]
    
    Examples:
    - firestore://my-project/my-database
    - firestore://default  (uses default project/database from env)
    """
    parsed = urlparse(uri)
    
    # Netloc handles the project ID
    project = parsed.netloc if parsed.netloc and parsed.netloc != "default" else None
    
    # Path handles the database ID (lstrip removes the leading /)
    database = parsed.path.lstrip('/') if parsed.path.lstrip('/') else None
    
    return FirestoreSessionService(project=project, database=database)
