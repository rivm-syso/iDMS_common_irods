import pytest
import pkgutil
import importlib
import idms.common.irods

def get_submodules(package):
    modules = []
    for module in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
        modules.append(module)
    return modules

all_modules = [module.name for module in get_submodules(idms.common.irods)]
@pytest.mark.parametrize("package", all_modules, ids = all_modules)
def test_import(package):
    importlib.import_module(package)
