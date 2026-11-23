import threading
import time
from dataclasses import dataclass, field

from idms.common.irods.session import assert_pool, trim_idle
from idms.common.irods.user import User
from irods.session import iRODSSession
from irods.connection import Connection
import logging

logger = logging.getLogger(__name__)

class _SingletonMeta(type):
    _instances = {}
    _lock = threading.Lock()

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with cls._lock:
                if cls not in cls._instances:
                    cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]

class SessionManager:
    def __init__(self, user: User):
        self._lock = threading.Lock()
        self._session = None
        self._user = user
        self._last_used = time.time()
    
    def session(self) -> iRODSSession:
        with self._lock:
            if self._session is None:
                self._session = self._user.session()
            self._last_used = time.time()
            return self._session

    @property
    def last_used(self):
        ses = self._session
        now = time.time()

        if ses is None or ses.pool is None:
            return self._last_used

        pool = ses.pool
        if not pool._lock.acquire(blocking=False):
            return now
        
        times = [self._last_used]
        try:
            if pool.active:
                times.append(now)
            for conn in pool.idle:
                conn: Connection
                times.append(conn.last_used_time.timestamp())
        finally:
            pool._lock.release()

        return max(times)

    def trim(self):
        """Trim iRODSSession.pool idle connections"""
        with self._lock:
            if self._session is not None:
                trim_idle(self._session)
            
    def cleanup(self):
        with self._lock:
            if self._session is not None:
                self._session.cleanup()
                self._session = None

class UserManager(metaclass=_SingletonMeta):
    def __init__(self, idle_timeout: float = 1 * 3600, trim_period: float = 5 * 60, start_loop = False):
        self.idle_timeout = idle_timeout
        self.trim_period = trim_period
        self._users: dict[tuple, SessionManager] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        if start_loop:
            self.init_loop()

    def user(self, user: User) -> SessionManager:
        key = user._uniq_key()

        with self._lock:
            if key not in self._users:
                self._users[key] = SessionManager(user)
            sm = self._users[key]
            sm._last_used = time.time()
            return  sm

    def clean(self):
        now = time.time()
        stale: list[SessionManager] = []
        with self._lock:
            to_pop = []
            for key in self._users:
                sm = self._users[key]
                if (now - sm.last_used) > (self.idle_timeout):
                    to_pop.append(key)
            for key in to_pop:
                stale.append(self._users.pop(key))

        for s in stale:
            try:
                s.cleanup()
            except Exception:
                logger.exception("iRODS session cleanup failed")

    def trim(self):
        with self._lock:
            sms = [*self._users.values()]

        for sm in sms:
            try:
                sm.trim()
            except Exception:
                logger.exception("iRODS session trimming failed")


    def init_loop(self):
        def loop():
            while not self._stop.wait(self.trim_period):
                try:
                    self.clean()
                except Exception:
                    logger.exception("iRODS session cleanup failed")
                try:
                    self.trim()
                except Exception:
                    logger.exception("iRODS session trim failed")

        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=loop, name="irods-session-clean", daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        with self._lock:
            sms = [*self._users.values()]
            self._users.clear()
        for sm in sms:
            try:
                sm.cleanup()
            except Exception:
                logger.exception("iRODS session cleanup failed")

