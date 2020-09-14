import logging
from collections import OrderedDict
from enum import Enum
from typing import Optional, List, Set, Tuple, Any

from IPy import IP
from pytz import timezone

from irrd.conf import get_setting, RPKI_IRR_PSEUDO_SOURCE
from irrd.rpki.status import RPKIStatus
from irrd.rpsl.rpsl_objects import (OBJECT_CLASS_MAPPING, lookup_field_names)
from irrd.scopefilter.status import ScopeFilterStatus
from irrd.storage.database_handler import DatabaseHandler, is_serial_synchronised, \
    RPSLDatabaseResponse
from irrd.storage.preload import Preloader
from irrd.storage.queries import RPSLDatabaseQuery, DatabaseStatusQuery
from irrd.utils.validators import parse_as_number

logger = logging.getLogger(__name__)


class InvalidQueryException(ValueError):
    pass


class RouteLookupType(Enum):
    EXACT = 'EXACT'
    LESS_SPECIFIC_ONE_LEVEL = 'LESS_SPECIFIC_ONE_LEVEL'
    LESS_SPECIFIC_WITH_EXACT = 'LESS_SPECIFIC_WITH_EXACT'
    MORE_SPECIFIC_WITHOUT_EXACT = 'MORE_SPECIFIC_WITHOUT_EXACT'


class QueryResolver:
    """
    Resolver for all RPSL queries.

    Some aspects like setting sources retain state, so a single instance
    should not be shared across unrelated query sessions.
    """
    lookup_field_names = lookup_field_names()
    database_handler: DatabaseHandler
    _current_set_root_object_class: Optional[str]

    def __init__(self, client_ip: str, client_str: str, preloader: Preloader,
                 database_handler: DatabaseHandler) -> None:
        self.all_valid_sources = list(get_setting('sources', {}).keys())
        self.sources_default = list(get_setting('sources_default', []))
        self.sources: List[str] = self.sources_default if self.sources_default else self.all_valid_sources
        if get_setting('rpki.roa_source'):
            self.all_valid_sources.append(RPKI_IRR_PSEUDO_SOURCE)
        self.object_class_filter: List[str] = []
        self.rpki_aware = bool(get_setting('rpki.roa_source'))
        self.rpki_invalid_filter_enabled = self.rpki_aware
        self.out_scope_filter_enabled = True
        self.user_agent: Optional[str] = None
        self.client_ip = client_ip
        self.client_str = client_str
        self.preloader = preloader
        self.database_handler = database_handler

    def routes_for_origin(self, origin: str, ip_version: Optional[int]=None) -> Set[str]:
        """
        Resolve all route(6)s prefixes for an origin, returning a set
        of all prefixes. Origin must be in 'ASxxx' format.
        """
        prefixes = self.preloader.routes_for_origins([origin], self.sources, ip_version=ip_version)
        return prefixes

    def routes_for_as_set(self, set_name: str, ip_version: Optional[int]=None) -> Set[str]:
        """
        Find all originating prefixes for all members of an AS-set. May be restricted
        to IPv4 or IPv6. Returns a set of all prefixes.
        """
        self._current_set_root_object_class = 'as-set'
        members = self._recursive_set_resolve({set_name})
        return self.preloader.routes_for_origins(members, self.sources, ip_version=ip_version)

    def members_for_set(self, parameter: str, recursive=False) -> List[str]:
        """
        Find all members of an as-set or route-set, possibly recursively.
        Returns a list of all members, including leaf members.
        """
        self._current_set_root_object_class = None
        if not recursive:
            members, leaf_members = self._find_set_members({parameter})
            members.update(leaf_members)
        else:
            members = self._recursive_set_resolve({parameter})
        if parameter in members:
            members.remove(parameter)

        if get_setting('compatibility.ipv4_only_route_set_members'):
            original_members = set(members)
            for member in original_members:
                try:
                    IP(member)
                except ValueError:
                    continue  # This is not a prefix, ignore.
                try:
                    IP(member, ipversion=4)
                except ValueError:
                    # This was a valid prefix, but not a valid IPv4 prefix,
                    # and should be removed.
                    members.remove(member)

        return sorted(members)

    def _recursive_set_resolve(self, members: Set[str], sets_seen=None) -> Set[str]:
        """
        Resolve all members of a number of sets, recursively.

        For each set in members, determines whether it has been seen already (to prevent
        infinite recursion), ignores it if already seen, and then either adds
        it directly or adds it to a set that requires further resolving.
        """
        if not sets_seen:
            sets_seen = set()

        if all([member in sets_seen for member in members]):
            return set()
        sets_seen.update(members)

        set_members = set()
        resolved_as_members = set()
        sub_members, leaf_members = self._find_set_members(members)

        for sub_member in sub_members:
            if self._current_set_root_object_class is None or self._current_set_root_object_class == 'route-set':
                try:
                    IP(sub_member)
                    set_members.add(sub_member)
                    continue
                except ValueError:
                    pass
            # AS numbers are permitted in route-sets and as-sets, per RFC 2622 5.3.
            # When an AS number is encountered as part of route-set resolving,
            # the prefixes originating from that AS should be added to the response.
            try:
                as_number_formatted, _ = parse_as_number(sub_member)
                if self._current_set_root_object_class == 'route-set':
                    set_members.update(self.preloader.routes_for_origins(
                        [as_number_formatted], self.sources))
                    resolved_as_members.add(sub_member)
                else:
                    set_members.add(sub_member)
                continue
            except ValueError:
                pass

        further_resolving_required = sub_members - set_members - sets_seen - resolved_as_members
        new_members = self._recursive_set_resolve(further_resolving_required, sets_seen)
        set_members.update(new_members)

        return set_members

    def _find_set_members(self, set_names: Set[str]) -> Tuple[Set[str], Set[str]]:
        """
        Find all members of a number of route-sets or as-sets. Includes both
        direct members listed in members attribute, but also
        members included by mbrs-by-ref/member-of.

        Returns a tuple of two sets:
        - members found of the sets included in set_names, both
          references to other sets and direct AS numbers, etc.
        - leaf members that were included in set_names, i.e.
          names for which no further data could be found - for
          example references to non-existent other sets
        """
        members: Set[str] = set()
        sets_already_resolved: Set[str] = set()

        columns = ['parsed_data', 'rpsl_pk', 'source', 'object_class']
        query = self._prepare_query(column_names=columns)

        object_classes = ['as-set', 'route-set']
        # Per RFC 2622 5.3, route-sets can refer to as-sets,
        # but as-sets can only refer to other as-sets.
        if self._current_set_root_object_class == 'as-set':
            object_classes = [self._current_set_root_object_class]

        query = query.object_classes(object_classes).rpsl_pks(set_names)
        query_result = list(self.database_handler.execute_query(query))

        if not query_result:
            # No sub-members are found, and apparantly all inputs were leaf members.
            return set(), set_names

        # Track the object class of the root object set.
        # In one case self._current_set_root_object_class may already be set
        # on the first run: when the set resolving should be fixed to one
        # type of set object.
        if not self._current_set_root_object_class:
            self._current_set_root_object_class = query_result[0]['object_class']

        for result in query_result:
            rpsl_pk = result['rpsl_pk']

            # The same PK may occur in multiple sources, but we are
            # only interested in the first matching object, prioritised
            # according to the source order. This priority is part of the
            # query ORDER BY, so basically we only process an RPSL pk once.
            if rpsl_pk in sets_already_resolved:
                continue
            sets_already_resolved.add(rpsl_pk)

            object_class = result['object_class']
            object_data = result['parsed_data']
            mbrs_by_ref = object_data.get('mbrs-by-ref', None)
            for members_attr in ['members', 'mp-members']:
                if members_attr in object_data:
                    members.update(set(object_data[members_attr]))

            if not rpsl_pk or not object_class or not mbrs_by_ref:
                continue

            # If mbrs-by-ref is set, find any objects with member-of pointing to the route/as-set
            # under query, and include a maintainer listed in mbrs-by-ref, unless mbrs-by-ref
            # is set to ANY.
            query_object_class = ['route', 'route6'] if object_class == 'route-set' else ['aut-num']
            query = self._prepare_query(column_names=columns).object_classes(query_object_class)
            query = query.lookup_attrs_in(['member-of'], [rpsl_pk])

            if 'ANY' not in [m.strip().upper() for m in mbrs_by_ref]:
                query = query.lookup_attrs_in(['mnt-by'], mbrs_by_ref)

            referring_objects = self.database_handler.execute_query(query)

            for result in referring_objects:
                member_object_class = result['object_class']
                members.add(result['parsed_data'][member_object_class])

        leaf_members = set_names - sets_already_resolved
        return members, leaf_members

    def database_status(self, sources: Optional[List[str]]=None) -> 'OrderedDict[str, OrderedDict[str, Any]]':
        """Database status. If sources is None, return all valid sources."""
        if sources is None:
            sources = self.sources_default if self.sources_default else self.all_valid_sources
        invalid_sources = [s for s in sources if s not in self.all_valid_sources]
        query = DatabaseStatusQuery().sources(sources)
        query_results = self.database_handler.execute_query(query)

        results: OrderedDict[str, OrderedDict[str, Any]] = OrderedDict()
        for query_result in query_results:
            source = query_result['source'].upper()
            results[source] = OrderedDict()
            results[source]['authoritative'] = get_setting(f'sources.{source}.authoritative', False)
            object_class_filter = get_setting(f'sources.{source}.object_class_filter')
            results[source]['object_class_filter'] = list(object_class_filter) if object_class_filter else None
            results[source]['rpki_rov_filter'] = bool(get_setting('rpki.roa_source') and not get_setting(f'sources.{source}.rpki_excluded'))
            results[source]['scopefilter_enabled'] = not get_setting(f'sources.{source}.scopefilter_excluded')
            results[source]['local_journal_kept'] = get_setting(f'sources.{source}.keep_journal', False)
            results[source]['serial_oldest_journal'] = query_result['serial_oldest_journal']
            results[source]['serial_newest_journal'] = query_result['serial_newest_journal']
            results[source]['serial_last_export'] = query_result['serial_last_export']
            results[source]['serial_newest_mirror'] = query_result['serial_newest_mirror']
            results[source]['last_update'] = query_result['updated'].astimezone(timezone('UTC')).isoformat()
            results[source]['synchronised_serials'] = is_serial_synchronised(self.database_handler, source)

        for invalid_source in invalid_sources:
            results[invalid_source.upper()] = OrderedDict({'error': 'Unknown source'})
        return results

    def key_lookup(self, object_class: str, rpsl_pk: str) -> RPSLDatabaseResponse:
        """RPSL exact key lookup."""
        query = self._prepare_query().object_classes([object_class]).rpsl_pk(rpsl_pk).first_only()
        return self.database_handler.execute_query(query)

    def route_search(self, address: IP, lookup_type: RouteLookupType):
        """Route(6) object search for an address, supporting exact/less/more specific."""
        query = self._prepare_query(ordered_by_sources=False).object_classes(['route', 'route6'])
        lookup_queries = {
            RouteLookupType.EXACT: query.ip_exact,
            RouteLookupType.LESS_SPECIFIC_ONE_LEVEL: query.ip_less_specific_one_level,
            RouteLookupType.LESS_SPECIFIC_WITH_EXACT: query.ip_less_specific,
            RouteLookupType.MORE_SPECIFIC_WITHOUT_EXACT: query.ip_more_specific,
        }
        query = lookup_queries[lookup_type](address)
        return self.database_handler.execute_query(query)

    def set_query_sources(self, sources: Optional[List[str]]) -> None:
        """Set the sources for future queries. If sources is None, default source list is set."""
        if sources is None:
            sources = self.sources_default if self.sources_default else self.all_valid_sources
        elif not all([source in self.all_valid_sources for source in sources]):
            raise InvalidQueryException('One or more selected sources are unavailable.')
        self.sources = sources

    def set_object_class_filter_next_query(self, object_classes: List[str]) -> None:
        """Restrict object classes for the next query, comma-seperated"""
        self.object_class_filter = object_classes

    def rpsl_object_template(self, object_class) -> str:
        """Return the RPSL template for an object class"""
        try:
            return OBJECT_CLASS_MAPPING[object_class]().generate_template()
        except KeyError:
            raise InvalidQueryException(f'Unknown object class: {object_class}')

    def disable_rpki_filter(self) -> None:
        self.rpki_invalid_filter_enabled = False

    def disable_out_of_scope_filter(self) -> None:
        self.out_scope_filter_enabled = False

    def text_search(self, value: str) -> RPSLDatabaseResponse:
        query = self._prepare_query(ordered_by_sources=False).text_search(value)
        return self.database_handler.execute_query(query)

    def rpsl_attribute_search(self, attribute: str, value: str) -> RPSLDatabaseResponse:
        """
        -i/!o query - inverse search for attribute values
        e.g. `-i mnt-by FOO` finds all objects where (one of the) maintainer(s) is FOO,
        as does `!oFOO`. Restricted to designated lookup fields.
        """
        if attribute not in self.lookup_field_names:
            readable_lookup_field_names = ', '.join(self.lookup_field_names)
            msg = (f'Inverse attribute search not supported for {attribute},' +
                   f'only supported for attributes: {readable_lookup_field_names}')
            raise InvalidQueryException(msg)
        query = self._prepare_query(ordered_by_sources=False).lookup_attr(attribute, value)
        return self.database_handler.execute_query(query)

    def _prepare_query(self, column_names=None, ordered_by_sources=True) -> RPSLDatabaseQuery:
        """Prepare an RPSLDatabaseQuery by applying relevant sources/class filters."""
        query = RPSLDatabaseQuery(column_names, ordered_by_sources)
        if self.sources and self.sources != self.all_valid_sources:
            query.sources(self.sources)
        if self.object_class_filter:
            query.object_classes(self.object_class_filter)
        if self.rpki_invalid_filter_enabled:
            query.rpki_status([RPKIStatus.not_found, RPKIStatus.valid])
        if self.out_scope_filter_enabled:
            query.scopefilter_status([ScopeFilterStatus.in_scope])
        self.object_class_filter = []
        return query
