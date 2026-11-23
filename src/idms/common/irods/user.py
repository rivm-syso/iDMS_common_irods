import base64
import hashlib
from collections.abc import Callable, Hashable
from typing import Generic, Literal, cast, final, overload

from typing_extensions import Self, TypeVar

from idms.common.irods.config import iRODSConfig
from idms.common.irods.exception import UserUnsupportedScheme
from idms.common.irods.session import check_auth, get_pam_token
from irods.exception import (
    CAT_INVALID_AUTHENTICATION,
    CAT_INVALID_USER,
    CAT_PASSWORD_EXPIRED,
    PAM_AUTH_PASSWORD_FAILED,
)
from irods.session import iRODSSession


class UserAuthFailure(Exception):
    pass


# placeholder de/en-crypt functions are not proper encryption
def encrypt(string: str) -> str:
    return base64.b64encode(string.encode()).decode()

def decrypt(encrypted: str):
    return base64.b64decode(encrypted.encode()).decode()

T = TypeVar("T", default=str)

class User(Generic[T]):
    irods_env: iRODSConfig # zone info / zone ssl config
    username: str

    _encrypted_secret: T

    # so that classes inhereting this one can set up their own encryption if wanted
    _encrypt: Callable[[str], T] = cast("Callable[[str], T]", staticmethod(encrypt))
    _decrypt: Callable[[T], str] = cast("Callable[[T], str]", staticmethod(decrypt))

    pam_ttl: int | None = None

    AUTH_EXCEPTIONS = (CAT_PASSWORD_EXPIRED, CAT_INVALID_AUTHENTICATION, CAT_INVALID_USER, PAM_AUTH_PASSWORD_FAILED)

    def __init__(self, irods_config: iRODSConfig, username: str, encrypted_secret: T, *, check_auth = True):
        self.irods_config = irods_config
        self.username = username
        self._encrypted_secret = encrypted_secret
        if check_auth:
            self.check_auth()

    # overwriting this method in a parent class can add functionality to use 
    # the user's password as secret rather than the native token
    @staticmethod
    def _login_pam(irods_config: iRODSConfig, username: str, password: str) -> str:
        ses = irods_config.session_pam(username, password)
        secret = get_pam_token(ses)
        return secret

    @classmethod
    def login(cls, irods_config: iRODSConfig, username: str, password: str) -> Self:
        try:
            match irods_config.authentication_scheme:
                case "pam_password":
                    secret = cls._login_pam(irods_config, username, password)
                case "native":
                    secret = password
                case _:
                    raise UserUnsupportedScheme


            user = cls(irods_config, username, cls._encrypt(secret))
        except cls.AUTH_EXCEPTIONS as e:
            raise UserAuthFailure(username) from e
        return user


    @overload
    def check_auth(self, reuse_ses: Literal[True]) -> iRODSSession: ...
    @overload
    def check_auth(self, reuse_ses: Literal[False] = False) -> None: ...
    @overload
    def check_auth(self, reuse_ses: bool) -> iRODSSession | None: ...
    def check_auth(self, reuse_ses: bool = False) -> iRODSSession | None:
        ses = self.session()
        try:
            check_auth(ses)
        except self.AUTH_EXCEPTIONS as e:
            ses.cleanup()
            raise UserAuthFailure(self.username) from e
        except:
            ses.cleanup()
            raise
        if reuse_ses:
            return ses
        ses.cleanup()

    # same reasoning as _login_pam
    def _session_pam(self) -> iRODSSession:
        ses = self.irods_config.session_native(self.username, native_password=self._decrypt(self._encrypted_secret))
        return ses

    def session(self) -> iRODSSession:
        match self.irods_config.authentication_scheme:
            case "pam_password":
                ses = self._session_pam()
            case "native":
                ses = self.irods_config.session_native(self.username, native_password=self._decrypt(self._encrypted_secret))
            case _:
                raise UserUnsupportedScheme
        
        return ses

    @property
    def authentication_scheme(self):
        return self.irods_config.authentication_scheme

    @property
    def is_pam(self):
        return self.authentication_scheme == "pam_password"

    def _env_key(self) -> Hashable:
        return tuple(sorted(self.irods_config.as_dict().items()))

    def _secret_fingerprint(self) -> str:
        return hashlib.sha256(self._decrypt(self._encrypted_secret).encode()).hexdigest()

    # always keep fingerprint in the key
    def _uniq_key(self) -> tuple[Hashable, ...]:
        return (self._env_key(), self.username, self._secret_fingerprint())

    # don't overwrite this func, for special functionality edit _uniq_key
    @final
    def __hash__(self):
        return hash(self._uniq_key())

    @final
    def __eq__(self, other):
        return isinstance(other, User) and self._uniq_key() == other._uniq_key()
