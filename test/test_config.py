import pytest
from irods.session import iRODSSession
from pydantic import ValidationError

from idms.common.irods.config import iRODSConfig
from idms.common.irods.session import get_pam_token, session_get_account


def user_check(ses: iRODSSession):
    account = session_get_account(ses)
    ses.collections.get(f"/{account.client_zone}/home/{account.client_user}")


# --- live server ---

@pytest.mark.irods
def test_session_native(native_config: iRODSConfig, native_creds):
    with native_config.session_native(*native_creds) as ses:
        user_check(ses)

@pytest.mark.irods
def test_session_pam(pam_config: iRODSConfig, pam_creds):
    with pam_config.session_pam(*pam_creds) as ses:
        user_check(ses)

@pytest.mark.irods
def test_pam_token_as_native_password(pam_config: iRODSConfig, pam_creds):
    username, password = pam_creds
    with pam_config.session_pam(username, password) as ses:
        token = get_pam_token(ses)

    with pam_config.session_native(username, token) as ses:
        user_check(ses)


# no server needed

def offline_config(scheme) -> iRODSConfig:
    return iRODSConfig(host="localhost", zone="tempZone", authentication_scheme=scheme)

@pytest.mark.unit
def test_config_rejects_irods_scheme():
    with pytest.raises(ValidationError):
        offline_config("irods")

@pytest.mark.unit
def test_session_pam_rejects_native_scheme():
    with pytest.raises(ValueError):
        offline_config("native").session_pam("rods", "rods")
