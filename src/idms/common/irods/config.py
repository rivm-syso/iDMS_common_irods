from copy import deepcopy
from pathlib import Path
from typing import Literal, TypedDict, cast

from pydantic import BaseModel, ConfigDict

import irods.auth
from idms.common.irods.session import session_get_account
from irods.session import iRODSAccount, iRODSSession


# if any field is missing, it can be added here with the same naming irods python client expects
# only fields that pass directly into iRODSSession(*) should be in this model
class iRODSConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    zone: str
    port: int = 1247

    authentication_scheme: Literal["native", "pam_password"] = "native"
    irods_client_server_negotiation: Literal["request_server_negotiation"] | None = None
    irods_client_server_policy: Literal["CS_NEG_REQUIRE", "CS_NEG_REFUSE", "CS_NEG_DONT_CARE"] | None = None
    irods_ssl_verify_server: Literal["none", "cert", "hostname"] | None = None
    irods_ssl_ca_certificate_file: Path | None = None
    irods_encryption_algorithm: str | None = None
    irods_encryption_key_size: int | None = None
    irods_encryption_num_hash_rounds: int | None = None
    irods_encryption_salt_size: int | None = None

    # helper functions:
    # these functions never change the class and they always only output something that does not link back to this class

    def as_dict(self, exclude_none = True):
        return self.model_dump(exclude_none=exclude_none)

    def server_version(self):
        env = self.model_dump()
        env.pop("authentication_scheme")
        with iRODSSession(user="", password="", authentication_scheme="native", **env) as ses:
            version = ses.server_version_without_auth()
        return version

    def _session(
        self,
        username: str,
        *,
        password: str,
        scheme: Literal["native", "pam_password"] | None = None,
        **kwargs,
    ) -> iRODSSession:
        scheme = scheme if scheme is not None else self.authentication_scheme

        env = self.as_dict()
        env.pop("authentication_scheme")

        return iRODSSession(
            user=username,
            password=password,
            authentication_scheme=scheme,
            **env,
            **kwargs
        )

    def session_native(self, username: str, native_password: str) -> iRODSSession:
        if self.authentication_scheme not in ("native", "pam_password"):
            raise ValueError(f"`session_native` should not be called with {self.authentication_scheme=}")
        return self._session(username, password=native_password, scheme="native")

    def session_pam(self, username: str, pam_password: str) -> iRODSSession:
        if self.authentication_scheme != "pam_password":
            raise ValueError(f"Attempted to create a pam session for authentication scheme `{self.authentication_scheme}`")
        return self._session(username, password=pam_password, scheme="pam_password")
