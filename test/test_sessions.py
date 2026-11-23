import threading
import time

import pytest
from irods.session import iRODSSession

from idms.common.irods.config import iRODSConfig
from idms.common.irods.session import assert_pool, check_auth
from idms.common.irods.sessions import SessionManager, UserManager, _SingletonMeta
from idms.common.irods.user import User


def offline_user(username="rods", secret="x") -> User:
    # autoconnect off
    config = iRODSConfig(host="localhost", zone="tempZone")
    return User(config, username, User._encrypt(secret), check_auth=False)

def open_idle(ses: iRODSSession, n: int):
    conns = [assert_pool(ses).get_connection() for _ in range(n)]
    for conn in conns:
        conn.release()
    return conns

def make_stale(sm: SessionManager):
    sm._last_used = time.time() - 1000

def hold_lock_in_thread(lock):
    acquired, release = threading.Event(), threading.Event()
    def run():
        with lock:
            acquired.set()
            release.wait()
    thread = threading.Thread(target=run)
    thread.start()
    acquired.wait()
    return release, thread

def wait_until(condition, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False

@pytest.fixture(autouse=True)
def fresh_user_manager():
    # UserManager is a singleton, reset it so tests don't share state
    _SingletonMeta._instances.pop(UserManager, None)
    yield
    um = _SingletonMeta._instances.pop(UserManager, None)
    if um is not None:
        um.close()


# live

@pytest.mark.irods
def test_session_manager_live(native_config: iRODSConfig, native_creds):
    user = User.login(native_config, *native_creds)
    sm = UserManager().user(user)

    ses = sm.session()
    check_auth(ses)
    assert sm.session() is ses

    sm.cleanup()
    assert sm._session is None

@pytest.mark.irods
def test_last_used_active_connection(native_config: iRODSConfig, native_creds):
    sm = SessionManager(User.login(native_config, *native_creds))
    ses = sm.session()
    sm._last_used = 0

    conn = assert_pool(ses).get_connection()
    try:
        assert sm.last_used >= time.time() - 1
    finally:
        conn.release()
        sm.cleanup()

@pytest.mark.irods
def test_last_used_idle_connection(native_config: iRODSConfig, native_creds):
    sm = SessionManager(User.login(native_config, *native_creds))
    ses = sm.session()
    sm._last_used = 0

    [conn] = open_idle(ses, 1)
    assert sm.last_used == conn.last_used_time.timestamp()
    sm.cleanup()

@pytest.mark.irods
def test_session_manager_trim_live(native_config: iRODSConfig, native_creds):
    sm = SessionManager(User.login(native_config, *native_creds))
    ses = sm.session()
    open_idle(ses, 3)

    sm.trim()
    assert len(assert_pool(ses).idle) == 1
    sm.cleanup()

@pytest.mark.irods
def test_user_manager_trim_live(native_config: iRODSConfig, native_creds):
    um = UserManager()
    ses = um.user(User.login(native_config, *native_creds)).session()
    open_idle(ses, 3)

    um.trim()
    assert len(assert_pool(ses).idle) == 1


# SessionManager offline

@pytest.mark.unit
def test_session_is_lazy_and_reused():
    sm = SessionManager(offline_user())
    assert sm._session is None

    ses = sm.session()
    assert isinstance(ses, iRODSSession)
    assert sm.session() is ses

@pytest.mark.unit
def test_last_used_without_session():
    sm = SessionManager(offline_user())
    sm._last_used = 123
    assert sm.last_used == 123

@pytest.mark.unit
def test_last_used_pool_none():
    sm = SessionManager(offline_user())
    sm.session().pool = None
    sm._last_used = 123
    assert sm.last_used == 123

@pytest.mark.unit
def test_last_used_empty_pool():
    sm = SessionManager(offline_user())
    sm.session()
    sm._last_used = 123
    assert sm.last_used == 123

@pytest.mark.unit
def test_last_used_pool_locked_means_in_use():
    sm = SessionManager(offline_user())
    ses = sm.session()
    sm._last_used = 0

    release, thread = hold_lock_in_thread(assert_pool(ses)._lock)
    try:
        assert sm.last_used >= time.time() - 1
    finally:
        release.set()
        thread.join()

@pytest.mark.unit
def test_trim_without_session():
    SessionManager(offline_user()).trim()

@pytest.mark.unit
def test_trim_empty_pool():
    sm = SessionManager(offline_user())
    sm.session()
    sm.trim()

@pytest.mark.unit
def test_cleanup():
    sm = SessionManager(offline_user())
    ses = sm.session()

    sm.cleanup()
    assert sm._session is None

    sm.cleanup()
    assert sm.session() is not ses


# UserManager offline

@pytest.mark.unit
def test_user_manager_is_singleton():
    a = UserManager(idle_timeout=5)
    b = UserManager(idle_timeout=10)
    assert a is b
    assert b.idle_timeout == 5

@pytest.mark.unit
def test_user_same_key_same_manager():
    um = UserManager()
    assert um.user(offline_user()) is um.user(offline_user())
    assert um.user(offline_user()) is not um.user(offline_user(secret="y"))
    assert um.user(offline_user()) is not um.user(offline_user(username="alice"))

@pytest.mark.unit
def test_user_refreshes_last_used():
    um = UserManager()
    sm = um.user(offline_user())
    make_stale(sm)

    um.user(offline_user())
    assert sm._last_used >= time.time() - 1

@pytest.mark.unit
def test_clean_removes_only_stale():
    um = UserManager(idle_timeout=10)
    stale = um.user(offline_user("stale"))
    fresh = um.user(offline_user("fresh"))
    stale.session()
    make_stale(stale)

    um.clean()
    assert list(um._users.values()) == [fresh]
    assert stale._session is None

@pytest.mark.unit
def test_clean_logs_cleanup_failure(caplog, monkeypatch):
    um = UserManager(idle_timeout=10)
    sm = um.user(offline_user())
    make_stale(sm)
    def broken():
        raise RuntimeError("cleanup broke")
    monkeypatch.setattr(sm, "cleanup", broken)

    um.clean()
    assert um._users == {}
    assert "cleanup failed" in caplog.text

@pytest.mark.unit
def test_trim_users_without_sessions():
    um = UserManager()
    um.user(offline_user("a"))
    um.user(offline_user("b"))
    um.trim()

@pytest.mark.unit
def test_trim_logs_failure(caplog, monkeypatch):
    um = UserManager()
    sm = um.user(offline_user())
    def broken():
        raise RuntimeError("trim broke")
    monkeypatch.setattr(sm, "trim", broken)

    um.trim()
    assert "trimming failed" in caplog.text

@pytest.mark.unit
def test_close():
    um = UserManager()
    sm = um.user(offline_user())
    sm.session()

    um.close()
    assert um._users == {}
    assert sm._session is None
    assert um._stop.is_set()

@pytest.mark.unit
def test_close_logs_cleanup_failure(caplog, monkeypatch):
    um = UserManager()
    sm = um.user(offline_user())
    def broken():
        raise RuntimeError("cleanup broke")
    monkeypatch.setattr(sm, "cleanup", broken)

    um.close()
    assert "cleanup failed" in caplog.text

@pytest.mark.unit
def test_loop_cleans_stale_users():
    um = UserManager(idle_timeout=10, trim_period=0.01, start_loop=True)
    sm = um.user(offline_user())
    sm.session()
    make_stale(sm)

    assert wait_until(lambda: um._users == {})
    assert sm._session is None

    thread = um._thread
    assert thread is not None
    um.close()
    thread.join(timeout=1)
    assert not thread.is_alive()

@pytest.mark.unit
def test_loop_started_once():
    um = UserManager(trim_period=10, start_loop=True)
    thread = um._thread
    um.init_loop()
    assert um._thread is thread

@pytest.mark.unit
def test_loop_logs_failures(caplog, monkeypatch):
    um = UserManager(trim_period=0.01)
    def broken():
        raise RuntimeError("broke")
    monkeypatch.setattr(um, "clean", broken)
    monkeypatch.setattr(um, "trim", broken)

    um.init_loop()
    assert wait_until(lambda: "cleanup failed" in caplog.text and "trim failed" in caplog.text)