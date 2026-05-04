import collections
import time
from datetime import timezone
from irods.meta import iRODSMeta
from irods.models import DataObject, Collection, CollectionMeta
from irods.column import Criterion
from irods.exception import CAT_SUCCESS_BUT_WITH_NO_INFO
from .irods_helper import uuidshort, getmetaitem

#from constants import ATTR_LOCK
from idms.common.constants.attribute_names import ATTR_LOCK

# For concurrent lock requests, a special metadata attr is added
# to a collection while a new ticket is being created
# This indicates if there are still processes requesting a lock
# When this choose metadata is older than CHOOSE_TIMEOUT, it will 
# probably be stale, and will be removed
CHOOSE_TIMEOUT = 60

# The next four rules implement a mechanism to lock 
# exclusive acces to a collection, based on the bakery algorithm
# Usage:
#  1) Call lock_get_ticket (once) to get a ticket and a job id
#  2) Call lock_check_turn (repeatedly), to check if its your turn
#     to process *coll
#  3) Run critical collection processing code
#  4) Call lock_release_ticket upon finishing critical processing
#
# Jobs will be run order by ticket number, and when two jobs have the 
# same ticket numbers: by jobid

Attrs = collections.namedtuple('Attrs', 
    ['choose', 'description', 'ticket', 'timeout', 'runtime', 'valid_till', 
     'ticket_wildcard', 'class_wildcard', 'valid_till_wildcard', 'lock_wildcard', 'count_wildcard', 'all_lock_wildcard'])

def attr_names(id='UNKNOWN', classname="none", attr_lock=ATTR_LOCK):
    """Returning names of locking attributes
        id:  ticket id
        classname: ticket class
        
    """
    attrs = Attrs(
        f'{attr_lock}{classname}::choose',
        f'{attr_lock}{classname}::desc::{id}',
        f'{attr_lock}{classname}::ticket::{id}',
        f'{attr_lock}{classname}::time::{id}::timeout',
        f'{attr_lock}{classname}::time::{id}::runtime',
        f'{attr_lock}{classname}::time::{id}::valid_till',
        f'{attr_lock}{classname}::ticket::%',
        f'{attr_lock}%::ticket::{id}',
        f'{attr_lock}%::time::%::valid_till',
        f'{attr_lock}%{id}%',
        f'{attr_lock}{classname}::desc::%',
        f'{attr_lock}%'
    )
    return attrs

def get_class(collection, id):
    attrs = attr_names(id)
    query = collection.manager.sess.query(CollectionMeta.name).filter( \
        Criterion('=', Collection.id, collection.id)).filter( \
        Criterion('like', CollectionMeta.name, attrs.class_wildcard))
    myclass = "UNKNOWN"
    for r in query:
        attr = r[CollectionMeta.name].split('::')
        myclass = attr[2]
    return myclass

def lock_get_ticket(collection, description, timeout, runtime, classname="none"):
    # First remove any expired tickets
    lock_remove_expired(collection)

    id = uuidshort(12)
    attrs = attr_names(id, classname=classname)

    # Set the CHOOSE avu to the collection
    meta_choose = iRODSMeta(attrs.choose, id)
    collection.metadata.add(meta_choose)
    # find the highest ticket number for my collection
    query = collection.manager.sess.query(CollectionMeta.value).filter( \
        Criterion('=', Collection.id, collection.id)).filter( \
        Criterion('like', CollectionMeta.name, attrs.ticket_wildcard))
    ticketnr = 0 
    for r in query:
        if int(r[CollectionMeta.value]) > ticketnr:
            ticketnr = int(r[CollectionMeta.value])
    ticketnr += 1

    # Attach metadata to the collection
    collection.metadata.add(iRODSMeta(attrs.ticket, str(ticketnr)))
    collection.metadata.add(iRODSMeta(attrs.description, description))
    collection.metadata.add(iRODSMeta(attrs.timeout, str(timeout)))
    collection.metadata.add(iRODSMeta(attrs.runtime, str(runtime)))
    collection.metadata.add(iRODSMeta(attrs.valid_till, str(time.time()//1 + timeout)))

    # Remove the choose lock
    collection.metadata.remove(meta_choose)

    # Wait till there are no more ticket requests pending
    while True:
        query = collection.manager.sess.query(CollectionMeta).filter( \
            Criterion('=', Collection.id, collection.id)).filter( \
            Criterion('=', CollectionMeta.name, attrs.choose))
        results = list(query)
        if len(results) == 0:
            break
        for r in results:
            age = time.time() - r[CollectionMeta.create_time].replace(tzinfo=timezone.utc).timestamp()
            if age > CHOOSE_TIMEOUT:
                collection.metadata.remove(iRODSMeta(r[CollectionMeta.name], r[CollectionMeta.value]))
        time.sleep(1)

    # return the ticketid
    return id

def lock_check_turn(collection, id):
    # Check if this job is up for processing
    # INPUT 
    #    session    : irodsSession object
    #    collection : collection name
    #    id         : job id, as retrieved with lock_get_ticket
    # OUTPUT
    #    return value:
    #           0:      Ticket is up, continue
    #           1:      Ticket is not up, wait
    #           2:      Ticket not found

    # Remove any expired tickets
    lock_remove_expired(collection)

    myclass = get_class(collection, id)

    attrs = attr_names(id, myclass)

    # Find my ticket
    query = collection.manager.sess.query(CollectionMeta.name, CollectionMeta.value).filter( \
        Criterion('=', Collection.id, collection.id)).filter( \
        Criterion('=', CollectionMeta.name, attrs.ticket))
    ticket = 0
    for r in query:
        ticket = int(r[CollectionMeta.value])
    if not ticket:
        return 2
    
    # Extend my ticket
    check_time = getmetaitem(collection, attrs.timeout, 0)
    lock_extend_ticket(collection, id, check_time)

    # Find lowest ticket number
    lowest_ticket = 999
    lowest_id = 'zzz'

    query = collection.manager.sess.query(CollectionMeta.name, CollectionMeta.value).filter( \
        Criterion('=', Collection.id, collection.id)).filter( \
        Criterion('like', CollectionMeta.name, attrs.ticket_wildcard))
    for r in query:
        lowest_ticket = min(lowest_ticket, int(r[CollectionMeta.value]))

    if ticket != lowest_ticket:
        return 1

    # Check for lowest ID
    query = collection.manager.sess.query(CollectionMeta.name, CollectionMeta.value).filter( \
        Criterion('=', Collection.id, collection.id)).filter( \
        Criterion('like', CollectionMeta.name, attrs.ticket_wildcard)).filter( \
        Criterion('=', CollectionMeta.value, ticket))
    for r in query:
        attr = r[CollectionMeta.name]
        qid = attr[len(attrs.ticket_wildcard)-1:]
        lowest_id = min(lowest_id, qid)

    if id != lowest_id:
        return 1

    timeout = getmetaitem(collection, attrs.runtime, 0)
    lock_extend_ticket(collection, id, timeout)

    return 0

def lock_extend_ticket(collection, id, timeout):
    myclass = get_class(collection, id)
    attrs = attr_names(id, myclass)
    valid_time = int(time.time()) + int(timeout)
    valid_meta = iRODSMeta(attrs.valid_till, str(valid_time))
    collection.metadata[attrs.valid_till] = valid_meta


def lock_release_ticket(collection, id):
    # Release(delete) ticket with id
    # INPUT
    #     collection  : collection name
    #     id          : ticket id
    myclass = get_class(collection, id)
    attrs = attr_names(id, myclass)

    query = collection.manager.sess.query(CollectionMeta.name, CollectionMeta.value).filter( \
        Criterion('=', Collection.id, collection.id)).filter( \
        Criterion('like', CollectionMeta.name, attrs.lock_wildcard))
    for r in query:
        meta_delete = iRODSMeta(r[CollectionMeta.name], r[CollectionMeta.value])
        # Run this in a try - except to prevent concurrency errors 
        try:
            collection.metadata.remove(meta_delete)
        except CAT_SUCCESS_BUT_WITH_NO_INFO:
            pass

def lock_remove_expired(collection):
    attrs = attr_names()
    now = time.time()
    query = collection.manager.sess.query(CollectionMeta.name, CollectionMeta.value).filter( \
        Criterion('=', Collection.id, collection.id)).filter( \
        Criterion('like', CollectionMeta.name, attrs.all_lock_wildcard))
    ticket_valid_times = {}
    for r in query:
        attr = r[CollectionMeta.name]
        attr_array = attr.split('::')
        if len(attr_array) < 5:
            continue
        ticket_id = attr_array[4]
        if attr.endswith('valid_till'):
            ticket_valid_times[ticket_id] = float((r[CollectionMeta.value]))
        else:
            ticket_valid_times.setdefault(ticket_id, 0)
    for ticket_id, valid_till in ticket_valid_times.items():
        if valid_till < now:
            lock_release_ticket(collection, ticket_id)

def lock_ticket_count(collection, description=None, classname="none"):
    """Count tickets for the specified class
        if classname is None, or '%', count tickets for all classes
    """
    if classname is None:
        classname = '%'

    lock_remove_expired(collection)

    attrs = attr_names(classname=classname)

    query = collection.manager.sess.query(CollectionMeta.name).filter( \
        Criterion('=', Collection.id, collection.id)).filter( \
        Criterion('like', CollectionMeta.name, attrs.count_wildcard))
    if description:
        query = query.filter(Criterion('=', CollectionMeta.value, description))
    query = query.count(CollectionMeta.name)
    result = int(next(query.get_results())[CollectionMeta.name])

    return result
        