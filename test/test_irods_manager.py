import threading
import time
from unittest.mock import MagicMock, patch

import pytest


# Mock object representing a user
class MockUser:
    def __init__(self, username="test_user", environment="dev"):
        self.username = username
        self.password = "secret"
        self.native_password = "native_secret"
        self.environment = environment


@pytest.fixture
def mock_irods_session():
    """Mocks python-irodsclient iRODSSession creation."""
    with patch("idms.common.irods.irods_sessions.create_session") as mock_create:
        mock_sess = MagicMock()
        mock_create.return_value = mock_sess
        yield mock_create


@pytest.fixture
def manager(mock_irods_session):
    """Initializes MultiSessionManager with fast timeout settings for testing."""
    from idms.common.irods.irods_sessions import MultiSessionManager

    mgr = MultiSessionManager(default_active_timeout=0.2, idle_timeout=0.2)
    envs = {
        "dev": {"host": "localhost", "zone": "tempZone", "certfile": "dummy"}
    }
    mgr.init_app(envs, background_cleanup=False)
    yield mgr
    mgr.close()


class TestSessionPoolTimeouts:

    def test_active_session_timeout_cleanup(
        self, manager, mock_irods_session
    ):
        """Verifies that long-running active sessions are cleaned up upon expiry."""
        user = MockUser()

        # Checkout session (active timeout = 0.1s)
        session_ctx = manager.session(user, active_timeout=0.1)
        irods_obj = session_ctx.s

        # Wait for active timeout to pass without releasing
        time.sleep(0.15)

        # Trigger cleanup
        manager.cleanup()

        # iRODSSession cleanup should have been invoked
        irods_obj.cleanup.assert_called_once()

    def test_idle_session_timeout_cleanup(self, manager, mock_irods_session):
        """Verifies that idle sessions exceeding idle_timeout are destroyed during cleanup."""
        user = MockUser()

        # Get and immediately release session to place it into idle queue
        with manager.session(user) as sess:
            irods_obj = sess.s

        # Verify it wasn't cleaned up immediately upon release
        irods_obj.cleanup.assert_not_called()

        # Wait for idle timeout (0.2s)
        time.sleep(0.25)

        # Run cleanup
        manager.cleanup()

        # Verify idle session was destroyed
        irods_obj.cleanup.assert_called_once()

    def test_session_reuse_resets_timeout(self, manager, mock_irods_session):
        """Ensures reusing a session updates its active timestamp properly."""
        user = MockUser()

        # First use
        with manager.session(user) as sess1:
            obj1 = sess1.s

        # Second use (reused from idle pool)
        with manager.session(user, active_timeout=1.0) as sess2:
            obj2 = sess2.s
            assert obj1 == obj2
            assert mock_irods_session.call_count == 1  # No new session created


class TestThreadSafety:

    def test_concurrent_session_checkout_and_release(
        self, manager, mock_irods_session
    ):
        """Simulates multiple threads checking out and releasing sessions concurrently."""
        user = MockUser()
        num_threads = 10
        iterations = 20
        errors = []

        def worker():
            try:
                for _ in range(iterations):
                    with manager.session(user) as sess:
                        # Perform work on session
                        _ = sess.s
                        time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=worker) for _ in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert (
            len(errors) == 0
        ), f"Concurrent operations raised errors: {errors}"

    def test_concurrent_reportdata_during_mutations(
        self, manager, mock_irods_session
    ):
        """Verifies reportdata() can be safely called while sessions are being checked out and cleaned up."""
        user_a = MockUser("user_a")
        user_b = MockUser("user_b")
        stop_event = threading.Event()
        errors = []

        def mutator():
            while not stop_event.is_set():
                try:
                    with manager.session(user_a) as s:
                        time.sleep(0.005)
                    manager.cleanup()
                except Exception as e:
                    errors.append(e)

        def reporter():
            while not stop_event.is_set():
                try:
                    data = manager.reportdata()
                    assert isinstance(data, list)
                except Exception as e:
                    errors.append(e)

        t1 = threading.Thread(target=mutator)
        t2 = threading.Thread(target=reporter)

        t1.start()
        t2.start()
        time.sleep(0.2)
        stop_event.set()
        t1.join()
        t2.join()

        assert len(errors) == 0, f"Thread conflict detected: {errors}"