class iRODSSessionNonePool(Exception):
    """iRODSSession.pool being None"""

class UserUnsupportedScheme(Exception):
    """user.User doesnt support the auth scheme defined in the iRODSConfig"""