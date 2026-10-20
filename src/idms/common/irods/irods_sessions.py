import atexit
import os
import ssl
import threading
import time
import datetime
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from irods.session import iRODSSession
from irods.version import __version__

logger = logging.getLogger(__name__)

def versiontuple(v):
    return tuple(map(int, (v.split("."))))

irodsclient_before_1_1_4 = versiontuple(__version__) < versiontuple('1.1.4')

def create_session(envdata, user, use_pam=False):
    """Create an iRODS session for <user>

    Args:
        envdata (dict): dict from IRODS_ENVS in config.py
        user (WebUser): user to create a session for
        use_pam (bool, optional): Indicate wether to do a native or PAM login. Defaults to False.

    Returns:
        iRODSSession: iRODSSession object

    If use_pam is True, will login user with <user.username, user.password>
    If false, will use <user.username, user.native_password>
    """
    context = ssl._create_unverified_context(
        purpose=ssl.Purpose.SERVER_AUTH,
        cafile=None,
        capath=None,
        cadata=None
    )

    ssl_settings = {
        'irods_client_server_negotiation': 'request_server_negotiation',
        'irods_client_server_policy': 'CS_NEG_REQUIRE',
        'irods_ssl_ca_certificate_file': envdata.get('certfile', 'dummy'),
        'irods_encryption_algorithm': 'AES-256-CBC',
        'irods_encryption_key_size': 32,
        'irods_encryption_num_hash_rounds': 16,
        'irods_encryption_salt_size': 8,
        'ssl_context': context
    }

    # Creating an iRODS session does not imply a connection is set up.
    if use_pam:
        session_password = user.password
        authentication_scheme = 'pam_password'
    else:
        session_password = user.native_password
        authentication_scheme = 'native'
    return iRODSSession(
        host=envdata.get('host'),
        port=1247,
        user=user.username,
        password=session_password,
        zone=envdata.get('zone'),
        authentication_scheme=authentication_scheme,
        refresh_time=300,
        **ssl_settings)

class Session:
    def __init__(self, pool, irods_session):
        self._pool = pool
        self.s = irods_session

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()

    def release(self):
        if self.s:
            self._pool.release(self.s)

    def __getattr__(self, attr):
        return getattr(self.s, attr)


class PoolObject:
    counter = 0
    def __init__(self, obj, active_timeout=3600, idle_timeout=1):
        self.idle_timeout = idle_timeout
        self.id = PoolObject.counter
        PoolObject.counter += 1
        self.obj = obj
        self.update(active_timeout=active_timeout)

    def update(self, active_timeout=None):
        self.timestamp = time.time()
        if active_timeout is not None:
            self.active_timeout = active_timeout

    @property
    def active_timeout_timestamp(self):
        return self.timestamp + self.active_timeout

    @property
    def idle_timeout_timestamp(self):
        return self.timestamp + self.idle_timeout

    def __str__(self):
        return f"PoolObject(id={self.id}, time={self.timestamp})"

    def reportdata(self):
        return { 
            'Timestamp': datetime.datetime.fromtimestamp(self.timestamp),
            'Active_timeout': datetime.datetime.fromtimestamp(self.active_timeout_timestamp),
            'Idle_timeout': datetime.datetime.fromtimestamp(self.idle_timeout_timestamp)
            }


class SessionPool:
    """Pool of irodsSessions for one user and irods environment
    """
    def __init__(self, envdata, targetsize=0, default_active_timeout=3600, idle_timeout=1):
        self.targetsize = targetsize
        self._idle = []
        self._active_map = {}
        self._lock = threading.RLock()
        self.envdata = envdata
        self.default_active_timeout = default_active_timeout
        self.idle_timeout = idle_timeout
        
        self.stat_created = 0
        self.stat_reused = 0
        self.stat_released = 0
        self.stat_idle_destroyed = 0
        self.stat_active_destroyed = 0

    def cleanup(self):
        """Remove unused sessions
            Return number of active + idle sessions

            The function uses an progressive timeout model to calculate which sessions to remove:
            max_idle_time = idle_timeout/idle_sessions
            so idle sessions will be removed sooner when there are more
        """
        logger.debug(f"queue lengths: idle {len(self._idle)}, active {len(self._active_map)}")
        logger.debug(f"SessionPool stats: created: {self.stat_created}, reused: {self.stat_reused}, released: {self.stat_released}, idle destroyed: {self.stat_idle_destroyed}, active destroyed: {self.stat_active_destroyed}")

        to_destroy = []

        with self._lock:
            now = time.time()
            # Reduce idle queue size
            if len(self._idle) > self.targetsize:
                remain_idle = []
                for sess in self._idle:
                    if (now > sess.idle_timeout_timestamp) and (len(self._idle) - len(to_destroy) > self.targetsize):
                        to_destroy.append(sess)
                        self.stat_idle_destroyed += 1
                    else:
                        remain_idle.append(sess)
                self._idle = remain_idle

            # Remove long-running active sessions
            expired_active_keys = []
            for sess_id, sess in self._active_map.items():
                if now > sess.active_timeout_timestamp:
                    to_destroy.append(sess)
                    expired_active_keys.append(sess_id)
                    self.stat_active_destroyed += 1

            for sess_id in expired_active_keys:
                del self._active_map[sess_id]

            total_remaining = len(self._idle) + len(self._active_map)

        for sess in to_destroy:
            try:
                sess.obj.cleanup()
            except Exception as e:
                logger.warning(f"Error cleaning up iRODS session: {e}")

        return total_remaining

    def get(self, user, active_timeout=None):
        effective_active_timeout = active_timeout if active_timeout is not None else self.default_active_timeout
        with self._lock:
            if self._idle:
                latest_idx = max(
                    range(len(self._idle)), key=lambda i: self._idle[i].timestamp
                )
                poolentry = self._idle.pop(latest_idx)
                self.stat_reused += 1
                poolentry.update(active_timeout=effective_active_timeout)
                self._active_map[id(poolentry.obj)] = poolentry
                return Session(self, poolentry.obj)
            
        raw_session = create_session(self.envdata, user)
        poolentry = PoolObject(
            raw_session,
            active_timeout=effective_active_timeout,
            idle_timeout=self.idle_timeout,
        )

        with self._lock:
            self.stat_created += 1
            self._active_map[id(poolentry.obj)] = poolentry

        return Session(self, poolentry.obj)

    def release(self, sess):
        sess_key = id(sess)
        with self._lock:
            poolentry = self._active_map.pop(sess_key, None)
            if poolentry:
                poolentry.update()
                self._idle.append(poolentry)
                self.stat_released += 1

    def reportdata(self):
        data = []
        with self._lock:
            for session in self._idle:
                data.append(session.reportdata() | {'State': 'IDLE'})
            for session in self._active_map.values():
                data.append(session.reportdata() | {'State': 'ACTIVE'})
        return data

    def __del__(self):
        self.close()

    def close(self):
        with self._lock:
            for sess in self._idle + list(self._active_map.values()):
                sess.obj.cleanup()
            self._idle = []
            self._active_map.clear()

class SessionPoolManager:
    """Manage a set of SessionPools,
    one for each logged in user
    """
    def __init__(self, envdata, refresh_time=120, default_active_timeout=3600, idle_timeout=1):
        self.envdata = envdata
        self._pools = {}
        self._lock = threading.RLock()
        self.default_active_timeout = default_active_timeout
        self.idle_timeout = idle_timeout

    def session(self, user, active_timeout=None):
        with self._lock:
            if user.username not in self._pools:
                self._pools[user.username] = SessionPool(self.envdata, default_active_timeout=self.default_active_timeout, idle_timeout=self.idle_timeout)
        effective_active_timeout = active_timeout if active_timeout is not None else self.default_active_timeout
        return self._pools[user.username].get(user, active_timeout=effective_active_timeout)

    def cleanup(self):
        """Cleanup unused session pools"""
        empty_pools = []
        with self._lock:
            userlist = self._pools.keys()
            for user in userlist:
                logger.debug(f"Cleaning sessions for user: {user}")
                counter = self._pools[user].cleanup()
                if counter == 0:
                    logger.debug(f"Session pool for {user} is now empty, removing {str(self._pools[user])}")
                    empty_pools.append(user)
            for user in empty_pools:
                del self._pools[user]

    def reportdata(self):
        data = []
        with self._lock:
            for user, pool in self._pools.items():
                for record in pool.reportdata():
                    data.append( record | {'User': user})
        return data

    def remove(self, user):
        with self._lock:
            if user.username in self._pools:
                del self._pools[user.username]

    def close(self):
        for sp in self._pools.values():
            sp.close()
        self._pools = {}


class MultiSessionManager:
    """Manage the SessionManagers for
    all irods environments
    """
    def __init__(self, default_active_timeout=1, idle_timeout=1):
        self._managers = {}
        self._lock = threading.RLock()
        self.default_active_timeout = default_active_timeout
        self.idle_timeout = idle_timeout

    def init_app(self, irods_envs, conn_refresh_time=120, background_cleanup=False):
        executors = {
            'default': {'type': 'threadpool', 'max_workers': 2}
        }
        if background_cleanup:
            self.scheduler = BackgroundScheduler(executors=executors, daemon=True)
            self.scheduler.add_job(
                func=self.cleanup,
                trigger="interval",
                seconds=60,
                jitter=5,
                max_instances=1
            )

            # Prevent APScheduler from submitting jobs during interpreter teardown
            atexit.register(self.scheduler_shutdown)
            logger.debug(f'PID: {os.getpid()}: start scheduler')
            self.scheduler.start()

        with self._lock:
            for envname, envdata in irods_envs.items():
                self._managers[envname] = SessionPoolManager(envdata, refresh_time=conn_refresh_time, default_active_timeout=self.default_active_timeout, idle_timeout=self.idle_timeout)

    def scheduler_shutdown(self):
        """Gracefully shutdown scheduler when interpreter exits."""
        logger.debug(f'PID: {os.getpid()}: scheduler_shutdown')
        if hasattr(self, 'scheduler') and self.scheduler.running:
            logger.debug('Shutdown scheduler thread')
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                pass

    def session(self, user, active_timeout=None):  #here we used flask.current_user!
        """ Return an irods session object
        for current_user
        """
        with self._lock:
            environment = user.environment if hasattr(user, 'environment') else '__NONE__'
            mgr = self._managers.get(environment)
            if mgr:
                effective_active_timeout = active_timeout if active_timeout is not None else self.default_active_timeout
                return mgr.session(user, active_timeout=effective_active_timeout)
        return None

    def cleanup(self):
        """Calls cleanup for all managers"""
        with self._lock:
            for env, mgr in self._managers.items():
                mgr.cleanup()

    def reportdata(self):
        data = []
        with self._lock:
            for env, manager in self._managers.items():
                for record in manager.reportdata():
                    data.append(record | { 'Environment': env } )
        return data

    def remove(self, user):
        """Removes the session for user"""
        ...
        with self._lock:
            if (manager := self._managers.get(user.environment)):
                manager.remove(user)

    def close(self):
        logger.debug(f'PID: {os.getpid()}: MultiSessionManager.close')
        self.scheduler_shutdown()
        for _, manager in self._managers.items():
            manager.close()

irods_manager = MultiSessionManager(default_active_timeout=600, idle_timeout=300)