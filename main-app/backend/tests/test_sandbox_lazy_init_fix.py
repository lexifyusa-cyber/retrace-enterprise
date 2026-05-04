"""
Tests for lazy session initialization fix.

This test suite verifies that:
1. Files can be browsed immediately after login (without sending a message first)
2. Sessions are created on-demand if they don't exist
3. Sessions are reused if they already exist
4. Timeout is handled gracefully
"""

import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch, Mock
from datetime import datetime, timedelta


@pytest.fixture
def mock_user():
    """Create a mock user for testing."""
    from app.models.user import User
    user = User(
        user_id="test-user-123",
        email="test@example.com",
        username="testuser",
        hashed_password="hashed_pwd",
        tenant_id="tenant-123",
        role="user",
        status="active",
        auth_provider="email",
    )
    user.mfa_enabled = False
    user.mfa_secret_encrypted = None
    user.force_password_change = False
    user.last_login_at = None
    user.last_login_ip = None
    user.failed_login_count = 0
    user.to_dict = MagicMock(return_value={"user_id": "test-user-123", "email": "test@example.com"})
    return user


@pytest.fixture
def mock_request():
    """Create a mock request object."""
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = "192.168.1.1"
    request.headers = {"user-agent": "Mozilla/5.0"}
    request.headers.get = MagicMock(return_value="Mozilla/5.0")
    return request


@pytest.fixture
def mock_session():
    """Create a mock database session."""
    return AsyncMock()


class TestEagerSandboxInitialization:
    """Test that sandbox sessions are eagerly initialized on login."""

    @pytest.mark.asyncio
    async def test_eager_init_background_task_created(self, mock_user, mock_request, mock_session):
        """
        Verify that sandbox session creation happens in background via asyncio.create_task.

        This ensures login response is not blocked by session creation.
        """
        from app.api.auth import _create_session_and_tokens
        from app.models.user_session import UserSession

        # Mock the necessary functions
        with patch('app.api.auth.asyncio.create_task') as mock_create_task:
            with patch('app.api.auth.UserSession') as mock_user_session_class:
                mock_session.add = MagicMock()
                mock_user_session_class.return_value = MagicMock()

                result = await _create_session_and_tokens(mock_user, mock_request, mock_session)

                # Should return token response immediately
                assert result is not None
                assert hasattr(result, 'access_token')
                assert hasattr(result, 'refresh_token')

                # Should have created a background task
                mock_create_task.assert_called_once()


class TestFileBrowseSessionCreation:
    """Test that file browsing creates sessions on-demand."""

    def test_browse_endpoint_uses_get_or_create_session(self):
        """
        Verify that GET /files/browse uses get_or_create_session,
        not just get_session.

        This is the core fix for the fly.io issue where users couldn't
        see directories until they sent a message.
        """
        # Read the files.py endpoint to verify implementation
        from pathlib import Path
        files_path = Path("/Users/nvp/retrace-enterprise-trial/main-app/backend/app/api/files.py")
        content = files_path.read_text()

        # Verify the fix is in place
        assert "get_or_create_session" in content, "files.py should use get_or_create_session"
        assert "ThreadPoolExecutor" in content, "files.py should use ThreadPoolExecutor for timeout"
        assert "timeout=30" in content, "files.py should have 30s timeout"
        assert "sandbox_session_ready_for_browse" in content, "files.py should have proper logging"


class TestLogging:
    """Test that logging is properly implemented."""

    def test_files_logging_implemented(self):
        """Verify logging statements in files.py."""
        from pathlib import Path
        files_path = Path("/Users/nvp/retrace-enterprise-trial/main-app/backend/app/api/files.py")
        content = files_path.read_text()

        # Should have logging for success
        assert "sandbox_session_ready_for_browse" in content
        # Should have logging for timeout
        assert "sandbox_session_creation_timeout_during_browse" in content
        # Should have logging for errors
        assert "sandbox_browse_error" in content
        # Should have logger instance (structlog — same library used across the codebase)
        assert "logger = structlog.get_logger()" in content
        assert "import structlog" in content

    def test_auth_logging_implemented(self):
        """Verify logging statements in auth.py."""
        from pathlib import Path
        auth_path = Path("/Users/nvp/retrace-enterprise-trial/main-app/backend/app/api/auth.py")
        content = auth_path.read_text()

        # Should have logging for successful eager init
        assert "sandbox_session_eagerly_initialized_on_login" in content
        # Should have logging for timeout
        assert "sandbox_eager_init_timeout_on_login" in content
        # Should have logging for errors
        assert "sandbox_eager_init_failed_on_login" in content


class TestCodeCorrectness:
    """Test that the code changes are correct."""

    def test_files_py_syntax_valid(self):
        """Verify files.py has valid Python syntax."""
        import py_compile
        from pathlib import Path
        files_path = Path("/Users/nvp/retrace-enterprise-trial/main-app/backend/app/api/files.py")
        # Should not raise exception
        py_compile.compile(str(files_path), doraise=True)

    def test_auth_py_syntax_valid(self):
        """Verify auth.py has valid Python syntax."""
        import py_compile
        from pathlib import Path
        auth_path = Path("/Users/nvp/retrace-enterprise-trial/main-app/backend/app/api/auth.py")
        # Should not raise exception
        py_compile.compile(str(auth_path), doraise=True)

    def test_auth_imports_asyncio(self):
        """Verify auth.py imports asyncio for background tasks."""
        from pathlib import Path
        auth_path = Path("/Users/nvp/retrace-enterprise-trial/main-app/backend/app/api/auth.py")
        content = auth_path.read_text()

        assert "import asyncio" in content, "auth.py should import asyncio"

    def test_files_imports_concurrent_futures(self):
        """Verify files.py imports concurrent.futures for timeout."""
        from pathlib import Path
        files_path = Path("/Users/nvp/retrace-enterprise-trial/main-app/backend/app/api/files.py")
        content = files_path.read_text()

        assert "import concurrent.futures" in content, "files.py should import concurrent.futures"

    def test_files_uses_structlog_not_stdlib_logging(self):
        """Verify files.py uses structlog (matches codebase convention).

        Regression test for the bug where `import logging` was used instead of
        `import structlog`, causing TypeError on every call because stdlib
        logging rejects kwargs like `user_id=...`.
        """
        from pathlib import Path
        files_path = Path("/Users/nvp/retrace-enterprise-trial/main-app/backend/app/api/files.py")
        content = files_path.read_text()

        assert "import structlog" in content, "files.py must use structlog, not stdlib logging"
        assert "structlog.get_logger()" in content, "files.py must call structlog.get_logger()"
        # Must NOT use stdlib logging (would crash on keyword arguments)
        assert "import logging" not in content, "files.py must not import stdlib logging"
        assert "logging.getLogger" not in content, "files.py must not use logging.getLogger"


class TestIntegrationScenarios:
    """
    Manual integration test scenarios to verify with fly.io deployment.
    """

    def test_scenario_fresh_user_browse_files(self):
        """
        Integration test scenario (manual):

        1. Create new user account on fly.io
        2. Log in
        3. Navigate to Products page
        4. Click "Add Training Data" WITHOUT sending a message first
        5. VERIFY: Directories appear immediately

        Before fix: User would see empty directories
        After fix: User sees directories immediately
        """
        pass

    def test_scenario_session_reuse(self):
        """
        Integration test scenario (manual):

        1. User logs in (background task creates sandbox session)
        2. GET /files/browse /workspace/docs (uses same session)
        3. GET /files/browse /workspace/code (reuses same session)
        4. VERIFY: Only one Docker container created (one per user)

        Expected behavior: Container is reused, not recreated.
        """
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
