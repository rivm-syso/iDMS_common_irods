#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Jun 19 12:53:26 2020

@author: wierinve
"""
import os
import shutil
import ssl
import time
import uuid
from irods.session import iRODSSession
from irods.meta import iRODSMeta
from irods.models import DataObject, Collection, CollectionMeta
from irods.column import Criterion
from irods.exception import DataObjectDoesNotExist, CollectionDoesNotExist
from datetime import timezone
from idms.common.constants.attribute_names import META_SUFFIXLENGTH, ATTR_DATASETID
#from constants import *

from irods.exception import (
    CATALOG_ALREADY_HAS_ITEM_BY_THAT_NAME,
    CAT_SUCCESS_BUT_WITH_NO_INFO
)

BUF_SIZE = 1024 * 1024 * 4

class DATA_REPL_STATUS:
    STALE_REPLICA = '0'
    GOOD_REPLICA = '1'
    INTERMEDIATE_REPLICA = '2'
    READ_LOCKED = '3'
    WRITE_LOCKED = '4'

class OBJECT_TYPE:
    DATAOBJECT = 0
    COLLECTION = 1    

class ENoUniqueCollection(Exception):
    """Raised when an unique collection name cannot be found
    """
    pass

def irodsConnect(irodsfile="", use_ssl=False, **kwargs):
    """Connect to irods iCAT and return iRODSSession object

    Args:
        irodsfile: irods environment file to use.
        use_ssl: use ssl if True

    Returns:
        iRODSSession object
    """
    if irodsfile:
        envFile = irodsfile
    else:
        try:
            envFile = os.environ['IRODS_ENVIRONMENT_FILE']
        except KeyError:
            envFile = os.path.expanduser('~/.irods/irods_environment.json')

    if use_ssl:
        context = ssl._create_unverified_context(purpose=ssl.Purpose.SERVER_AUTH,
                                             cafile=None, capath=None, cadata=None)
        ssl_settings = {'irods_ssl_ca_certificate_file': '/etc/irods/ssl/irods.crt',
                        'ssl_context': context}
        session = iRODSSession(irods_env_file=envFile, **ssl_settings, **kwargs)
    else:
        session = iRODSSession(irods_env_file=envFile, **kwargs)
    return session

class Dataset:
    """Dataset class that holds a dataset location and possible suffix length
    """
    def __init__(self, irodsSession, path, suffix_length=0, create=False):
        self._sess = irodsSession
        self.path = path
        self.obj = None
        if self._sess.collections.exists(path):
            self.obj = self._sess.collections.get(path)
            self.suffix_length = getmetaitem(self.obj, META_SUFFIXLENGTH, 0)
        else:
            self.suffix_length = suffix_length
        if create:
            self.create()

    @classmethod
    def from_collection(cls, coll):
        return cls(coll.manager.sess, coll.path)

    @property
    def basename(self):
        if self.suffix_length:
            return self.path[:-self.suffix_length-1]
        else:
            return self.path

    def create(self):
        logcoll = os.path.join(self.path, 'log')
        if not self._sess.collections.exists(logcoll):
            self._sess.collections.create(logcoll, recurse=True)
            self.obj = self._sess.collections.get(self.path)
        self.update()

    def update(self):
        if not self.obj and self._sess.collections.exists(self.path):
            self.obj = self._sess.collections.get(self.path)
        if self.obj:
            if not getmetaitem(self.obj, META_SUFFIXLENGTH):
                self.obj.metadata[META_SUFFIXLENGTH] = iRODSMeta(META_SUFFIXLENGTH, str(self.suffix_length))
            get_or_set_uid(self.obj)

# Some functions to work with dataobjects

def provide_dataobject(irodsSession, path):
    """Get or create a dataobject"

    Args:
        irodsSession: iRODSSession object
        path        : iRods path to the dataobject

        return      : DataObject instance
    """
    if irodsSession.data_objects.exists(path):
        obj = irodsSession.data_objects.get(path)
    else:
        obj = irodsSession.data_objects.create(path)
    return obj

def read_dataobject(data_obj):
    """Read <line> to dataobject <data_obj>
    """
    with data_obj.open('r') as f:
        d = f.read()
    return d

def write_dataobject(data_obj, line):
    """Append <line> to dataobject <data_obj>
    """
    with data_obj.open('a+') as f:
        f.seek(0,2)
        f.write(line.encode('utf-8'))

def copy_dataobject(source, dpath):
    dest_file = source.manager.sess.data_objects.open(dpath, 'w', create=True)
    source_file = source.open('r')
    shutil.copyfileobj(source_file, dest_file, BUF_SIZE)
    dest_file.close()
    source_file.close()

###

def collection_mtime(coll_obj):
    """Return the most recent timestamp
    any of the objects in a collection has been modified"
    """
    mtime = 0
    query = coll_obj.manager.sess.query(DataObject.modify_time).filter(
        Criterion('=', Collection.name, coll_obj.path)).max(DataObject.modify_time)
    mdatetime = query.execute()[0][DataObject.modify_time]
    # mdatetime is in UTC:
    if mdatetime:
        mtime = mdatetime.replace(tzinfo=timezone.utc).timestamp()
    query = coll_obj.manager.sess.query(DataObject.modify_time).filter(
        Criterion('like', Collection.name, '{}/%'.format(coll_obj.path))).max(DataObject.modify_time)
    mdatetime = query.execute()[0][DataObject.modify_time]
    if mdatetime:
        mtime = max(mtime, mdatetime.replace(tzinfo=timezone.utc).timestamp())
    return int(mtime//1)

# Find the dataset a dataobject is in

def dataset_from_dataobject(irodsSession, search_path):
    """Find the dataset a dataobject is in

    Args:
        irodsSession (iRODSSession): iRODSSession object
        search_path (str): path to iRODS dataobject

    Raises:
        Exception: when a dataset cannot be found

    Returns:
        Dataset: Dataset object
    """
    path, name = os.path.split(search_path)
    if name == '':
        raise Exception
    path_obj = irodsSession.collections.get(path)
    if getmetaitem(path_obj, ATTR_DATASETID):
        return Dataset.from_collection(path_obj)
    return dataset_from_dataobject(irodsSession, path)

# Some metadata related functions

def get_avu(irods_obj, attr):
    try:
        avu = irods_obj.metadata.get_one(attr)
    except KeyError:
        avu = None
    return avu

def getmetaitem(irods_obj, attr, default=None):
    """Returns the value of the AVU specified with attr on irods_obj

    Args:
        irods_obj (irods object): The irods object to get metadata of
        attr (str): The AVU attribute name
        default (str, optional): Default value when attr is not present. Defaults to None.

    Returns:
        str: The metadata value or the value of default if metadata is not found
    """
    try:
        value = irods_obj.metadata.get_one(attr).value
    except KeyError:
        value = default
    return value

def getmetaitems(irods_obj, attr):
    """Returns a list of values of the AVU specified with attr on irods_obj

    Args:
        irods_obj (irods object): The irods object to get metadata of
        attr (str): The AVU attribute name

    Returns:
        list: list of values of the AVU specified with attr on irods_obj
    """
    return [item.value for item in irods_obj.metadata.items() if item.name == attr]


def get_objecttype(obj):
    # returns
    #  -C if obj is a collection
    #  -d if obj is a data object
    if obj.manager.session.collections.exists(obj):
        return("-C")
    if obj.manager.session.data_objects.exists(obj):
        return("-d")
    return None

# Functions for handling and creating datasets
def get_unique_dataset_name(irodsSession, prefix, name_hint, SUFFIX_LENGTH=4):
    """ A dataset name is globally unique, and will consist of
    The name_hint and an additional suffix if needed"""
    def collection_exists_anywhere(session, collname):
        q = session.query(Collection.name).filter( \
            Criterion('like', Collection.name, '%/{}'.format(collname)))
        return len(list(q.get_results())) > 0

    def suffixlist(session, collname):
        q = session.query(Collection.name, CollectionMeta.value).filter( \
            Criterion('like', Collection.name, '%/{}%'.format(collname))).filter(
            Criterion('=', CollectionMeta.name, META_SUFFIXLENGTH))
        suffixes = [ r[Collection.name][-int(r[CollectionMeta.value]):] for r in q ]
        return suffixes

    # Initialize new_name and suffix_length
    newname = '{}_{}'.format(name_hint, '0' * SUFFIX_LENGTH)
    suffixes = suffixlist(irodsSession, name_hint)
    suffix_start = 0

    while collection_exists_anywhere(irodsSession, newname):
        # Loop through names until a unique one is found
        for i in range(suffix_start, 10**SUFFIX_LENGTH):
            suffix = str(i).zfill(SUFFIX_LENGTH)
            if suffix not in suffixes:
                newname = '{}_{}'.format(name_hint, suffix)
                suffix_start = i + 1
                break
        else:
            raise ENoUniqueCollection
    datasetname = os.path.join(prefix, newname)
    return (datasetname, SUFFIX_LENGTH)

def generate_unique_dataset_from(prefix, base_collection, create=False):
    """Create a new collection based on the name of <base_collection>
    in the parent collection <prefix>
    """
    name_hint = get_collection_basename(base_collection)
    return generate_unique_dataset(base_collection.manager.sess, prefix, name_hint, create=create)

def generate_unique_dataset(irodsSession, prefix, name_hint, create=False):
    """Create a unique datasetname based on the provided name_hint

    Args:
        irodsSession    : irodsSession object
        prefix ([str]): [base path for generated collection]
        base_collection ([collection object]): [collection to base the generated collection name on]

    Returns:
        Collection: [description]
    """
    datasetname, suffix_length = get_unique_dataset_name(irodsSession,prefix,name_hint)
    return Dataset(irodsSession, datasetname, suffix_length=suffix_length, create=create)


def get_collection_basename(coll_obj):
    """Get the basename of a collection, without suffix

    Args:
        coll_obj (irodsCollection): Collection to get the base name of

    Returns:
        str: basename of collection
    """
    name = coll_obj.name
    coll_suffix_length = getmetaitem(coll_obj, META_SUFFIXLENGTH)
    if coll_suffix_length:
        basename = name[:-int(coll_suffix_length)-1]
    else:
        basename = name
    return basename


def get_or_set_avu(object, attr, value, unit=None):
    avu = get_avu(object, attr)
    if not avu:
        new_avu = iRODSMeta(attr, value, unit)
        object.metadata.add(new_avu)
    return avu


def get_or_set_uid(coll_obj):
    """If the referred collection has no dataset_id, generate one
    Return the dataset_id
    """
    uid = getmetaitem(coll_obj, ATTR_DATASETID)
    if not uid:
        uid = str(uuid.uuid4())
        coll_obj.metadata[ATTR_DATASETID] = iRODSMeta(ATTR_DATASETID, uid)
    return uid


def physical_collection_path(coll_obj, resource ):
    dataobj = "{}/__parmenides__".format(coll_obj.path)
    tmp_obj = coll_obj.manager.sess.data_objects.create(dataobj, resource=resource)
    physical_path = None
    for replica in tmp_obj.replicas:
        if replica.resource_name == resource:
            physical_path = replica.path
    tmp_obj.unlink(force=True)
    physical_dir, _ = os.path.split(physical_path)
    return physical_dir

def timestamp_object(obj, attr):
    """Write a unix timestamp value to obj

    Args:
        obj:  iRODS object to set timestamp to
        attr: attr name the timestamp is set to
    Returns:
        Nothing
    """
    new_meta = iRODSMeta(attr, str(time.time()//1), '')
    obj.metadata[attr] = new_meta


def uuidshort(length):
    return str(uuid.uuid4()).replace('-', '')[:length]

def set_collection_ref(coll_obj, metadata_attr, referenced_coll_obj):
    """Set the value of metadata_attr on collname to the dataset_id of referenced_coll

    Check if the item not already is referenced in the metadata to prevent
    duplicate key errors in database
    """
    # create list of existing metadata items
    meta_items = getmetaitems(coll_obj, metadata_attr)

    # get the referenced uid
    refuid = get_or_set_uid(referenced_coll_obj)

    # check if meta_item already exists, if not add
    if refuid not in meta_items:
        coll_obj.metadata.add(iRODSMeta(metadata_attr, refuid))


def collection_from_id(irodsSession, dataset_id):
    q = irodsSession.query(Collection).filter(
        Criterion('=', CollectionMeta.name, ATTR_DATASETID)).filter(
        Criterion('=', CollectionMeta.value, dataset_id))
    coll = None
    for r in q:
        coll = irodsSession.collections.get(r[Collection.name])
    return coll

def id_from_collection(irodsSession, collection):
    q = irodsSession.query(CollectionMeta).filter(
        Criterion('=', Collection.name, collection)).filter(
        Criterion('=', CollectionMeta.name, ATTR_DATASETID))
    datasetid = None
    for r in q:
        datasetid = r[CollectionMeta.value]
    return datasetid    

def localtimestamp(datetime):
    ctime = datetime.replace(tzinfo=timezone.utc).astimezone()
    return ctime.timestamp()

def object_with_type(irodsSession, path):
    """Return object and object_type for path
    """
    try:
        do = irodsSession.data_objects.get(path)
        return OBJECT_TYPE.DATAOBJECT, do
    except (DataObjectDoesNotExist, CollectionDoesNotExist):
        pass
    try:
        co = irodsSession.collections.get(path)
        return OBJECT_TYPE.COLLECTION, co
    except CollectionDoesNotExist:
        pass
    return None, None