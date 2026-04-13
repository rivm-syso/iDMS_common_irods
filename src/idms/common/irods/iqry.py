import json
import sys
import os
import time
from irods.models import Collection, CollectionMeta, DataObject, DataObjectMeta, User, UserMeta, Resource, ResourceMeta
from irods.meta import iRODSMeta, AVUOperation
from irods.column import Criterion
from irods.exception import CAT_NO_ACCESS_PERMISSION, CollectionDoesNotExist, DataObjectDoesNotExist

from .irods_sessions import irods_manager



def qusermeta(irods_session, user_name):
    #with irods_manager.session() as session:
    q = irods_session.query(UserMeta.name, UserMeta.value, UserMeta.units).filter(
        Criterion('=', User.name, user_name))
    result = [r for r in q]
    return result


def qusermetadict(irods_session, user):
    q = qusermeta(irods_session, user)
    return {r[UserMeta.name]: r[UserMeta.value] for r in q}


def qusermetaval(irods_session, user, attr, default=None, unit=None):
    d = [ m for m in qusermeta(irods_session, user) if m[UserMeta.name] == attr and m[UserMeta.units] == unit ]
    if d:
        return d[0][UserMeta.value]
    else:
        return default


def susermetaval(irods_session, user_name, attr, value, unit=None):
    #with irods_manager.session() as session:
    u = irods_session.users.get(user_name)
    u.metadata[attr] = iRODSMeta(attr, value, unit)


def qresmeta(irods_session, resource):
    #with irods_manager.session() as session:
    q = irods_session.query(ResourceMeta.name, ResourceMeta.value, ResourceMeta.units).filter(
        Criterion('=', Resource.name, resource))
    result = [r for r in q]
    return result

# TODO: This is not useable if units are used
def qresmetadict(irods_session, resource):
    q = qresmeta(irods_session, resource)
    return {r[ResourceMeta.name]: r[ResourceMeta.value] for r in q}


def qcollmeta(irods_session, collection):
    #with irods_manager.session() as session:
    q = irods_session.query(CollectionMeta.name, CollectionMeta.value, CollectionMeta.units).filter(
        Criterion('=', Collection.name, collection))
    result = [r for r in q]
    return result


def scollmetaval(irods_session, coll, attr, value, unit=None):
    if value == '':
        raise ValueError( 'Empty-string not allowed as value of AVU!') 
    if unit is None and qcollmetaval(irods_session, coll, attr) == value:
        return
    #with irods_manager.session() as session:
    u = irods_session.collections.get(coll)
    old_avus = [ iRODSMeta(m[CollectionMeta.name], m[CollectionMeta.value], m[CollectionMeta.units]) for m in qcollmeta(irods_session, coll) if m[CollectionMeta.name] == attr and m[CollectionMeta.units] == unit ]
    new_avu = iRODSMeta(attr, value, unit)

    # The atomic metadata operations are preferred, but require a higher permission level
    try:
        u.metadata.apply_atomic_operations(
            *[AVUOperation(operation='remove', avu=i) for i in old_avus],
            AVUOperation(operation='add', avu=new_avu)
        )
    except CAT_NO_ACCESS_PERMISSION:
        for oa in old_avus:
            u.metadata.remove(oa)
        u.metadata[attr] = new_avu



def addcollmetaval(irods_session, coll, attr, value, unit=None):
    if qcollmetaval(irods_session, coll, attr) == value:
        return
    #with irods_manager.session() as session:
    u = irods_session.collections.get(coll)
    new_avu = iRODSMeta(attr, value, unit)
    u.metadata.add(new_avu)

def rmallcollmetaattr(irods_session, coll, attr):
    #with irods_manager.session() as session:
    u = irods_session.collections.get(coll)
    u.metadata._delete_all_values(attr)

def delcollmeta(irods_session, coll, attr, value=None, unit=None):
    q = qcollmeta(irods_session, coll)
   # with irods_manager.session() as session:
    u = irods_session.collections.get(coll)
    for m in q:
        if m[CollectionMeta.name] == attr:
            if value is None or m[CollectionMeta.value] == value:
                if unit is None or m[CollectionMeta.units] == unit:
                    u.metadata.remove(m[CollectionMeta.name], m[CollectionMeta.value], m[CollectionMeta.units])


# def restore_type( str_value, type_name=None ):
#     if type_name:
#         try:
#             if type_name == 'list':         
#                 return json.loads(str_value)
            
#             # https://stackoverflow.com/questions/11775460/lexical-cast-from-string-to-type
#             #t = getattr(__builtins__, type_name)
#             t = __builtins__[type_name]
#             if not isinstance( t, type):
#                 raise ValueError( f"the unit: '{type_name}' is not a type!")
#             value = t(str_value)
#             return value
#         except Exception as e: 
#             print( f"for value: {str_value} and type: {type_name} got Exception {e}" )
#     return str_value
     

def qcollmetadict(irods_session, collection):
    q = qcollmeta(irods_session, collection)
    return {r[CollectionMeta.name]: r[CollectionMeta.value] for r in q}


# def qcollmetadict_typed(collection):
#     q = qcollmeta(collection)
#     result = {}
#     for r in q:
#         try:
#             v = json.loads(r[CollectionMeta.value])
#         except:
#             v = restore_type(r[CollectionMeta.value], r[CollectionMeta.units])
#         result[r[CollectionMeta.name]] = v
#     return result


def scollmetaval_typed(irods_session, coll, attr, value, unit=None):
    scollmetaval(irods_session, coll, attr, json.dumps(value), unit)


def qcollchildren(irods_session, collection):
    #with irods_manager.session() as session:
    q = irods_session.query(Collection).filter(
        Criterion('=', Collection.parent_name, collection))
    result = [r for r in q]
    return result


def qcolldataobjects(irods_session, collection):
    #with irods_manager.session() as session:
    q = irods_session.query(DataObject.name, DataObject.owner_name, DataObject.size).min(
        DataObject.create_time).filter(
        Criterion('=', Collection.name, collection))
    result = [r for r in q]
    return result


def qcollbymetaattr(irods_session, attr):
    #with irods_manager.session() as session:
    q = irods_session.query(Collection).filter(
        Criterion('=', CollectionMeta.name, attr))
    result = [r for r in q]
    return result


def qcollbymeta(irods_session, attr, value, unit=None):
    #with irods_manager.session() as session:
    q = irods_session.query(Collection).filter(
        Criterion('=', CollectionMeta.name, attr)).filter(
        Criterion('=', CollectionMeta.value, value))
    if unit:
        q = q.filter(Criterion('=', CollectionMeta.units, unit))            
    result = [r for r in q]
    return result


def qcollbystaticmeta(irods_session, attr, value, unit=None):
    #with irods_manager.session() as session:
    q = irods_session.query(Collection).filter(
        Criterion('=', CollectionMeta.name, attr)).filter(
        Criterion('=', CollectionMeta.value, value))
    if unit:
        q = q.filter(Criterion('=', CollectionMeta.units, unit))
    result = [r for r in q]
    return result


def qcollmetavals(irods_session, collection, attr, unit=None):
    q = qcollmeta(irods_session, collection)
    return [r for r in q if r[CollectionMeta.name] == attr and r[CollectionMeta.units] == unit ]


# TODO: Adjust for unit
def qcollmetavals_with_placeholder(irods_session, collection, attr, placeholder='[0]'):
    result = []
    q = qcollmeta(irods_session, collection)
    attr_parts = attr.split(placeholder)
    #fallback in case there is no placeholder
    if len(attr_parts) == 1:
        return qcollmetavals(irods_session, collection, attr)
    for i in range(1000):
        concrete_attr = f"{attr_parts[0]}{i}{attr_parts[1]}"
        matches = [r for r in q if r[CollectionMeta.name] == concrete_attr]
        result += matches
        if len(matches) == 0:
            return result
    return result


def qcollmetaval(irods_session, collection, attr, default=None, unit=None):
    if unit is None:
        d = [ m for m in qcollmeta(irods_session, collection) if m[CollectionMeta.name] == attr ]
    else:
        d = [ m for m in qcollmeta(irods_session, collection) if m[CollectionMeta.name] == attr and m[CollectionMeta.unit] == unit ]
    if d == []:
        return default
    return d[0][CollectionMeta.value]


def qcollmetavalstatic(irods_session, collection, attr, default=None, unit=None):
    m = qcollmetaval(irods_session, collection, attr, default=default, unit=unit)
    return m


def qcollproperty(irods_session, collection, property):
    #with irods_manager.session() as session:
    c = irods_session.collections.get(collection)
    return getattr(c, property)


def qdataobjmeta(irods_session, dataobject):
    #with irods_manager.session() as session:
    # split in dataobject_name and collection
    coll, dataobject_name = os.path.split(dataobject)
    q = irods_session.query(DataObjectMeta.name, DataObjectMeta.value, DataObjectMeta.units).filter(
        Criterion('=', DataObject.name, dataobject_name)).filter(
        Criterion('=', Collection.name, coll))
    result = [r for r in q]
    return result


def qdataobjbymeta(irods_session, attr, value):
    #with irods_manager.session() as session:
    q = irods_session.query(Collection.name, DataObject.name).filter(
        Criterion('=', DataObjectMeta.name, attr)).filter(
        Criterion('=', DataObjectMeta.value, value))
    result = [r for r in q]
    return result


def qcolldataobjectpaths(irods_session, collection):
    '''
    Query to fetch dataobject names for a collection.
    Returns paths of data objects in a given collection'''
    #with irods_manager.session() as session:
    q = irods_session.query(Collection.name, DataObject.name).min(
        DataObject.create_time).filter(
        Criterion('=', Collection.name, collection))
    dataobject_paths = [list(d.values())[0] + '/' + list(d.values())[1] for d in q]
    return dataobject_paths


def qpathobjecttype(irods_session, path):
    '''
    Check whether an iRODS path is a collection or a data_object
    
    params:
        path to be checked
    return:
        'path', 'dataobject' or 'not_found'
    '''
    #with irods_manager.session() as session:
    try:
        # Try to get it as a DataObject
        obj = irods_session.data_objects.get(path)
        return 'dataobject'
    except (CollectionDoesNotExist, DataObjectDoesNotExist):
        pass

    try:
        # Try to get it as a Collection
        coll = irods_session.collections.get(path)
        return 'path'
    except CollectionDoesNotExist:
        pass

    return 'not_found'