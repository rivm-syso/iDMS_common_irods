import pytest
from irods.connection import Connection, NetworkException
from irods.session import iRODSSession

from idms.common.irods.config import iRODSConfig
from idms.common.irods.exception import iRODSSessionNonePool
from idms.common.irods.session import assert_pool, check_auth, get_pam_token, trim_idle


def offline_session() -> iRODSSession:
    # .session doesn't auto connect
    return iRODSConfig(host="localhost", zone="tempZone").session_native("rods", "rods")

def open_idle(ses: iRODSSession, n: int) -> list[Connection]:
    conns = [assert_pool(ses).get_connection() for _ in range(n)]
    for conn in conns:
        conn.release()
    return conns


# live server

@pytest.mark.irods
def test_trim_idle_keeps_newest(native_config: iRODSConfig, native_creds):
    with native_config.session_native(*native_creds) as ses:
        conns = open_idle(ses, 3)
        newest = max(conns, key=lambda c: c.last_used_time)

        assert trim_idle(ses, keep=1) == 2
        assert assert_pool(ses).idle == {newest}
        check_auth(ses)

@pytest.mark.irods
def test_trim_idle_keep_more_than_idle(native_config: iRODSConfig, native_creds):
    with native_config.session_native(*native_creds) as ses:
        open_idle(ses, 2)
        assert trim_idle(ses, keep=5) == 0
        assert len(assert_pool(ses).idle) == 2

@pytest.mark.irods
def test_trim_idle_ignores_network_error(native_config: iRODSConfig, native_creds, monkeypatch):
    with native_config.session_native(*native_creds) as ses:
        conns = open_idle(ses, 2)

        def broken(self):
            raise NetworkException("gone")
        with monkeypatch.context() as m:
            m.setattr(Connection, "disconnect", broken)
            assert trim_idle(ses, keep=1) == 1

        assert len(assert_pool(ses).idle) == 1
        for conn in conns:
            conn.disconnect()


# offline

@pytest.mark.unit
def test_get_pam_token_rejects_native_session():
    with offline_session() as ses, pytest.raises(ValueError):
        get_pam_token(ses)

@pytest.mark.unit
def test_assert_pool():
    with offline_session() as ses:
        assert assert_pool(ses) is ses.pool

@pytest.mark.unit
def test_assert_pool_none():
    with offline_session() as ses:
        ses.pool = None
        with pytest.raises(iRODSSessionNonePool):
            assert_pool(ses)

@pytest.mark.unit
def test_trim_idle_empty_pool():
    with offline_session() as ses:
        assert trim_idle(ses) == 0