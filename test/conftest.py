import pytest

from idms.common.irods.config import iRODSConfig

SSL = {
    "irods_client_server_negotiation": "request_server_negotiation",
    "irods_client_server_policy": "CS_NEG_REQUIRE",
    "irods_ssl_ca_certificate_file": "/home/pierre/Documents/github_priv/internship-iRODS-Pierre/ssl/irods.crt",
    "irods_encryption_algorithm": "AES-256-CBC",
    "irods_encryption_key_size": 32,
    "irods_encryption_num_hash_rounds": 16,
    "irods_encryption_salt_size": 8,
    "irods_ssl_verify_server": "none",
}

BASE = {
    "host": "res-irods91.researchlumc.nl",
    "zone": "tempZone",
    "port": 1247,
    **SSL
}


@pytest.fixture()
def native_config():
    return iRODSConfig(**BASE, authentication_scheme="native")

@pytest.fixture()
def native_creds():
    return "rods", "rods"

@pytest.fixture()
def pam_config():
    return iRODSConfig(**BASE, authentication_scheme="pam_password")

@pytest.fixture()
def pam_creds():
    return "pamtestuser", "secret123"