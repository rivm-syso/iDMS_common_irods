
import logging
import time
import os
from . import iqry
from irods.models import DataObject, Collection, CollectionMeta, Resource, \
    ResourceMeta
from irods.column import Criterion
from irods.meta import iRODSMeta

from .irods_helper import localtimestamp
from idms.common.constants.attribute_names import ATTR_RESOURCE_SPACETARGET, ATTR_RESOURCE_SPACELIMIT, ATTR_RESOURCE_MAXCOPIES, ATTR_TIERING_GROUP
from idms.common.constants.attribute_names import ATTR_ARCHIVE_STATE, ATTR_ARCHIVE_DESIREDSTATE, ATTR_COLLSIZE,ATTR_PIPELINE_USEDBY, ATTR_RUNSHEET_STATE, ATTR_RUNSHEET_ID
from idms.common.constants.attribute_names import ATTR_ARCHIVE_STAGE, ATTR_RESOURCE_PREFIX, ATTR_RESOURCE_ENABLED, ATTR_RESOURCE_AVAILABLE, ATTR_RESOURCE_TAR

TRUE = 'true'
FALSE = 'false'

DEFAULT_COLL_SIZE_CACHE_TIME = 30 * 86400
EXABYTE = 10**18

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
class IRResource():
    def __init__(self, resource):
        self._resource = resource
        self._metadata = { i.name: i.value for i in resource.metadata.items()}

    @property
    def name(self):
        return self._resource.name

    @property
    def freespace(self):
        if self._resource.free_space is None:
            # We don't know the free space
            # Assume we have a lot
            return EXABYTE
        else:
            return int(self._resource.free_space)

    def has(self, attr, value=TRUE):
        return self._metadata.get(attr) == value

    def meta(self, attr, default=None):
        return self._metadata.get(attr, default)

    @property
    def spacetarget(self):
        return int(self.meta(ATTR_RESOURCE_SPACETARGET, 0))

    @property
    def spacelimit(self):
        return int(self.meta(ATTR_RESOURCE_SPACELIMIT, 0))

    @property
    def maxcopies(self):
        return int(self.meta(ATTR_RESOURCE_MAXCOPIES, 0))

    def __repr__(self):
        return self.name
        
     

class IRResourceList:
    def __init__(self, irodsSession, group):
        self._resources = {}
        q = irodsSession.query(Resource, ResourceMeta).filter(
                Criterion('=', ResourceMeta.name, ATTR_TIERING_GROUP)).filter(
                Criterion('=', ResourceMeta.value, group))
        for r in q:
            self._resources[int(r[ResourceMeta.units])-1] = IRResource(irodsSession.resources.get(r[Resource.name]))

    def count(self):
        return len(self._resources)

    def get(self, i):
        if i < len(self._resources):
            return self._resources[i]
        else:
            return None

    def __iter__(self):
        self.n = 0
        return self

    def __next__(self):
        if self.n < self.count():
            r = self.get(self.n)
            self.n += 1
            return r
        else:
            raise StopIteration
        
    def __repr__(self):
        return str(list(self._resources.values()))
        
class State:
    def __init__(self, resources, statestring):
        self._resources = resources
        self._state = []
        self._nostate = []
        self._statestr = statestring

        for i in range(0, len(statestring)):
            resource = resources.get(i)
            if resource:
                if statestring[i] == '1':
                    self._state.append(resource)
                else:
                    self._nostate.append(resource)

    def set_all(self):
        self._state = []
        self._nostate = []
        for r in self._resources:
            self._state.append(r)

    def clear(self, resource):
        if resource in self._state:
            self._state.remove(resource)
        if resource not in self._nostate:
            self._state.append(resource)
            
    def values_in_all_active(self, attr):
        result = set()
        for r in self._resources:
            if not (v := r.meta(attr)) is None:
                result.add(v)
        return result

    def dont_set_resource(self, other_state, resource):
        statediff = self - other_state
        for r in statediff:
            if r == resource:
                return False
        return True
    
    def dont_clear_resource(self, other_state, resource):
        statediff = other_state - self
        for r in statediff:
            if r == resource:
                return False
        return True    

    def clear_resource(self, other_state, resource):
        if resource not in other_state._state:
            return True
        if resource not in self._state:
            return True
        return False 

    def in_any_set_tiers(self, attr):
        """ Returns True if any active tiers have <attr> set
        """
        for r in self._state:
            if r.has(attr):
                return True
        return False

    def in_any_set_tiers_multi(self, attrlist):
        """ Returns True if any active tiers have all <attr> set to <value>
            attrlist: [(k, v), (k, v) ...]
        """
        for r in self._state:
            result = True
            for k, v in attrlist:
                result = result & r.has(k, value=v)
            if result:
                return True
        return False

    def in_all_set_tiers(self, attr):
        for r in self._state:
            if not r.has(attr):
                return False
        return True

    def in_no_set_tiers(self, attr):
        """ Returns true if no tiers have attr=true
        """
        for r in self._state:
            if r.has(attr):
                return False
        return True

    def in_unset_tiers(self, attr):
        """ Return true if any inactive tiers have <attr>=true
        """
        for r in self._nostate:
            if r.has(attr):
                return True
        return False

    def not_in_unset_tiers(self, attr):
        """ Return true if no inactive tiers have <attr>=true
        """
        return not self.in_unset_tiers(attr)

    def in_all_unset_tiers(self, attr):
        for r in self._nostate:
            if not r.has(attr):
                return False
        return True
    
    def dont_clear_any(self, other):
        if other - self:
            return False
        return True

    def dont_clear(self, other, attr, value=TRUE):
        change = other - self
        for r in change:
            if r.has(attr, value=value):
                return False
        return True

    def dont_set(self, other, attr, value=TRUE):
        change = self - other
        for r in change:
            if r.has(attr, value=value):
                return False
        return True

    def must_set(self, other, attr, value=TRUE):
        change = self - other
        for r in change:
            if not r.has(attr, value=value):
                return False
        return True

    def must_clear(self, other, attr, value=TRUE):
        change = other - self
        for r in change:
            if not r.has(attr, value=value):
                return False
        return True

    def numeric_add(self, attr):
        total = 0
        for r in self._state:
            total += float(r.meta(attr, default=0))
        return total

    def empty(self):
        return not bool(self._state)

    def migrations(self, other_state):
        return len(self - other_state) + len(other_state - self)
    
    def datamove(self, sizes_on_resource, total_size):
        datacopy = 0
        for r in self._state:
            datacopy += total_size - sizes_on_resource.get(r.name, 0)
        return datacopy 

    def minimum_total(self, attr, value):
        return self.numeric_add(attr) >= value

    def __sub__(self, other):
        ret_val = []
        for s in self._state:
            if not s in other._state:
                ret_val.append(s)
        return ret_val

    # def __eq__(self, other):
    #     return str(self) == str(other)

    def __repr__(self):
        repr = ''
        for i in range(0, self._resources.count()):
            r = self._resources.get(i)
            repr = '{}{}'.format(repr, '1' if r in self._state else '0')
        return repr

class StateList:
    def __init__(self, resources):
        self._resources = resources
        self._states = []
        format_string = '{{i:0{l}b}}'.format(l=self._resources.count())
        for i in range(1, 2**self._resources.count()):
            self._states.append(State(self._resources, format_string.format(i=i)))

    def try_to_set(self, new_list):
        """Set the statelist to new_list if new_list has at least one member
        """
        if new_list:
            self._states = new_list
            return True
        else:
            return False

    def try_to_apply(self, func, *attr):
        return self.try_to_set([s for s in self.states if func(s, *attr)])

    def sort_and_cut(self, func, *args):
        dbg = { state: func(state, *args) for state in self._states }
        self._states = sorted(self._states, key=lambda state: func(state, *args))
        if self._states is None:
            return 
        cost = dbg[self._states[0]]
        self._states = [ s for s in self._states if dbg[s] == cost ]

    @property
    def states(self):
        return self._states

    def __repr__(self):
        states = ''
        for s in self._states:
            states = '{}{}{}'.format(states, ',' if states else '', s)
        return "[ {} ]".format(states)


class Dataset:
    def __init__(self, mgr, coll):
        self._coll = coll
        self._mgr = mgr
        self._collobj = mgr.irodsSession.collections.get(coll)
        self._meta = None
        self.flush = False
        self._create_time = None
        self._state = State(mgr._resources, self.getmeta(ATTR_ARCHIVE_STATE, default=''))
        self._desiredstate = State(mgr._resources, self.getmeta(ATTR_ARCHIVE_DESIREDSTATE, default=''))

    def __repr__(self):
        return self._coll

    @property
    def state(self):
        return self._state

    def copy_in_progress(self):
        return self._desiredstate - self._state

    def trim_in_progress(self):
        return self._state - self._desiredstate

    @property
    def create_time(self):
        if self._create_time is None:
            q = self._mgr.irodsSession.query(Collection).filter(
                Criterion('=', Collection.id, self._collobj.id)
            )
            for r in q:
                self._create_time = r[Collection.create_time]
        return localtimestamp(self._create_time)

    def size(self, resource=None):

        if resource:
            size_key = '{}::{}'.format(ATTR_COLLSIZE, resource.name)
        else:
            size_key = ATTR_COLLSIZE

        return int(self.getmeta(size_key, 0))

    @property
    def meta(self):
        if self._meta is None:
            self._meta = {i.name: i.value for i in self._collobj.metadata.items()}
        return self._meta      

    def getmeta(self, attr, default=None, tree=False):
        val = self.meta.get(attr)
        if val is None:
            if tree:
                if self._coll == '/':
                    return default
                else:
                    parent = os.path.dirname(self._coll)
                    return self._mgr.get(parent).getmeta(attr, default=default, tree=tree)
            else:
                return default
        return val

    def setmeta(self, attr, value):
        self._collobj.metadata[attr] = iRODSMeta(attr, value)

    def metalist(self, attr):
        q = self._mgr.irodsSession.query(CollectionMeta).filter( \
            Criterion('=', Collection.id, self._collobj.id)).filter( \
            Criterion('like', CollectionMeta.name, attr))
        return [ r[CollectionMeta.value] for r in q ]
        #return [ m.value for m in self._collobj.metadata.get_all(attr)]

    def verify_usage(self, check=False):
        """If usage is not empty, check if the corresponding runsheets are existing and in an active state

        Args:
            coll (): [description]
        """

        # Get the list of runsheets from the usage parameter
        if ATTR_PIPELINE_USEDBY in self.meta:
            use = self.metalist(ATTR_PIPELINE_USEDBY)

            # Try to find all runsheets that are using this collection
            for runsheet in use:
                q = self._mgr.irodsSession.query(Collection.name).filter(
                    Criterion('=', CollectionMeta.name, ATTR_RUNSHEET_ID)).filter(
                    Criterion('=', CollectionMeta.value, runsheet))
                active_runsheet_found = False
                for rs in q: # This will give us at most 1 collection, hopefully
                    c = self._mgr.get(rs[Collection.name])
                    # If the runsheet collection is not active anymore, remove from ATTR_PIPELINE_USEDBY
                    if c.getmeta(ATTR_RUNSHEET_STATE) != 'done':
                        active_runsheet_found = True
                        break
                if active_runsheet_found == False:
                    # The runsheet in USEDBY is not found anywhere
                    logger.debug(f'Remove runsheet {runsheet} from usage list on collection {self._coll}')
                    if not check:
                        self._collobj.metadata.remove(ATTR_PIPELINE_USEDBY, runsheet)
                    else:
                        print(f'Remove ({ATTR_PIPELINE_USEDBY}, {runsheet}) from {self}')                    
                    
            use = self.metalist(ATTR_PIPELINE_USEDBY)
        else:
            use = None

        if not use:
            logger.debug(f'Clear STAGE attr on collection {self}')
            if not check:
                self._collobj.metadata._delete_all_values(ATTR_ARCHIVE_STAGE)
                self._meta = None
            else:
                print(f'Clear STAGE attr on collection {self}')


class DatasetMgr:
    def __init__(self, irodsSession, resources):
        self._colls = {}
        self._resources = resources
        self.irodsSession = irodsSession

    def get(self, coll):
        if not coll in self._colls:
            self._colls[coll] = Dataset(self, coll)
        return self._colls.get(coll)



class Tier:
    """A storage tier, corresponding to an iRODS resource
    """
    def __init__(self, tier, resourcename):
        self.tiernumber = tier
        self.resourcename = resourcename
        self._resourcetags = []
        self._available = True
        self._availabilty_check_time = 0
        self.refresh()

    def refresh(self):
        self._resourcetags = []
        meta = iqry.qresmetadict(self.resourcename)
        for key in meta:
            if key.startswith(ATTR_RESOURCE_PREFIX) and meta[key] == 'true':
                self._resourcetags.append(key)

    def available(self):
        """Check if resource is available and enabled
        Only returns true if 'sys::resource::available' and 'sys::resource::enabled' are present and
        have the value 'true'
        """
        enabled = self._hastags(ATTR_RESOURCE_ENABLED)
        available = self._hastags(ATTR_RESOURCE_AVAILABLE)
        return available and enabled

    def hastags(self, tags):
        """Returns true if tags in <tags> are all present in resourcetags
        """
        if isinstance(tags, str):
            taglist = [tags]
        else:
            taglist = tags
        if self._resourcetags:
            present = True
            for tag in list(taglist):
                if not tag in self._resourcetags:
                    present = False
            return present
        else:
            return False

    def __eq__(self, other):
        return self.resourcename == other.resourcename

    def __repr__(self):
        return 'T{}: {}'.format(self.tiernumber, self.resourcename)

class Tierlist:
    """A list of tier objects for a specified tier_group
    """
    def __init__(self, irods_session, tier_group):
        self.tiers = []
        q = irods_session.query(Resource.name, ResourceMeta).filter(
            Criterion('=', ResourceMeta.name, ATTR_TIERING_GROUP)).filter(
            Criterion('=', ResourceMeta.value, tier_group))
        for row in q:
            self.add(Tier(tier=int(row[ResourceMeta.units]), resourcename=row[Resource.name]))

    def add(self, tier):
        self.tiers.append(tier)

    def get(self, tiernumber):
        """Return a tier from the list  by number
        """
        for tier in self.tiers:
            if tier.tiernumber == tiernumber:
                return tier
        return None

    def byname(self, resourcename):
        for tier in self.tiers:
            if tier.resourcename == resourcename:
                return tier
        return None


    def preferred_list(self, tag=None, notag=[], available_only=False):
        """Returns a list of tiers, ordered by tiernumber

        Args:
            tag: list of tags that should be present to be included
            notag: list of tags that should not be present in order to be included
            available_only: Return only available tiers
        Returns:
            ordered list of tier objects
        """
        if tag:
            stiers = [ t for t in self.tiers if t.hastags(tag) ]
        else:
            stiers = self.tiers
        ctiers = []
        for t in stiers:
            add =True
            for t2 in notag:
                if t.hastags(t2):
                    add = False
            if add:
                ctiers.append(t)
        result = sorted(ctiers, key = lambda x: x.tiernumber)
        if available_only:
            result = [ tier for tier in result if tier.available() ]
        return result

    def tag_present(self, type_query):
        """ Returns true if any of the tiers in the list has all tags in type_query
        """
        tagged_tiers = [ t for t in self.tiers if t.hastags(type_query) ]
        return bool(tagged_tiers)

    def __iter__(self):
        return iter(self.tiers)

class TierState:
    Present = '1'
    Partial = '?'
    Absent = '0'
    Unknown = '.'

class IntTierState:
    T = 'T'
    d = 'd'
    D = 'D'

class iState:
    """Internal state of data in the different tiers
    """
    def __init__(self, tiers, state=None):
        """
        Args:
            state: string representation of data presence per tier, e.g. '110'.
        """
        self._state = {}
        self._tiers = tiers
        for tier in tiers:
            self._state[tier.tiernumber] = set()
        if state:
            for i, s in enumerate(state):
                if s == '1':
                    tier = self._tiers.get(i+1)
                    if tier.hastags(ATTR_RESOURCE_TAR):
                        self.set(tier, IntTierState.T )
                    else:
                        self.set(tier, IntTierState.D)

    def set(self, tier, tierstate):
        self._state[tier.tiernumber].add(tierstate)

    def clear(self, tier, tierstate):
        if tierstate in self._state[tier.tiernumber]:
            self._state[tier.tiernumber].remove(tierstate)
    
    def isset(self, tier, tierstate):
        return tierstate in self._state[tier.tiernumber]

    def ispresent(self, tierstate):
        present = False
        for tier in self._tiers:
            present = present or self.isset(tier, tierstate)
        return present

    def equal_for_tierstate(self, other, tierstate):
        """ Compare states for only a single tierstate (T,t,d)
        """
        equal = True
        for tier in self._tiers:
            equal = equal and (self.isset(tier, tierstate) == other.isset(tier, tierstate))
        return equal

    def tag_present(self, type_query):
        """ Returns true if any of the tiers in the list has all tags in type_query
        """
        for nr in self._state:
            if self._state[nr] and self._tiers.get(nr).hastags(type_query):
                return True
        return False

    def state_repr(self):
        """Return state representation as string.

        Similar to __repr__(), except that for present data there is no 
        separate symbol to distinguish between data in TAR form or data objects
        (i.e. "T" or "D" is always "1", "0" for no data, "?" for partial data)
        """
        tier_symbols = []
        for tier in self._tiers.preferred_list():
            if (self.isset(tier, IntTierState.T) and tier.hastags(ATTR_RESOURCE_TAR)) or \
                self.isset(tier, IntTierState.D):
                tier_symbols.append(TierState.Present)
            elif self.isset(tier, IntTierState.d):
                tier_symbols.append(TierState.Partial)
            else:
                tier_symbols.append(TierState.Absent)
        return ''.join(tier_symbols)

    def __repr__(self):
        result = ''
        sorted_tiers = self._tiers.preferred_list()
        for tier in sorted_tiers:
            if self.isset(tier, IntTierState.D):
                s1 = 'D'
            elif self.isset(tier, IntTierState.d):
                s1 = 'd'
            else:
                s1 = '0'
            if self.isset(tier, IntTierState.T):
                s2 = 'T'
            else:
                s2 = '0'
            result = '{}{}{}{}'.format(result, '-' if result else '', s1, s2)
        return result

    def __eq__(self, other):
        return str(self.__repr__) == str(other.__repr__)
