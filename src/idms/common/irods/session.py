"""Helper functions that run on `iRODSession`s"""
from copy import deepcopy
from typing import cast

import irods.auth
from irods.session import Pool, iRODSAccount, iRODSSession

from idms.common.irods.exception import iRODSSessionNonePool
from irods.connection import Connection, NetworkException


def session_get_account(ses: iRODSSession) -> iRODSAccount:
    return cast(iRODSAccount, getattr(ses, "_iRODSSession__configured"))

def get_pam_token(ses: iRODSSession, ttl_hours: int = 24*14) -> str:
    # token in this case means a temporary native password that can be used in place of the user's pam password

    a_ttl = str(ttl_hours)
    
    ses = ses.clone()
    # .clone() is only shallow
    ses.auth_options_by_scheme = deepcopy(ses.auth_options_by_scheme)

    if session_get_account(ses).authentication_scheme != "pam_password":
        raise ValueError("This is not a pam session")
    
    native_password_list: list[str] = []
    ses.set_auth_option_for_scheme("pam_password", irods.auth.CLIENT_GET_REQUEST_RESULT, native_password_list)
    ses.set_auth_option_for_scheme("pam_password", "a_ttl", a_ttl)
    assert ses.pool is not None # gets set in iRODSSession.__init__, should never actually be None
    try:
        ses.pool.get_connection()
    finally:
        ses.cleanup()
    return native_password_list[0]

def assert_pool(ses: iRODSSession) -> Pool:
    if ses.pool is None:
        raise iRODSSessionNonePool
    return ses.pool

def check_auth(ses: iRODSSession):
    with assert_pool(ses).get_connection():
        pass

def trim_idle(ses: iRODSSession, keep = 1):
    pool = assert_pool(ses)
    with pool._lock:
        p_idle: set[Connection] = pool.idle
        idle = sorted(p_idle, key=lambda c: c.last_used_time, reverse=True)
        trim = idle[keep:]
        pool.idle.difference_update(trim)
    
    for conn in trim:
        try:
            conn.disconnect()
        except NetworkException:
            pass
    return len(trim)
