import atexit
import os
import ssl
import threading
import time
import datetime
import logging

#from requests import session
from apscheduler.schedulers.background import BackgroundScheduler
from irods.session import iRODSSession
from irods.version import __version__

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

    # Creating an iRODS does not imply a connection is set up.
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

def objfromlist(li, a, v):
    for i in li:
        if getattr(i, a) == v:
            return i
    return None
            
class Session():
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
        _attr = getattr(self.s, attr)
        return _attr


class PoolObject():
    counter = 0
    def __init__(self, obj):
        self.timestamp = time.time()
        self.id = PoolObject.counter
        PoolObject.counter += 1
        self.obj = obj

    def update(self):
        self.timestamp = time.time()

    def __str__(self):
        return f"{self.timestamp=} {self.obj=}"
   
    def reportdata(self):
        return { 'Timestamp': datetime.datetime.fromtimestamp(self.timestamp) }

    def debug(self):
        print('      ================= PoolObject =================')
        print(f'        ID {self.id}, TIME: {self.timestamp}')
        
class SessionPool():
    """Pool of irodsSessions for one user and irods environment
    """
    def __init__(self, envdata, targetsize=0, idle_timeout=300, active_timeout=120):
        self.targetsize = targetsize
        self.idle_timeout = idle_timeout
        self.active_timeout = active_timeout
        self._idle = []
        self._active = []
        self._lock = threading.RLock()
        self.envdata = envdata
    
    def cleanup(self):
        """Remove unused sessions
            Return number of active + idle sessions

            The function uses an progressive timeout model to calculate which sessions to remove:
            max_idle_time = idle_timeout/idle_sessions
            so idle sessions will be removed sooner when there are more
        """
        logging.debug(f"queue lengths: idle {len(self._idle)}, active {len(self._active)}")
        def remove_sessions(queue, removelist):
            for sess in removelist:
                queue.remove(sess)
                sess.obj.cleanup()

        with self._lock:
            # Reduce idle queue size
            removelist = []
            if len(self._idle) > self.targetsize:
                self._idle.sort(key=lambda s: s.timestamp)
                index = len(self._idle)
                for sess in list(self._idle):
                    idle_remove_age = self.idle_timeout // index
                    if time.time() - sess.timestamp > idle_remove_age:
                        removelist.append(sess)
                        index -= 1
                    else:
                        break
                    if index <= self.targetsize:
                        break
                if removelist:
                    logging.debug(f"removing {list(map(str, removelist))} from idle queue")
                    remove_sessions(self._idle, removelist)

            # Remove long-running active sessions
            removelist = []
            for sess in list(self._active):
                if time.time() - sess.timestamp > self.active_timeout:
                    removelist.append(sess)
            if removelist:
                logging.debug(f"removing {list(map(str, removelist))} from active queue")
                remove_sessions(self._active, removelist)

        return len(self._idle) + len(self._active)

    def get(self, user):
        with self._lock:
            if not self._idle:
                poolentry = PoolObject(create_session(self.envdata, user))
            else:
                latest_idx = max(range(len(self._idle)), key=lambda i: self._idle[i].timestamp)
                poolentry = self._idle.pop(latest_idx)
            poolentry.update()
            self._active.append(poolentry)
        return Session(self, poolentry.obj)

    def release(self, sess):
        with self._lock:
            poolentry = objfromlist(self._active, 'obj', sess)
            if poolentry:
                self._active.remove(poolentry)
                poolentry.update()
                self._idle.append(poolentry)
                
    def reportdata(self):
        data = []
        for session in self._idle:
            data.append(session.reportdata() | {'State': 'IDLE'})
        for session in self._active:
            data.append(session.reportdata() | {'State': 'ACTIVE'})
        return data   

    def __del__(self):
        self.close()

    def debug(self):
        print('    ================= SessionPool =================')
        print('      ================== IDLE =====================')
        for sess in self._idle:
            sess.debug()
        print('      ================== ACTIVE ===================')
        for sess in self._active:
            sess.debug()
            
    def close(self):
        with self._lock:
            for sess in self._idle + self._active:
                sess.obj.cleanup()
            self._idle = []
            self._active = []        
                            


class SessionPoolManager():
    """Manage a set of SessionPools,
    one for each logged in user
    """
    def __init__(self, envdata, refresh_time=120):
        self.envdata = envdata
        self._pools = {}
        self._lock = threading.RLock()

    def session(self, user):
        with self._lock:
            if user.username not in self._pools:
                self._pools[user.username] = SessionPool(self.envdata)
        return self._pools[user.username].get(user)

    def cleanup(self):
        """Cleanup unused session pools"""
        empty_pools = []
        with self._lock:
            userlist = self._pools.keys()
            for user in userlist:
                logging.debug(f"Cleaning sessions for user: {user}")
                counter = self._pools[user].cleanup()
                if counter == 0:
                    logging.debug(f"Session pool for {user} is now empty, removing {str(self._pools[user])}")
                    empty_pools.append(user)
            for user in empty_pools:
                del self._pools[user]
                
    def reportdata(self):
        data = []
        for user, pool in self._pools.items():
            for record in pool.reportdata():
                data.append( record | {'User': user})
        return data
    
    def remove(self, user):
        with self._lock:
            if user.username in self._pools:
                del self._pools[user.username]
                
    def debug(self):
        print('  ================= SessionPoolManager =================')
        for user, sp in self._pools.items():
            print(f'    User: {user:13}')
            sp.debug()
            
    def close(self):
        for sp in self._pools.values():
            sp.close()
        self._pools = {}
                

class MultiSessionManager():
    """Manage the SessionManagers for 
    all irods environments
    """
    def __init__(self):
        self._managers = {}
        self._lock = threading.RLock()

    def init_app(self, irods_envs, conn_refresh_time=120, background_cleanup=False):
        executors = {
            'default': {'type': 'threadpool', 'max_workers': 2}
        }
        if background_cleanup:
            self.scheduler = BackgroundScheduler(executors=executors, daemon=True)
            self.scheduler.add_job(
                func=self.cleanup,
                trigger="interval",
                seconds=120,
                jitter=15,
                max_instances=1
            )

            # Prevent APScheduler from submitting jobs during interpreter teardown
            atexit.register(self.scheduler_shutdown)
            logging.debug(f'PID: {os.getpid()}: start scheduler')
            self.scheduler.start()

        with self._lock:
            for envname, envdata in irods_envs.items():
                self._managers[envname] = SessionPoolManager(envdata, refresh_time=conn_refresh_time)
        
    def scheduler_shutdown(self):
        """Gracefully shutdown scheduler when interpreter exits."""
        logging.debug(f'PID: {os.getpid()}: scheduler_shutdown')
        if hasattr(self, 'scheduler') and self.scheduler.running:
            logging.debug('Shutdown scheduler thread')
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                pass
 
    def session(self, user):  #here we used flask.current_user!
        """ Return an irods session object
        for current_user
        """
        with self._lock:
            environment = user.environment if hasattr(user, 'environment') else '__NONE__'
            mgr = self._managers.get(environment)
            if mgr:
                return mgr.session(user)
        return None

    def cleanup(self):
        """Calls cleanup for all managers"""
        with self._lock:
            for env, mgr in self._managers.items():
                mgr.cleanup()

    def reportdata(self):
        data = []
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
                
    def debug(self):
        print('================= MultiSessionManager =================')
        for environ, mgr in self._managers.items():
            print(f'  Environment: {environ:13}')
            mgr.debug()
            
    def close(self):
        logging.debug(f'PID: {os.getpid()}: MultiSessionManager.close')
        self.scheduler_shutdown()
        for _, manager in self._managers.items():
            manager.close()

irods_manager = MultiSessionManager()