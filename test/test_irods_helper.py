import pytest
from src.idms.common.irods.irods_helper import OBJECT_TYPE


@pytest.mark.unit
def test_object_type():
    assert OBJECT_TYPE.DATAOBJECT == 0
    assert OBJECT_TYPE.COLLECTION == 1
