import hashlib
from typing import Literal

import pytest
from irods.exception import CAT_INVALID_AUTHENTICATION
from irods.session import iRODSSession

import idms.common.irods.user as user_module
from idms.common.irods.config import iRODSConfig
from idms.common.irods.exception import UserUnsupportedScheme
from idms.common.irods.session import session_get_account
from idms.common.irods.user import User, UserAuthFailure


class ReversingUser(User):
    _encrypt = staticmethod(lambda s: s[::-1])
    _decrypt = staticmethod(lambda s: s[::-1])

class StorePasswordUser(User):
    @staticmethod
    def _login_pam(irods_config: iRODSConfig, username: str, password: str) -> str:
        return password

    def _session_pam(self) -> iRODSSession:
        ses = self.irods_config.session_pam(self.username, pam_password=self._decrypt(self._encrypted_secret))
        return ses


# live server

@pytest.mark.irods
def test_login_native(native_config: iRODSConfig, native_creds):
    user = User.login(native_config, *native_creds)
    user.check_auth()

@pytest.mark.irods
def test_login_pam(pam_config: iRODSConfig, pam_creds):
    user = User.login(pam_config, *pam_creds)
    user.check_auth()

@pytest.mark.irods
def test_check_auth_reuse_session(native_config: iRODSConfig, native_creds):
    user = User.login(native_config, *native_creds)
    with user.check_auth(reuse_ses=True) as ses:
        assert session_get_account(ses).client_user == user.username

@pytest.mark.irods
def test_native_stores_password(native_config: iRODSConfig, native_creds):
    username, password = native_creds
    user = User.login(native_config, username, password)
    assert user._decrypt(user._encrypted_secret) == password

@pytest.mark.irods
def test_pam_stores_token_not_password(pam_config: iRODSConfig, pam_creds):
    username, password = pam_creds
    user = User.login(pam_config, username, password)
    assert user._decrypt(user._encrypted_secret) != password

@pytest.mark.irods
def test_native_wrong_password(native_config: iRODSConfig, native_creds):
    username, _ = native_creds
    with pytest.raises(UserAuthFailure) as e:
        User.login(native_config, username, "wrong-password")
    assert isinstance(e.value.__cause__, CAT_INVALID_AUTHENTICATION)

@pytest.mark.irods
def test_native_wrong_username(native_config: iRODSConfig, native_creds):
    _, password = native_creds
    with pytest.raises(UserAuthFailure) as e:
        User.login(native_config, "fake-user", password)
    assert isinstance(e.value.__cause__, CAT_INVALID_AUTHENTICATION)

@pytest.mark.irods
def test_pam_wrong_password(pam_config: iRODSConfig, pam_creds):
    username, _ = pam_creds
    with pytest.raises(UserAuthFailure):
        User.login(pam_config, username, "wrong-password")

@pytest.mark.irods
def test_pam_bad_token(pam_config: iRODSConfig, pam_creds):
    username, _ = pam_creds
    user = User(pam_config, username, User._encrypt("not-a-token"), check_auth=False)
    with pytest.raises(UserAuthFailure):
        user.check_auth()

@pytest.mark.irods
def test_custom_encryption(native_config: iRODSConfig, native_creds):
    username, password = native_creds
    user = ReversingUser.login(native_config, username, password)
    assert user._encrypted_secret == password[::-1]
    assert user._decrypt(user._encrypted_secret) == password

@pytest.mark.irods
def test_store_password_pam(pam_config: iRODSConfig, pam_creds):
    username, password = pam_creds
    user = StorePasswordUser.login(pam_config, username, password)
    assert user._decrypt(user._encrypted_secret) == password

@pytest.mark.irods
def test_store_password_pam_wrong_password(pam_config: iRODSConfig, pam_creds):
    username, _ = pam_creds
    with pytest.raises(UserAuthFailure):
        StorePasswordUser.login(pam_config, username, "wrong-password")


# offline

def offline_user(scheme: Literal["native", "pam_password"] = "native", host="localhost", username="rods", secret="x") -> User:
    config = iRODSConfig(host=host, zone="tempZone", authentication_scheme=scheme)
    return User(config, username, User._encrypt(secret), check_auth=False)

def unsupported_config() -> iRODSConfig:
    # skip validation
    return iRODSConfig.model_construct(host="localhost", zone="tempZone", authentication_scheme="foobar")

@pytest.mark.unit
@pytest.mark.parametrize("scheme", ["native", "pam_password"])
def test_scheme_properties(scheme):
    user = offline_user(scheme)
    assert user.authentication_scheme == scheme
    assert user.is_pam == (scheme == "pam_password")

@pytest.mark.unit
def test_login_unsupported_scheme():
    with pytest.raises(UserUnsupportedScheme):
        User.login(unsupported_config(), "rods", "rods")

@pytest.mark.unit
def test_session_unsupported_scheme():
    user = User(unsupported_config(), "rods", User._encrypt("x"), check_auth=False)
    with pytest.raises(UserUnsupportedScheme):
        user.session()

@pytest.mark.unit
def test_env_key():
    assert offline_user()._env_key() == offline_user()._env_key()
    assert offline_user()._env_key() != offline_user(host="other-host")._env_key()

@pytest.mark.unit
def test_secret_fingerprint():
    user = offline_user(secret="hunter2")
    assert user._secret_fingerprint() == hashlib.sha256(b"hunter2").hexdigest()

@pytest.mark.unit
def test_fingerprint_ignores_encryption():
    a = offline_user(secret="x")
    b = ReversingUser(a.irods_config, "rods", ReversingUser._encrypt("x"), check_auth=False)
    assert a._secret_fingerprint() == b._secret_fingerprint()

@pytest.mark.unit
def test_hash_same_user():
    assert hash(offline_user()) == hash(offline_user())

@pytest.mark.unit
@pytest.mark.parametrize("change", [{"host": "other-host"}, {"username": "alice"}, {"secret": "y"}, {"scheme": "pam_password"}])
def test_hash_differs(change):
    assert hash(offline_user()) != hash(offline_user(**change))

@pytest.mark.unit
def test_equal_users_dedupe_in_set():
    assert len({offline_user(), offline_user()}) == 1



@pytest.mark.unit
def test_check_auth_non_auth_error_not_wrapped(monkeypatch):
    def broken(ses):
        raise ConnectionError("server unreachable")
    cleaned = []
    monkeypatch.setattr(user_module, "check_auth", broken)
    monkeypatch.setattr(iRODSSession, "cleanup", lambda self: cleaned.append(True))

    with pytest.raises(ConnectionError):
        offline_user().check_auth()
    assert cleaned