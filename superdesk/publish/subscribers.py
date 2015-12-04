# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2013, 2014 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

import json
import logging
from superdesk import get_resource_service
from eve.utils import ParsedRequest, config
from superdesk.utils import ListCursor
from superdesk.resource import Resource
from superdesk.services import BaseService
from superdesk.errors import SuperdeskApiError
from superdesk.publish import subscriber_types, SUBSCRIBER_TYPES  # NOQA
from superdesk.metadata.item import not_analyzed
from flask import current_app as app

logger = logging.getLogger(__name__)


class SubscribersResource(Resource):
    schema = {
        'name': {
            'type': 'string',
            'iunique': True,
            'required': True,
            'nullable': False,
            'empty': False
        },
        'media_type': {
            'type': 'string'
        },
        'geo_restrictions': {
            'type': 'string',
            'nullable': True
        },
        'subscriber_type': {
            'type': 'string',
            'allowed': subscriber_types,
            'required': True
        },
        'sequence_num_settings': {
            'type': 'dict',
            'schema': {
                'min': {'type': 'integer'},
                'max': {'type': 'integer'}
            },
            'required': True
        },
        'email': {
            'type': 'email',
            'empty': False,
            'required': True
        },
        'is_active': {
            'type': 'boolean',
            'default': True
        },
        'critical_errors': {
            'type': 'dict',
            'keyschema': {
                'type': 'boolean'
            }
        },
        'last_closed': {
            'type': 'dict',
            'schema': {
                'closed_at': {'type': 'datetime'},
                'closed_by': Resource.rel('users', nullable=True),
                'message': {'type': 'string'}
            }
        },
        'destinations': {
            'type': 'list',
            'required': True,
            "minlength": 1,
            'schema': {
                'type': 'dict',
                'schema': {
                    'name': {'type': 'string', 'required': True, 'empty': False},
                    'format': {'type': 'string', 'required': True},
                    'delivery_type': {'type': 'string', 'required': True},
                    'config': {'type': 'dict'}
                }
            }
        },
        'content_filter': {
            'type': 'dict',
            'schema': {
                'filter_id': Resource.rel('content_filters', nullable=True),
                'filter_type': {
                    'type': 'string',
                    'allowed': ['blocking', 'permitting'],
                    'default': 'blocking'
                }
            },
            'nullable': True
        },
        'global_filters': {
            'type': 'dict',
            'keyschema': {
                'type': 'boolean'
            }
        },
        'sequence_number': {
            'type': 'number',
            'default': 0,
            'mapping': not_analyzed
        },
    }

    item_methods = ['GET', 'PATCH', 'PUT']

    etag_ignore_fields = ['sequence_number']
    privileges = {'POST': 'subscribers', 'PATCH': 'subscribers'}


class SubscribersService(BaseService):
    def get(self, req, lookup):
        if req is None:
            req = ParsedRequest()
        if req.args and req.args.get('filter_condition'):
            filter_condition = json.loads(req.args.get('filter_condition'))
            return ListCursor(self._get_subscribers_by_filter_condition(filter_condition))
        return super().get(req=req, lookup=lookup)

    def on_create(self, docs):
        for doc in docs:
            self._validate_seq_num_settings(doc)

    def on_update(self, updates, original):
        self._validate_seq_num_settings(updates)

    def _get_subscribers_by_filter_condition(self, filter_condition):
        """
        Searches all subscribers that has a content filter with the given filter condition
        If filter condition is used in a global filter then it returns all
        subscribers that not disabled the global filter.
        :param filter_condition: Filter condition to test
        :return: List of subscribers
        """
        req = ParsedRequest()
        all_subscribers = list(super().get(req=req, lookup=None))
        selected_subscribers = {}

        filter_condition_service = get_resource_service('filter_conditions')
        content_filter_service = get_resource_service('content_filters')
        existing_filter_conditions = filter_condition_service.check_similar(filter_condition)
        for fc in existing_filter_conditions:
            existing_content_filters = content_filter_service.get_content_filters_by_filter_condition(fc['_id'])
            for pf in existing_content_filters:
                if pf.get('is_global', False):
                    for s in all_subscribers:
                        gfs = s.get('global_filters', {})
                        if gfs.get(str(pf['_id']), True):
                            selected_subscribers[s['_id']] = s

                for s in all_subscribers:
                    if s.get('content_filter') and \
                        'filter_id' in s['content_filter'] and \
                            s['content_filter']['filter_id'] == pf['_id']:
                        selected_subscribers[s['_id']] = s

        return list(selected_subscribers.values())

    def _validate_seq_num_settings(self, subscriber):
        """
        Validates the 'sequence_num_settings' property if present in subscriber. Below are the validation rules:
            1.  If min value is present then it should be greater than 0
            2.  If min is present and max value isn't available then it's defaulted to MAX_VALUE_OF_PUBLISH_SEQUENCE

        :return: True if validation succeeds otherwise return False.
        """

        if subscriber.get('sequence_num_settings'):
            min = subscriber.get('sequence_num_settings').get('min', 1)
            max = subscriber.get('sequence_num_settings').get('max', app.config['MAX_VALUE_OF_PUBLISH_SEQUENCE'])

            if min <= 0:
                raise SuperdeskApiError.badRequestError(payload={"sequence_num_settings.min": 1},
                                                        message="Value of Minimum in Sequence Number Settings should "
                                                                "be greater than 0")

            if min >= max:
                raise SuperdeskApiError.badRequestError(payload={"sequence_num_settings.min": 1},
                                                        message="Value of Minimum in Sequence Number Settings should "
                                                                "be less than the value of Maximum")

            del subscriber['sequence_num_settings']
            subscriber['sequence_num_settings'] = {"min": min, "max": max}

        return True

    def generate_sequence_number(self, subscriber):
        """
        Generates Published Sequence Number for the passed subscriber
        """

        assert (subscriber is not None), "Subscriber can't be null"

        max_seq_number = app.config['MAX_VALUE_OF_PUBLISH_SEQUENCE']
        subscribers_resource = get_resource_service('subscribers')
        subscriber_id = subscriber[config.ID_FIELD]
        subscriber = subscribers_resource.find_and_modify(
            query={'_id': subscriber_id},
            update={'$inc': {'sequence_number': 1}},
            upsert=False
        )
        sequence_number = subscriber.get("sequence_number")

        if subscriber.get('sequence_num_settings'):
            if sequence_number == 0 or sequence_number == 1:
                sequence_number = subscriber['sequence_num_settings']['min']
                subscribers_resource.find_and_modify(
                    query={'_id': subscriber_id},
                    update={'sequence_number': sequence_number},
                    upsert=False
                )

            max_seq_number = subscriber['sequence_num_settings']['max']

        if sequence_number == max_seq_number:
            subscribers_resource.find_and_modify(
                query={'_id': subscriber_id},
                update={'sequence_number': 0},
                upsert=False
            )

        return sequence_number
