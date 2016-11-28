from collections import defaultdict, namedtuple
from copy import copy
from datetime import datetime
import json
import itertools
from couchdbkit.exceptions import ResourceConflict, ResourceNotFound
from casexml.apps.phone.exceptions import IncompatibleSyncLogType
from corehq.toggles import LEGACY_SYNC_SUPPORT
from corehq.util.global_request import get_request_domain
from corehq.util.soft_assert import soft_assert
from corehq.toggles import ENABLE_LOADTEST_USERS
from corehq.apps.domain.models import Domain
from dimagi.ext.couchdbkit import *
from django.db import models
from dimagi.utils.decorators.memoized import memoized
from dimagi.utils.mixins import UnicodeMixIn
from dimagi.utils.couch import LooselyEqualDocumentSchema
from dimagi.utils.couch.database import get_db
from casexml.apps.case import const
from casexml.apps.case.sharedmodels import CommCareCaseIndex, IndexHoldingMixIn
from casexml.apps.phone.checksum import Checksum, CaseStateHash
import logging

logger = logging.getLogger('phone.models')


class OTARestoreUser(object):
    """
    This is the OTA restore user's interface that's used for OTA restore to properly
    find cases and generate the user XML for both a web user and mobile user.

    Note: When adding methods to this user, you'll need to ensure that it is
    functional with both a CommCareUser and WebUser.
    """
    def __init__(self, domain, couch_user, loadtest_factor=1):
        self.domain = domain
        self._loadtest_factor = loadtest_factor
        self._couch_user = couch_user

    @property
    def user_id(self):
        return self._couch_user.user_id

    @property
    def loadtest_factor(self):
        """
        Gets the loadtest factor for a domain and user. Is always 1 unless
        both the toggle is enabled for the domain, and the user has a non-zero,
        non-null factor set.
        """
        if ENABLE_LOADTEST_USERS.enabled(self.domain):
            return self._loadtest_factor or 1
        return 1

    @property
    def username(self):
        return self._couch_user.raw_username

    @property
    def password(self):
        return self._couch_user.password

    @property
    def user_session_data(self):
        return self._couch_user.user_session_data

    @property
    def date_joined(self):
        return self._couch_user.date_joined

    @property
    @memoized
    def project(self):
        return Domain.get_by_name(self.domain)

    @property
    def locations(self):
        raise NotImplementedError()

    @property
    def sql_location(self):
        "User's primary SQLLocation"
        return self._couch_user.get_sql_location(self.domain)

    def get_fixture_data_items(self):
        raise NotImplementedError()

    def get_groups(self):
        raise NotImplementedError()

    def get_commtrack_location_id(self):
        raise NotImplementedError()

    def get_owner_ids(self):
        raise NotImplementedError()

    def get_call_center_indicators(self):
        raise NotImplementedError()

    def get_case_sharing_groups(self):
        raise NotImplementedError()

    def get_fixture_last_modified(self):
        raise NotImplementedError()

    def get_ucr_filter_value(self, ucr_filter, ui_filter):
        return ucr_filter.get_filter_value(self._couch_user, ui_filter)

    @memoized
    def get_locations_to_sync(self):
        from corehq.apps.locations.fixtures import get_all_locations_to_sync
        return get_all_locations_to_sync(self)


class OTARestoreWebUser(OTARestoreUser):

    def __init__(self, domain, couch_user, **kwargs):
        from corehq.apps.users.models import WebUser

        assert isinstance(couch_user, WebUser)
        super(OTARestoreWebUser, self).__init__(domain, couch_user, **kwargs)

    @property
    def locations(self):
        return []

    def get_fixture_data_items(self):
        return []

    def get_groups(self):
        return []

    def get_commtrack_location_id(self):
        return None

    def get_owner_ids(self):
        return [self.user_id]

    def get_call_center_indicators(self, config):
        return None

    def get_case_sharing_groups(self):
        return []

    def get_fixture_last_modified(self):
        from corehq.apps.fixtures.models import UserFixtureStatus

        return UserFixtureStatus.DEFAULT_LAST_MODIFIED


class OTARestoreCommCareUser(OTARestoreUser):

    def __init__(self, domain, couch_user, **kwargs):
        from corehq.apps.users.models import CommCareUser

        assert isinstance(couch_user, CommCareUser)
        super(OTARestoreCommCareUser, self).__init__(domain, couch_user, **kwargs)

    @property
    def locations(self):
        return self._couch_user.locations

    def add_to_assigned_locations(self, location):
        return self._couch_user.add_to_assigned_locations(location)

    def set_location(self, location):
        return self._couch_user.set_location(location)

    def get_fixture_data_items(self):
        from corehq.apps.fixtures.models import FixtureDataItem

        return FixtureDataItem.by_user(self._couch_user)

    def get_groups(self):
        # this call is only used by bihar custom code and can be removed when that project is inactive
        from corehq.apps.groups.models import Group
        return Group.by_user(self._couch_user)

    def get_commtrack_location_id(self):
        from corehq.apps.commtrack.util import get_commtrack_location_id

        return get_commtrack_location_id(self._couch_user, self.project)

    def get_owner_ids(self):
        return self._couch_user.get_owner_ids(self.domain)

    def get_call_center_indicators(self, config):
        from corehq.apps.callcenter.indicator_sets import CallCenterIndicators

        return CallCenterIndicators(
            self.project.name,
            self.project.default_timezone,
            self.project.call_center_config.case_type,
            self._couch_user,
            indicator_config=config
        )

    def get_case_sharing_groups(self):
        return self._couch_user.get_case_sharing_groups()

    def get_fixture_last_modified(self):
        from corehq.apps.fixtures.models import UserFixtureType

        return self._couch_user.fixture_status(UserFixtureType.LOCATION)

    @memoized
    def get_locations_to_sync(self):
        from corehq.apps.locations.fixtures import get_all_locations_to_sync
        return get_all_locations_to_sync(self)


class CaseState(LooselyEqualDocumentSchema, IndexHoldingMixIn):
    """
    Represents the state of a case on a phone.
    """

    case_id = StringProperty()
    type = StringProperty()
    indices = SchemaListProperty(CommCareCaseIndex)

    @classmethod
    def from_case(cls, case):
        if isinstance(case, dict):
            return cls.wrap({
                'case_id': case['_id'],
                'type': case['type'],
                'indices': case['indices'],
            })

        return cls(
            case_id=case.case_id,
            type=case.type,
            indices=case.indices,
        )

    def __repr__(self):
        return "case state: %s (%s)" % (self.case_id, self.indices)


class SyncLogAssertionError(AssertionError):

    def __init__(self, case_id, *args, **kwargs):
        self.case_id = case_id
        super(SyncLogAssertionError, self).__init__(*args, **kwargs)


LOG_FORMAT_LEGACY = 'legacy'
LOG_FORMAT_SIMPLIFIED = 'simplified'


class AbstractSyncLog(SafeSaveDocument, UnicodeMixIn):
    date = DateTimeProperty()
    domain = StringProperty()  # this is only added as of 11/2016 - not guaranteed to be set
    user_id = StringProperty()
    build_id = StringProperty()  # this is only added as of 11/2016 and only works with app-aware sync

    previous_log_id = StringProperty()  # previous sync log, forming a chain
    duration = IntegerProperty()  # in seconds
    log_format = StringProperty()

    # owner_ids_on_phone stores the ids the phone thinks it's the owner of.
    # This typically includes the user id,
    # as well as all groups that that user is a member of.
    owner_ids_on_phone = StringListProperty()

    # for debugging / logging
    previous_log_rev = StringProperty()  # rev of the previous log at the time of creation
    last_submitted = DateTimeProperty()  # last time a submission caused this to be modified
    rev_before_last_submitted = StringProperty()  # rev when the last submission was saved
    last_cached = DateTimeProperty()  # last time this generated a cached response
    hash_at_last_cached = StringProperty()  # the state hash of this when it was last cached

    # save state errors and hashes here
    had_state_error = BooleanProperty(default=False)
    error_date = DateTimeProperty()
    error_hash = StringProperty()

    strict = True  # for asserts

    @classmethod
    def get(cls, doc_id):
        doc = get_sync_log_doc(doc_id)
        return cls.wrap(doc)

    def _assert(self, conditional, msg="", case_id=None):
        if not conditional:
            logger.warn("assertion failed: %s" % msg)
            if self.strict:
                raise SyncLogAssertionError(case_id, msg)
            else:
                self.has_assert_errors = True

    @classmethod
    def wrap(cls, data):
        ret = super(AbstractSyncLog, cls).wrap(data)
        if hasattr(ret, 'has_assert_errors'):
            ret.strict = False
        return ret

    def case_count(self):
        """
        How many cases are associated with this. Used in reports.
        """
        raise NotImplementedError()

    def phone_is_holding_case(self, case_id):
        raise NotImplementedError()

    def get_footprint_of_cases_on_phone(self):
        """
        Gets the phone's flat list of all case ids on the phone,
        owned or not owned but relevant.
        """
        raise NotImplementedError()

    def get_state_hash(self):
        return CaseStateHash(Checksum(self.get_footprint_of_cases_on_phone()).hexdigest())

    def update_phone_lists(self, xform, case_list):
        """
        Given a form an list of touched cases, update this sync log to reflect the updated
        state on the phone.
        """
        raise NotImplementedError()

    def get_payload_attachment_name(self, version):
        return 'restore_payload_{version}.xml'.format(version=version)

    def has_cached_payload(self, version):
        return self.get_payload_attachment_name(version) in self._doc.get('_attachments', {})

    def get_cached_payload(self, version, stream=False):
        try:
            return self.fetch_attachment(self.get_payload_attachment_name(version), stream=stream)
        except ResourceNotFound:
            return None

    def set_cached_payload(self, payload, version):
        self.put_attachment(payload, name=self.get_payload_attachment_name(version),
                            content_type='text/xml')

    def invalidate_cached_payloads(self):
        for name in copy(self._doc.get('_attachments', {})):
            self.delete_attachment(name)

    @classmethod
    def from_other_format(cls, other_sync_log):
        """
        Convert to an instance of a subclass from another subclass. Subclasses can
        override this to provide conversion functions.
        """
        raise IncompatibleSyncLogType('Unable to convert from {} to {}'.format(
            type(other_sync_log), cls,
        ))

    # anything prefixed with 'tests_only' is only used in tests
    def tests_only_get_cases_on_phone(self):
        raise NotImplementedError()

    def test_only_clear_cases_on_phone(self):
        raise NotImplementedError()

    def test_only_get_dependent_cases_on_phone(self):
        raise NotImplementedError()


class SyncLog(AbstractSyncLog):
    """
    A log of a single sync operation.
    """
    log_format = StringProperty(default=LOG_FORMAT_LEGACY)
    last_seq = StringProperty()  # the last_seq of couch during this sync

    # we need to store a mapping of cases to indices for generating the footprint
    # cases_on_phone represents the state of all cases the server
    # thinks the phone has on it and cares about.
    cases_on_phone = SchemaListProperty(CaseState)

    # dependant_cases_on_phone represents the possible list of cases
    # also on the phone because they are referenced by a real case's index
    # (or a dependent case's index).
    # This list is not necessarily a perfect reflection
    # of what's on the phone, but is guaranteed to be after pruning
    dependent_cases_on_phone = SchemaListProperty(CaseState)

    @classmethod
    def wrap(cls, data):
        # last_seq used to be int, but is now string for cloudant compatibility
        if isinstance(data.get('last_seq'), (int, long)):
            data['last_seq'] = unicode(data['last_seq'])
        return super(SyncLog, cls).wrap(data)

    @classmethod
    def last_for_user(cls, user_id):
        from casexml.apps.phone.dbaccessors.sync_logs_by_user import get_last_synclog_for_user

        return get_last_synclog_for_user(user_id)

    def case_count(self):
        return len(self.cases_on_phone)

    def get_previous_log(self):
        """
        Get the previous sync log, if there was one.  Otherwise returns nothing.
        """
        if not hasattr(self, "_previous_log_ref"):
            self._previous_log_ref = SyncLog.get(self.previous_log_id) if self.previous_log_id else None
        return self._previous_log_ref

    def phone_has_case(self, case_id):
        """
        Whether the phone currently has a case, according to this sync log
        """
        return self.get_case_state(case_id) is not None

    def get_case_state(self, case_id):
        """
        Get the case state object associated with an id, or None if no such
        object is found
        """
        filtered_list = self._case_state_map()[case_id]
        if filtered_list:
            self._assert(len(filtered_list) == 1,
                         "Should be exactly 0 or 1 cases on phone but were %s for %s" %
                         (len(filtered_list), case_id))
            return CaseState.wrap(filtered_list[0])
        return None

    def phone_has_dependent_case(self, case_id):
        """
        Whether the phone currently has a dependent case, according to this sync log
        """
        return self.get_dependent_case_state(case_id) is not None

    def get_dependent_case_state(self, case_id):
        """
        Get the dependent case state object associated with an id, or None if no such
        object is found
        """
        filtered_list = self._dependent_case_state_map()[case_id]
        if filtered_list:
            self._assert(len(filtered_list) == 1,
                         "Should be exactly 0 or 1 dependent cases on phone but were %s for %s" %
                         (len(filtered_list), case_id))
            return CaseState.wrap(filtered_list[0])
        return None

    @memoized
    def _dependent_case_state_map(self):
        return self._build_state_map('dependent_cases_on_phone')

    @memoized
    def _case_state_map(self):
        return self._build_state_map('cases_on_phone')

    def _build_state_map(self, list_name):
        state_map = defaultdict(list)
        # referencing the property via self._doc is because we don't want to needlessly call wrap
        # (which couchdbkit does not make any effort to cache on repeated calls)
        # deterministically this change shaved off 10 seconds from an ota restore
        # of about 300 cases.
        for case in self._doc[list_name]:
            state_map[case['case_id']].append(case)

        return state_map

    def _get_case_state_from_anywhere(self, case_id):
        return self.get_case_state(case_id) or self.get_dependent_case_state(case_id)

    def archive_case(self, case_id):
        state = self.get_case_state(case_id)
        if state:
            self.cases_on_phone.remove(state)
            self._case_state_map.reset_cache(self)
            all_indices = [i for case_state in self.cases_on_phone + self.dependent_cases_on_phone
                           for i in case_state.indices]
            if any([i.referenced_id == case_id for i in all_indices]):
                self.dependent_cases_on_phone.append(state)
                self._dependent_case_state_map.reset_cache(self)
            return state
        else:
            state = self.get_dependent_case_state(case_id)
            if state:
                all_indices = [i for case_state in self.cases_on_phone + self.dependent_cases_on_phone
                               for i in case_state.indices]
                if not any([i.referenced_id == case_id for i in all_indices]):
                    self.dependent_cases_on_phone.remove(state)
                    self._dependent_case_state_map.reset_cache(self)
                    return state

    def _phone_owns(self, action):
        # whether the phone thinks it owns an action block.
        # the only way this can't be true is if the block assigns to an
        # owner id that's not associated with the user on the phone
        owner = action.updated_known_properties.get("owner_id")
        if owner:
            return owner in self.owner_ids_on_phone
        return True

    def update_phone_lists(self, xform, case_list):
        # for all the cases update the relevant lists in the sync log
        # so that we can build a historical record of what's associated
        # with the phone
        removed_states = {}
        new_indices = set()
        for case in case_list:
            actions = case.get_actions_for_form(xform)
            for action in actions:
                logger.debug('OLD {}: {}'.format(case.case_id, action.action_type))
                if action.action_type == const.CASE_ACTION_CREATE:
                    self._assert(not self.phone_has_case(case.case_id),
                                 'phone has case being created: %s' % case.case_id)
                    starter_state = CaseState(case_id=case.case_id, indices=[])
                    if self._phone_owns(action):
                        self.cases_on_phone.append(starter_state)
                        self._case_state_map.reset_cache(self)
                    else:
                        removed_states[case.case_id] = starter_state
                elif action.action_type == const.CASE_ACTION_UPDATE:
                    if not self._phone_owns(action):
                        # only action necessary here is in the case of
                        # reassignment to an owner the phone doesn't own
                        state = self.archive_case(case.case_id)
                        if state:
                            removed_states[case.case_id] = state
                elif action.action_type == const.CASE_ACTION_INDEX:
                    # in the case of parallel reassignment and index update
                    # the phone might not have the case
                    if self.phone_has_case(case.case_id):
                        case_state = self.get_case_state(case.case_id)
                    else:
                        case_state = self.get_dependent_case_state(case.case_id)
                    # reconcile indices
                    if case_state:
                        for index in action.indices:
                            new_indices.add(index.referenced_id)
                        case_state.update_indices(action.indices)

                elif action.action_type == const.CASE_ACTION_CLOSE:
                    if self.phone_has_case(case.case_id):
                        state = self.archive_case(case.case_id)
                        if state:
                            removed_states[case.case_id] = state

        # if we just removed a state and added an index to it
        # we have to put it back in our dependent case list
        readded_any = False
        for index in new_indices:
            if index in removed_states:
                self.dependent_cases_on_phone.append(removed_states[index])
                readded_any = True

        if readded_any:
            self._dependent_case_state_map.reset_cache(self)

        if case_list:
            try:
                self.save()
                self.invalidate_cached_payloads()
            except ResourceConflict:
                logging.exception('doc update conflict saving sync log {id}'.format(
                    id=self._id,
                ))
                raise

    def get_footprint_of_cases_on_phone(self):
        def children(case_state):
            return [self._get_case_state_from_anywhere(index.referenced_id)
                    for index in case_state.indices]

        relevant_cases = set()
        queue = list(self.cases_on_phone)
        while queue:
            case_state = queue.pop()
            # I don't actually understand why something is coming back None
            # here, but we can probably just ignore it.
            if case_state is not None and case_state.case_id not in relevant_cases:
                relevant_cases.add(case_state.case_id)
                queue.extend(children(case_state))
        return relevant_cases

    def phone_is_holding_case(self, case_id):
        """
        Whether the phone is holding (not purging) a case.
        """
        # this is inefficient and could be optimized
        if self.phone_has_case(case_id):
            return True
        else:
            cs = self.get_dependent_case_state(case_id)
            if cs and case_id in self.get_footprint_of_cases_on_phone():
                return True
            return False

    def __unicode__(self):
        return "%s synced on %s (%s)" % (self.user_id, self.date.date(), self.get_id)

    def tests_only_get_cases_on_phone(self):
        return self.cases_on_phone

    def test_only_clear_cases_on_phone(self):
        self.cases_on_phone = []

    def test_only_get_dependent_cases_on_phone(self):
        return self.dependent_cases_on_phone


class IndexTree(DocumentSchema):
    """
    Document type representing a case dependency tree (which is flattened to a single dict)
    """
    # a flat mapping of cases to dicts of their indices. The keys in each dict are the index identifiers
    # and the values are the referenced case IDs
    indices = SchemaDictProperty()

    def __repr__(self):
        return json.dumps(self.indices, indent=2)

    @staticmethod
    def get_all_dependencies(case_id, child_index_tree, extension_index_tree, closed_cases=None,
                             cached_child_map=None, cached_extension_map=None):
        """Takes a child and extension index tree and returns returns a set of all dependencies of <case_id>

        Traverse each incoming index, return each touched case. Stop traversing
        incoming extensions if they lead to closed cases.
        Traverse each outgoing index in the extension tree, return each touched case
        """
        if closed_cases is None:
            closed_cases = set()

        def _recursive_call(case_id, all_cases, cached_child_map, cached_extension_map):
            all_cases.add(case_id)
            incoming_extension_indices = extension_index_tree.get_cases_that_directly_depend_on_case(
                case_id,
                cached_map=cached_extension_map
            )
            open_incoming_extension_indices = {
                case for case in incoming_extension_indices if case not in closed_cases
            }
            all_incoming_indices = itertools.chain(
                child_index_tree.get_cases_that_directly_depend_on_case(case_id, cached_map=cached_child_map),
                open_incoming_extension_indices,
            )

            for dependent_case in all_incoming_indices:
                # incoming indices
                if dependent_case not in all_cases:
                    all_cases.add(dependent_case)
                    _recursive_call(dependent_case, all_cases, cached_child_map, cached_extension_map)
            for indexed_case in extension_index_tree.indices.get(case_id, {}).values():
                # outgoing extension indices
                if indexed_case not in all_cases:
                    all_cases.add(indexed_case)
                    _recursive_call(indexed_case, all_cases, cached_child_map, cached_extension_map)

        all_cases = set()
        cached_child_map = cached_child_map or _reverse_index_map(child_index_tree.indices)
        cached_extension_map = cached_extension_map or _reverse_index_map(extension_index_tree.indices)
        _recursive_call(case_id, all_cases, cached_child_map, cached_extension_map)
        return all_cases

    @staticmethod
    def get_all_outgoing_cases(case_id, child_index_tree, extension_index_tree):
        """traverse all outgoing child and extension indices"""
        all_cases = set([case_id])
        new_cases = set([case_id])
        while new_cases:
            case_to_check = new_cases.pop()
            parent_cases = set(child_index_tree.indices.get(case_to_check, {}).values())
            host_cases = set(extension_index_tree.indices.get(case_to_check, {}).values())
            new_cases = (new_cases | parent_cases | host_cases) - all_cases
            all_cases = all_cases | parent_cases | host_cases
        return all_cases

    @staticmethod
    def traverse_incoming_extensions(case_id, extension_index_tree, closed_cases, cached_map=None):
        """traverse open incoming extensions"""
        all_cases = set([case_id])
        new_cases = set([case_id])
        cached_map = cached_map or _reverse_index_map(extension_index_tree.indices)
        while new_cases:
            case_to_check = new_cases.pop()
            open_incoming_extension_indices = {
                case for case in
                extension_index_tree.get_cases_that_directly_depend_on_case(case_to_check,
                                                                            cached_map=cached_map)
                if case not in closed_cases
            }
            for incoming_case in open_incoming_extension_indices:
                new_cases.add(incoming_case)
                all_cases.add(incoming_case)
        return all_cases

    def get_cases_that_directly_depend_on_case(self, case_id, cached_map=None):
        cached_map = cached_map or _reverse_index_map(self.indices)
        return cached_map.get(case_id, [])

    def get_all_cases_that_depend_on_case(self, case_id, cached_map=None):
        """
        Recursively builds a tree of all cases that depend on this case and returns
        a flat set of case ids.

        Allows passing in a cached map of reverse index references if you know you are going
        to call it more than once in a row to avoid rebuilding that.
        """

        def _recursive_call(case_id, all_cases, cached_map):
            all_cases.add(case_id)
            for dependent_case in self.get_cases_that_directly_depend_on_case(case_id, cached_map=cached_map):
                if dependent_case not in all_cases:
                    all_cases.add(dependent_case)
                    _recursive_call(dependent_case, all_cases, cached_map)

        all_cases = set()
        cached_map = cached_map or _reverse_index_map(self.indices)
        _recursive_call(case_id, all_cases, cached_map)
        return all_cases

    def delete_index(self, from_case_id, index_name):
        prior_ids = self.indices.pop(from_case_id, {})
        prior_ids.pop(index_name, None)
        if prior_ids:
            self.indices[from_case_id] = prior_ids

    def set_index(self, from_case_id, index_name, to_case_id):
        prior_ids = self.indices.get(from_case_id, {})
        prior_ids[index_name] = to_case_id
        self.indices[from_case_id] = prior_ids

    def apply_updates(self, other_tree):
        """
        Apply updates from another IndexTree and return a copy with those applied.

        If an id is found in the new one, use that id's indices, otherwise, use this ones,
        (defaulting to nothing).
        """
        assert isinstance(other_tree, IndexTree)
        new = IndexTree(
            indices=copy(self.indices),
        )
        new.indices.update(other_tree.indices)
        return new


def _reverse_index_map(index_map):
    reverse_indices = defaultdict(set)
    for case_id, indices in index_map.items():
        for indexed_case_id in indices.values():
            reverse_indices[indexed_case_id].add(case_id)
    return dict(reverse_indices)


class SimplifiedSyncLog(AbstractSyncLog):
    """
    New, simplified sync log class that is used by ownership cleanliness restore.

    Just maintains a flat list of case IDs on the phone rather than the case/dependent state
    lists from the SyncLog class.
    """
    log_format = StringProperty(default=LOG_FORMAT_SIMPLIFIED)
    case_ids_on_phone = SetProperty(unicode)
    # this is a subset of case_ids_on_phone used to flag that a case is only around because it has dependencies
    # this allows us to purge it if possible from other actions
    dependent_case_ids_on_phone = SetProperty(unicode)
    owner_ids_on_phone = SetProperty(unicode)
    index_tree = SchemaProperty(IndexTree)  # index tree of subcases / children
    extension_index_tree = SchemaProperty(IndexTree)  # index tree of extensions
    closed_cases = SetProperty(unicode)
    extensions_checked = BooleanProperty(default=False)

    def save(self, *args, **kwargs):
        # force doc type to SyncLog to avoid changing the couch view.
        self.doc_type = "SyncLog"
        super(SimplifiedSyncLog, self).save(*args, **kwargs)

    def case_count(self):
        return len(self.case_ids_on_phone)

    def phone_is_holding_case(self, case_id):
        """
        Whether the phone currently has a case, according to this sync log
        """
        return case_id in self.case_ids_on_phone

    def get_footprint_of_cases_on_phone(self):
        return list(self.case_ids_on_phone)

    @property
    def primary_case_ids(self):
        return self.case_ids_on_phone - self.dependent_case_ids_on_phone

    def purge(self, case_id):
        """
        This happens in 3 phases, and recursively tries to purge outgoing indices of purged cases.
        Definitions:
        -----------
        A case is *relevant* if:
        - it is open and owned or,
        - it has a relevant child or,
        - it has a relevant extension or,
        - it is the extension of a relevant case.

        A case is *available* if:
        - it is open and not an extension case or,
        - it is open and is the extension of an available case.

        A case is *live* if:
        - it is owned and available or,
        - it has a live child or,
        - it has a live extension or,
        - it is the exension of a live case.

        Algorithm:
        ----------
        1. Mark *relevant* cases
            Mark all open cases owned by the user relevant. Traversing all outgoing child
            and extension indexes, as well as all incoming extension indexes, mark all
            touched cases relevant.

        2. Mark *available* cases
            Mark all relevant cases that are open and have no outgoing extension indexes
            as available. Traverse incoming extension indexes which don't lead to closed
            cases, mark all touched cases as available.

        3. Mark *live* cases
            Mark all relevant, owned, available cases as live. Traverse incoming
            extension indexes which don't lead to closed cases, mark all touched
            cases as live.
        """
        logger.debug("purging: {}".format(case_id))
        self.dependent_case_ids_on_phone.add(case_id)
        relevant = self._get_relevant_cases(case_id)
        available = self._get_available_cases(relevant)
        live = self._get_live_cases(available)
        to_remove = relevant - live
        self._remove_cases_purge_indices(to_remove, case_id)

    def _get_relevant_cases(self, case_id):
        """
        Mark all open cases owned by the user relevant. Traversing all outgoing child
        and extension indexes, as well as all incoming extension indexes,
        mark all touched cases relevant.
        """
        relevant = IndexTree.get_all_dependencies(
            case_id,
            closed_cases=self.closed_cases,
            child_index_tree=self.index_tree,
            extension_index_tree=self.extension_index_tree,
            cached_child_map=_reverse_index_map(self.index_tree.indices),
            cached_extension_map=_reverse_index_map(self.extension_index_tree.indices),
        )
        logger.debug("Relevant cases: {}".format(relevant))

        return relevant

    def _get_available_cases(self, relevant):
        """
        Mark all relevant cases that are open and have no outgoing extension indexes
        as available. Traverse incoming extension indexes which don't lead to closed
        cases, mark all touched cases as available
        """
        incoming_extensions = _reverse_index_map(self.extension_index_tree.indices)
        available = {case for case in relevant
                     if case not in self.closed_cases
                     and (not self.extension_index_tree.indices.get(case) or self.index_tree.indices.get(case))}
        new_available = set() | available
        while new_available:
            case_to_check = new_available.pop()
            for incoming_extension in incoming_extensions.get(case_to_check, []):
                if incoming_extension not in self.closed_cases:
                    new_available.add(incoming_extension)
            available = available | new_available
        logger.debug("Available cases: {}".format(available))

        return available

    def _get_live_cases(self, available):
        """
        Mark all relevant, owned, available cases as live. Traverse incoming
        extension indexes which don't lead to closed cases, mark all touched
        cases as available.
        """
        incoming_extensions = _reverse_index_map(self.extension_index_tree.indices)
        live = {case for case in available if case in self.primary_case_ids}
        new_live = set() | live
        checked = set()
        while new_live:
            case_to_check = new_live.pop()
            checked.add(case_to_check)
            new_live = new_live | IndexTree.get_all_outgoing_cases(
                case_to_check,
                self.index_tree,
                self.extension_index_tree
            )
            new_live = new_live | IndexTree.traverse_incoming_extensions(
                case_to_check,
                self.extension_index_tree,
                self.closed_cases, cached_map=incoming_extensions
            )
            new_live = new_live - checked
            live = live | new_live

        logger.debug("live cases: {}".format(live))

        return live

    def _remove_cases_purge_indices(self, all_to_remove, checked_case_id):
        """Remove all cases marked for removal. Traverse child cases and try to purge those too."""
        logger.debug("cases to to_remove: {}".format(all_to_remove))
        for to_remove in all_to_remove:
            indices = self.index_tree.indices.get(to_remove, {})
            self._remove_case(to_remove, all_to_remove, checked_case_id)
            for referenced_case in indices.values():
                is_dependent_case = referenced_case in self.dependent_case_ids_on_phone
                already_primed_for_removal = referenced_case in all_to_remove
                if is_dependent_case and not already_primed_for_removal and referenced_case != checked_case_id:
                    self.purge(referenced_case)

    def _remove_case(self, to_remove, all_to_remove, checked_case_id):
        """Removes case from index trees, case_ids_on_phone and dependent_case_ids_on_phone if pertinent"""
        logger.debug('removing: {}'.format(to_remove))

        deleted_indices = self.index_tree.indices.pop(to_remove, {})
        deleted_indices.update(self.extension_index_tree.indices.pop(to_remove, {}))

        self._validate_case_removal(to_remove, all_to_remove, deleted_indices, checked_case_id)

        try:
            self.case_ids_on_phone.remove(to_remove)
        except KeyError:
            _assert = soft_assert(to=['czue' + '@' + 'dimagi.com'], exponential_backoff=False)
            should_fail_softly = _domain_has_legacy_toggle_set()
            if should_fail_softly:
                pass
            else:
                # this is only a soft assert for now because of http://manage.dimagi.com/default.asp?181443
                # we should convert back to a real Exception when we stop getting any of these
                _assert(False, 'case {} already removed from sync log {}'.format(to_remove, self._id))

        if to_remove in self.dependent_case_ids_on_phone:
            self.dependent_case_ids_on_phone.remove(to_remove)

    def _validate_case_removal(self, case_to_remove, all_to_remove, deleted_indices, checked_case_id):
        """Traverse immediate outgoing indices. Validate that these are also candidates for removal."""
        if case_to_remove == checked_case_id:
            return

        for index in deleted_indices.values():
            if not _domain_has_legacy_toggle_set():
                # unblocking http://manage.dimagi.com/default.asp?185850#1039475
                _assert = soft_assert(to=['czue' + '@' + 'dimagi.com'], exponential_backoff=True,
                                      fail_if_debug=True)
                _assert(index in (all_to_remove | set([checked_case_id])),
                        "expected {} in {} but wasn't".format(index, all_to_remove))

    def _add_primary_case(self, case_id):
        self.case_ids_on_phone.add(case_id)
        if case_id in self.dependent_case_ids_on_phone:
            self.dependent_case_ids_on_phone.remove(case_id)

    def _add_index(self, index, case_update):
        logger.debug('adding index {} --<{}>--> {} ({}).'.format(
            index.case_id, index.relationship, index.referenced_id, index.identifier))
        if index.relationship == const.CASE_INDEX_EXTENSION:
            self._add_extension_index(index, case_update)
        else:
            self._add_child_index(index)

    def _add_extension_index(self, index, case_update):
        assert index.relationship == const.CASE_INDEX_EXTENSION
        self.extension_index_tree.set_index(index.case_id, index.identifier, index.referenced_id)

        if index.referenced_id not in self.case_ids_on_phone:
            self.case_ids_on_phone.add(index.referenced_id)
            self.dependent_case_ids_on_phone.add(index.referenced_id)

        case_child_indices = [idx for idx in case_update.indices_to_add
                              if idx.relationship == const.CASE_INDEX_CHILD
                              and idx.referenced_id == index.referenced_id]
        if not case_child_indices and not case_update.is_live:
            # this case doesn't also have child indices, and it is not owned, so it is dependent
            self.dependent_case_ids_on_phone.add(index.case_id)

    def _add_child_index(self, index):
        assert index.relationship == const.CASE_INDEX_CHILD
        self.index_tree.set_index(index.case_id, index.identifier, index.referenced_id)
        if index.referenced_id not in self.case_ids_on_phone:
            self.case_ids_on_phone.add(index.referenced_id)
            self.dependent_case_ids_on_phone.add(index.referenced_id)

    def _delete_index(self, index):
        self.index_tree.delete_index(index.case_id, index.identifier)
        self.extension_index_tree.delete_index(index.case_id, index.identifier)

    def update_phone_lists(self, xform, case_list):
        made_changes = False
        logger.debug('updating sync log for {}'.format(self.user_id))
        logger.debug('case ids before update: {}'.format(', '.join(self.case_ids_on_phone)))
        logger.debug('dependent case ids before update: {}'.format(', '.join(self.dependent_case_ids_on_phone)))
        logger.debug('index tree before update: {}'.format(self.index_tree))

        class CaseUpdate(object):

            def __init__(self, case_id, owner_ids_on_phone):
                self.case_id = case_id
                self.owner_ids_on_phone = owner_ids_on_phone
                self.was_live_previously = True
                self.final_owner_id = None
                self.is_closed = None
                self.indices_to_add = []
                self.indices_to_delete = []

            @property
            def extension_indices_to_add(self):
                return [index for index in self.indices_to_add
                        if index.relationship == const.CASE_INDEX_EXTENSION]

            def has_extension_indices_to_add(self):
                return len(self.extension_indices_to_add) > 0

            @property
            def is_live(self):
                """returns whether an update is live for a specifc set of owner_ids"""
                if self.is_closed:
                    return False
                elif self.final_owner_id is None:
                    # we likely didn't touch owner_id so just default to whatever it was previously
                    return self.was_live_previously
                else:
                    return self.final_owner_id in self.owner_ids_on_phone

        ShortIndex = namedtuple('ShortIndex', ['case_id', 'identifier', 'referenced_id', 'relationship'])

        # this is a variable used via closures in the function below
        owner_id_map = {}

        def get_latest_owner_id(case_id, action=None):
            # "latest" just means as this forms actions are played through
            if action is not None:
                owner_id_from_action = action.updated_known_properties.get("owner_id")
                if owner_id_from_action is not None:
                    owner_id_map[case_id] = owner_id_from_action
            return owner_id_map.get(case_id, None)

        all_updates = {}
        for case in case_list:
            if case.case_id not in all_updates:
                logger.debug('initializing update for case {}'.format(case.case_id))
                all_updates[case.case_id] = CaseUpdate(case_id=case.case_id,
                                                   owner_ids_on_phone=self.owner_ids_on_phone)

            case_update = all_updates[case.case_id]
            case_update.was_live_previously = case.case_id in self.primary_case_ids
            actions = case.get_actions_for_form(xform)
            for action in actions:
                logger.debug('{}: {}'.format(case.case_id, action.action_type))
                owner_id = get_latest_owner_id(case.case_id, action)
                if owner_id is not None:
                    case_update.final_owner_id = owner_id
                if action.action_type == const.CASE_ACTION_INDEX:
                    for index in action.indices:
                        if index.referenced_id:
                            case_update.indices_to_add.append(
                                ShortIndex(case.case_id, index.identifier, index.referenced_id, index.relationship)
                            )
                        else:
                            case_update.indices_to_delete.append(
                                ShortIndex(case.case_id, index.identifier, None, None)
                            )
                elif action.action_type == const.CASE_ACTION_CLOSE:
                    case_update.is_closed = True

        non_live_updates = []
        for case in case_list:
            case_update = all_updates[case.case_id]
            if case_update.is_live:
                logger.debug('case {} is live.'.format(case_update.case_id))
                if case.case_id not in self.case_ids_on_phone:
                    self._add_primary_case(case.case_id)
                    made_changes = True
                elif case.case_id in self.dependent_case_ids_on_phone:
                    self.dependent_case_ids_on_phone.remove(case.case_id)
                    made_changes = True

                for index in case_update.indices_to_add:
                    self._add_index(index, case_update)
                    made_changes = True
                for index in case_update.indices_to_delete:
                    self._delete_index(index)
                    made_changes = True
            else:
                # process the non-live updates after all live are already processed
                non_live_updates.append(case_update)
                # populate the closed cases list before processing non-live updates
                if case_update.is_closed:
                    self.closed_cases.add(case_update.case_id)

        for update in non_live_updates:
            logger.debug('case {} is NOT live.'.format(update.case_id))
            if update.has_extension_indices_to_add():
                # non-live cases with extension indices should be added and processed
                self.case_ids_on_phone.add(update.case_id)
                for index in update.indices_to_add:
                    self._add_index(index, update)
                    made_changes = True

        for update in non_live_updates:
            if update.case_id in self.case_ids_on_phone:
                # try purging the case
                self.purge(update.case_id)
                if update.case_id in self.case_ids_on_phone:
                    # if unsuccessful, process the rest of the update
                    for index in update.indices_to_add:
                        self._add_index(index, update)
                    for index in update.indices_to_delete:
                        self._delete_index(index)
                made_changes = True

        logger.debug('case ids after update: {}'.format(', '.join(self.case_ids_on_phone)))
        logger.debug('dependent case ids after update: {}'.format(', '.join(self.dependent_case_ids_on_phone)))
        logger.debug('index tree after update: {}'.format(self.index_tree))
        logger.debug('extension index tree after update: {}'.format(self.extension_index_tree))
        if made_changes or case_list:
            try:
                if made_changes:
                    logger.debug('made changes, saving.')
                    self.last_submitted = datetime.utcnow()
                    self.rev_before_last_submitted = self._rev
                    self.save()
                    if case_list:
                        try:
                            self.invalidate_cached_payloads()
                        except ResourceConflict:
                            # this operation is harmless so just blindly retry and don't
                            # reraise if it goes through the second time
                            SimplifiedSyncLog.get(self._id).invalidate_cached_payloads()
            except ResourceConflict:
                logging.exception('doc update conflict saving sync log {id}'.format(
                    id=self._id,
                ))
                raise

    def purge_dependent_cases(self):
        """
        Attempt to purge any dependent cases from the sync log.
        """
        # this is done when migrating from old formats or during initial sync
        # to purge non-relevant dependencies
        for dependent_case_id in list(self.dependent_case_ids_on_phone):
            # need this additional check since the case might have already been purged/remove
            # as a result of purging the child case
            if dependent_case_id in self.dependent_case_ids_on_phone:
                # this will be a no-op if the case cannot be purged due to dependencies
                self.purge(dependent_case_id)

    @classmethod
    def from_other_format(cls, other_sync_log):
        """
        Migrate from the old SyncLog format to this one.
        """
        if isinstance(other_sync_log, SyncLog):
            previous_log_footprint = set(other_sync_log.get_footprint_of_cases_on_phone())

            def _add_state_contributions(new_sync_log, case_state, is_dependent=False):
                if case_state.case_id in previous_log_footprint:
                    new_sync_log.case_ids_on_phone.add(case_state.case_id)
                    for index in case_state.indices:
                        new_sync_log.index_tree.set_index(case_state.case_id, index.identifier,
                                                          index.referenced_id)
                    if is_dependent:
                        new_sync_log.dependent_case_ids_on_phone.add(case_state.case_id)

            ret = cls.wrap(other_sync_log.to_json())
            for case_state in other_sync_log.cases_on_phone:
                _add_state_contributions(ret, case_state)

            dependent_case_ids = set()
            for case_state in other_sync_log.dependent_cases_on_phone:
                if case_state.case_id in previous_log_footprint:
                    _add_state_contributions(ret, case_state, is_dependent=True)
                    dependent_case_ids.add(case_state.case_id)

            # try to purge any dependent cases - the old format does this on
            # access, but the new format does it ahead of time and always assumes
            # its current state is accurate.
            ret.purge_dependent_cases()

            # set and cleanup other properties
            ret.log_format = LOG_FORMAT_SIMPLIFIED
            del ret['last_seq']
            del ret['cases_on_phone']
            del ret['dependent_cases_on_phone']

            ret.migrated_from = other_sync_log.to_json()
            return ret
        else:
            return super(SimplifiedSyncLog, cls).from_other_format(other_sync_log)

    def tests_only_get_cases_on_phone(self):
        # hack - just for tests
        return [CaseState(case_id=id) for id in self.case_ids_on_phone]

    def test_only_clear_cases_on_phone(self):
        self.case_ids_on_phone = set()

    def test_only_get_dependent_cases_on_phone(self):
        # hack - just for tests
        return [CaseState(case_id=id) for id in self.dependent_case_ids_on_phone]


def _domain_has_legacy_toggle_set():
    # old versions of commcare (< 2.10ish) didn't purge on form completion
    # so can still modify cases that should no longer be on the phone.
    domain = get_request_domain()
    return LEGACY_SYNC_SUPPORT.enabled(domain) if domain else False


def get_sync_log_doc(doc_id):
    try:
        return SyncLog.get_db().get(doc_id)
    except ResourceNotFound:
        legacy_doc = get_db(None).get(doc_id, attachments=True)
        del legacy_doc['_rev']  # remove the rev so we can save this to the new DB
        return legacy_doc


def get_properly_wrapped_sync_log(doc_id):
    """
    Looks up and wraps a sync log, using the class based on the 'log_format' attribute.
    Defaults to the existing legacy SyncLog class.
    """
    return properly_wrap_sync_log(get_sync_log_doc(doc_id))


def properly_wrap_sync_log(doc):
    return get_sync_log_class_by_format(doc.get('log_format')).wrap(doc)


def get_sync_log_class_by_format(format):
    return {
        LOG_FORMAT_LEGACY: SyncLog,
        LOG_FORMAT_SIMPLIFIED: SimplifiedSyncLog,
    }.get(format, SyncLog)


class OwnershipCleanlinessFlag(models.Model):
    """
    Stores whether an owner_id is "clean" aka has a case universe only belonging
    to that ID.

    We use this field to optimize restores.
    """
    domain = models.CharField(max_length=100, db_index=True)
    owner_id = models.CharField(max_length=100, db_index=True)
    is_clean = models.BooleanField(default=False)
    last_checked = models.DateTimeField()
    hint = models.CharField(max_length=100, null=True, blank=True)

    def save(self, force_insert=False, force_update=False, using=None,
             update_fields=None):
        self.last_checked = datetime.utcnow()
        super(OwnershipCleanlinessFlag, self).save(force_insert, force_update, using, update_fields)

    @classmethod
    def get_for_owner(cls, domain, owner_id):
        return cls.objects.get_or_create(domain=domain, owner_id=owner_id)[0]

    class Meta:
        app_label = 'phone'
        unique_together = [('domain', 'owner_id')]
