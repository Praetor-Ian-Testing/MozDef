from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from mozdef_util.elasticsearch_client import ElasticsearchClient as ESClient
from mozdef_util.query_models import SearchQuery, TermMatch

import alerts.geomodel.config as config
import alerts.geomodel.query as query


# TODO: Switch to dataclasses when we move to Python3.7+

def _dict_take(dictionary, keys):
    return {key: dictionary[key] for key in keys}


class Locality(NamedTuple):
    '''Represents a specific locality.
    '''

    sourceipaddress: str
    city: str
    country: str
    lastaction: datetime
    latitude: float
    longitude: float
    radius: int


class State(NamedTuple):
    '''Represents the state tracked for each user regarding their localities.
    '''

    type_: str
    username: str
    localities: List[Locality]

class Entry(NamedTuple):
    '''A top-level container for locality state that will be inserted into
    ElasticSearch.
    The `identifier` field here is the `_id` field of the ES document.  When
    this id is `None`, a new document is inserted whereas when the id is known,
    the existing document is updated.
    '''

    identifier: Optional[str]
    state: State

JournalInterface = Callable[[List[Entry], str], None]

def wrap_journal(client: ESClient) -> JournalInterface:
    '''Wrap an `ElasticsearchClient` in a closure of type `JournalInterface`.
    '''

    def wrapper(entries: List[Entry], esindex: str):
        for entry in entries:
            document = dict(entry.state._asdict())

            client.save_object(
                index=esindex,
                body=document,
                doc_id=entry.identifier)

    return wrapper

class Update(NamedTuple):
    '''Produced by calls to functions operating on lists of `State`s to
    indicate when an update was applied without having to maintain distinct
    lists.
    '''

    state: State
    did_update: bool

def _update(state: State, from_evt: State) -> Update:
    did_update = False

    for loc1 in from_evt.localities:
        did_find = False

        for index, loc2 in enumerate(state.localities):
            # If we find that the new state's locality has been recorded
            # for the user in question, we only want to update it if either
            # their IP changed or the new time of activity is more recent.
            if loc1.city == loc2.city and loc1.country == loc2.country:
                did_find = True

                new_more_recent = loc1.lastaction > loc2.lastaction
                new_ip = loc1.sourceipaddress != loc2.sourceipaddress

                if new_more_recent or new_ip:
                    state.localities[index] = loc1
                    did_update = True

                # Stop looking for the locality in the records pulled from ES.
                break
        
        if not did_find:
            state.localities.append(loc1)
            did_update = True

    return Update(state, did_update)

def find_all(
        query_es: query.QueryInterface,
        locality: config.Localities
) -> List[Entry]:
    '''Retrieve all locality state from ElasticSearch.
    '''

    def to_state(result: Dict[str, Any]) -> Optional[State]:
        try:
            result['localities'] = [
                Locality(**_dict_take(loc, Locality._fields))
                for loc in result['localities']
            ]

            return State(**_dict_take(result, State._fields))
        except TypeError:
            return None
        except KeyError:
            return None

    search = SearchQuery()
    search.add_must([TermMatch('type_', 'locality')])

    results = query_es(search, locality.es_index)

    entries = []
    for result in results:
        state = to_state(result['_source'])

        if state is not None:
            entries.append(Entry(result['_id'], state))

    return entries

def merge(persisted: List[State], event_sourced: List[State]) -> List[Update]:
    '''Merge together a list of states already stored in ElasticSearch
    (obtained via `find_all`) and a list of new states extracted from events.
    This process results in the creation of a new list of states wherein the
    state for each user in either list has had their list of localities updated
    to reflect:

        1. Observations of activity within known localities and
        2. Observations of activity within new localities
    '''

    mapped = {state.username: Update(state, False) for state in persisted}

    for new_state in event_sourced:
        if new_state.username in mapped:
            old_state = mapped[new_state.username].state
            mapped[new_state.username] = _update(old_state, new_state)
        else:
            mapped[new_state.username] = Update(new_state, True)

    return list(mapped.values())

def remove_outdated(
        localities: List[Locality],
        days_valid: int
) -> List[Locality]:
    '''Return a new list of localities with those that are considered
    "outdated" removed.  A `Locality` is considered to be out of date when the
    recorded last activity within that locality was greater than some number of
    days ago.
    '''

    return [
        loc
        for loc in localities
        if loc.lastaction >= datetime.utcnow() - timedelta(days=days_valid)
    ]
