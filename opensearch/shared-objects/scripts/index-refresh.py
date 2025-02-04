#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import mmguero
import re
import requests
import os
import sys
import urllib3

from collections import defaultdict
from requests.auth import HTTPBasicAuth

GET_STATUS_API = 'api/status'
GET_INDEX_PATTERN_INFO_URI = 'api/saved_objects/_find'
GET_FIELDS_URI = 'api/index_patterns/_fields_for_wildcard'
GET_PUT_INDEX_PATTERN_URI = 'api/saved_objects/index-pattern'
OS_GET_INDEX_TEMPLATE_URI = '_index_template'
OS_GET_COMPONENT_TEMPLATE_URI = '_component_template'
GET_SHARDS_URL = '_cat/shards?h=index,state'
SHARD_UNASSIGNED_STATUS = 'UNASSIGNED'

###################################################################################################
debug = False
scriptName = os.path.basename(__file__)
scriptPath = os.path.dirname(os.path.realpath(__file__))
origPath = os.getcwd()
urllib3.disable_warnings()


###################################################################################################
# main
def main():
    global debug

    parser = argparse.ArgumentParser(description=scriptName, add_help=False, usage='{} <arguments>'.format(scriptName))
    parser.add_argument(
        '-v',
        '--verbose',
        dest='debug',
        type=mmguero.str2bool,
        nargs='?',
        const=True,
        default=False,
        help="Verbose output",
    )
    parser.add_argument(
        '-i',
        '--index',
        dest='index',
        metavar='<str>',
        type=str,
        default=os.getenv('DEFAULT_INDEX_PATTERN', 'ecs-*'),
        help='Index Pattern Name',
    )
    parser.add_argument(
        '-d',
        '--dashboards',
        dest='dashboardsUrl',
        metavar='<protocol://host:port>',
        type=str,
        default=os.getenv('DASHBOARDS_URL', 'https://localhost:5601'),
        help='Dashboards URL',
    )
    parser.add_argument(
        '-o',
        '--opensearch',
        dest='opensearchUrl',
        metavar='<protocol://host:port>',
        type=str,
        default=os.getenv('OPENSEARCH_HOSTS', None),
        help='OpenSearch URL',
    )
    parser.add_argument(
        '-c',
        '--opensearch-curlrc',
        dest='opensearchCurlRcFile',
        metavar='<filename>',
        type=str,
        default=os.getenv('OPENSEARCH_CREDS_CONFIG_FILE', '/var/local/curlrc/.creds.curlrc'),
        help='cURL.rc formatted file containing OpenSearch connection parameters',
    )
    parser.add_argument(
        '--opensearch-ssl-verify',
        dest='opensearchSslVerify',
        type=mmguero.str2bool,
        nargs='?',
        const=True,
        default=mmguero.str2bool(os.getenv('OPENSEARCH_SSL_CERTIFICATE_VERIFICATION', default='False')),
        help="Verify SSL certificates for OpenSearch",
    )
    parser.add_argument(
        '-t',
        '--template',
        dest='template',
        metavar='<str>',
        type=str,
        default=None,
        help='OpenSearch template to merge',
    )
    parser.add_argument(
        '-u',
        '--unassigned',
        dest='fixUnassigned',
        type=mmguero.str2bool,
        nargs='?',
        const=True,
        default=False,
        help="Set number_of_replicas for unassigned index shards to 0",
    )
    parser.add_argument(
        '-n',
        '--dry-run',
        dest='dryrun',
        type=mmguero.str2bool,
        nargs='?',
        const=True,
        default=False,
        help="Dry run (no PUT)",
    )
    try:
        parser.error = parser.exit
        args = parser.parse_args()
    except SystemExit:
        parser.print_help()
        exit(2)

    if args.opensearchUrl:
        if tmpUrlArray := mmguero.LoadStrIfJson(args.opensearchUrl):
            if isinstance(tmpUrlArray, list):
                args.opensearchUrl = tmpUrlArray[0]
    else:
        if opensearchIsLocal:
            args.opensearchUrl = 'http://opensearch:9200'
        elif 'url' in opensearchCreds:
            args.opensearchUrl = opensearchCreds['url']

    debug = args.debug
    if debug:
        mmguero.eprint(os.path.join(scriptPath, scriptName))
        mmguero.eprint("Arguments: {}".format(sys.argv[1:]))
        mmguero.eprint("Arguments: {}".format(args))
    else:
        sys.tracebacklimit = 0

    opensearchCreds = mmguero.ParseCurlFile(args.opensearchCurlRcFile)
    xsrfHeader = "osd-xsrf"

    opensearchReqHttpAuth = (
        HTTPBasicAuth(opensearchCreds['user'], opensearchCreds['password'])
        if opensearchCreds['user'] is not None
        else None
    )

    # get version number so Dashboards doesn't think we're doing a XSRF when we do the PUT
    statusInfoResponse = requests.get(
        '{}/{}'.format(args.dashboardsUrl, GET_STATUS_API),
        auth=opensearchReqHttpAuth,
        verify=args.opensearchSslVerify,
    )
    statusInfoResponse.raise_for_status()
    statusInfo = statusInfoResponse.json()
    dashboardsVersion = statusInfo['version']['number']
    if debug:
        mmguero.eprint('Dashboards version is {}'.format(dashboardsVersion))

    opensearchInfoResponse = requests.get(
        args.opensearchUrl,
        auth=opensearchReqHttpAuth,
        verify=args.opensearchSslVerify,
    )
    opensearchInfo = opensearchInfoResponse.json()
    opensearchVersion = opensearchInfo['version']['number']
    if debug:
        mmguero.eprint('OpenSearch version is {}'.format(opensearchVersion))

    # find the ID of the index name (probably will be the same as the name)
    getIndexInfoResponse = requests.get(
        '{}/{}'.format(args.dashboardsUrl, GET_INDEX_PATTERN_INFO_URI),
        params={'type': 'index-pattern', 'fields': 'id', 'search': '"{}"'.format(args.index)},
        auth=opensearchReqHttpAuth,
        verify=args.opensearchSslVerify,
    )
    getIndexInfoResponse.raise_for_status()
    getIndexInfo = getIndexInfoResponse.json()
    indexId = getIndexInfo['saved_objects'][0]['id'] if (len(getIndexInfo['saved_objects']) > 0) else None
    if debug:
        mmguero.eprint('Index ID for {} is {}'.format(args.index, indexId))

    if indexId is not None:
        # get the current fields list
        getFieldsResponse = requests.get(
            '{}/{}'.format(args.dashboardsUrl, GET_FIELDS_URI),
            params={'pattern': args.index, 'meta_fields': ["_source", "_id", "_type", "_index", "_score"]},
            auth=opensearchReqHttpAuth,
            verify=args.opensearchSslVerify,
        )
        getFieldsResponse.raise_for_status()
        getFieldsList = getFieldsResponse.json()['fields']
        fieldsNames = [field['name'] for field in getFieldsList if 'name' in field]

        # get the fields from the template, if specified, and merge those into the fields list
        if args.template is not None:
            try:
                # request template from OpenSearch and pull the mappings/properties (field list) out
                getTemplateResponse = requests.get(
                    '{}/{}/{}'.format(args.opensearchUrl, OS_GET_INDEX_TEMPLATE_URI, args.template),
                    auth=opensearchReqHttpAuth,
                    verify=args.opensearchSslVerify,
                )
                getTemplateResponse.raise_for_status()
                getTemplateResponseJson = getTemplateResponse.json()
                if 'index_templates' in getTemplateResponseJson:
                    for template in getTemplateResponseJson['index_templates']:
                        templateFields = mmguero.DeepGet(
                            template, ['index_template', 'template', 'mappings', 'properties'], default={}
                        )

                        # also include fields from component templates into templateFields before processing
                        # https://opensearch.org/docs/latest/opensearch/index-templates/#composable-index-templates
                        composedOfList = mmguero.DeepGet(template, ['index_template', 'composed_of'], default=[])

                        for componentName in composedOfList:
                            getComponentResponse = requests.get(
                                '{}/{}/{}'.format(args.opensearchUrl, OS_GET_COMPONENT_TEMPLATE_URI, componentName),
                                auth=opensearchReqHttpAuth,
                                verify=args.opensearchSslVerify,
                            )
                            getComponentResponse.raise_for_status()
                            getComponentResponseJson = getComponentResponse.json()
                            if 'component_templates' in getComponentResponseJson:
                                for component in getComponentResponseJson['component_templates']:
                                    properties = mmguero.DeepGet(
                                        component,
                                        ['component_template', 'template', 'mappings', 'properties'],
                                        default=None,
                                    )
                                    if properties:
                                        templateFields.update(properties)

                        # a field should be merged if it's not already in the list we have from Dashboards, and it's
                        # in the list of types we're merging (leave more complex types like nested and geolocation
                        # to be handled naturally as the data shows up)
                        for field in templateFields:
                            mergeFieldTypes = ("date", "float", "integer", "ip", "keyword", "long", "short", "text")
                            if (
                                (field not in fieldsNames)
                                and ('type' in templateFields[field])
                                and (templateFields[field]['type'] in mergeFieldTypes)
                            ):
                                # create field dict in same format as those returned by GET_FIELDS_URI above
                                mergedFieldInfo = {}
                                mergedFieldInfo['name'] = field
                                mergedFieldInfo['esTypes'] = [templateFields[field]['type']]
                                if (
                                    (templateFields[field]['type'] == 'float')
                                    or (templateFields[field]['type'] == 'integer')
                                    or (templateFields[field]['type'] == 'long')
                                    or (templateFields[field]['type'] == 'short')
                                ):
                                    mergedFieldInfo['type'] = 'number'
                                elif (templateFields[field]['type'] == 'keyword') or (
                                    templateFields[field]['type'] == 'text'
                                ):
                                    mergedFieldInfo['type'] = 'string'
                                else:
                                    mergedFieldInfo['type'] = templateFields[field]['type']
                                mergedFieldInfo['searchable'] = True
                                mergedFieldInfo['aggregatable'] = "text" not in mergedFieldInfo['esTypes']
                                mergedFieldInfo['readFromDocValues'] = mergedFieldInfo['aggregatable']
                                fieldsNames.append(field)
                                getFieldsList.append(mergedFieldInfo)

                            # elif debug:
                            #   mmguero.eprint('Not merging {}: {}'.format(field, json.dumps(templateFields[field])))

            except Exception as e:
                mmguero.eprint('"{}" raised for "{}", skipping template merge'.format(str(e), args.template))

        if debug:
            mmguero.eprint('{} would have {} fields'.format(args.index, len(getFieldsList)))

        # first get the previous field format map as a starting point, if any
        getResponse = requests.get(
            '{}/{}/{}'.format(args.dashboardsUrl, GET_PUT_INDEX_PATTERN_URI, indexId),
            headers={
                'Content-Type': 'application/json',
                xsrfHeader: 'true',
            },
            auth=opensearchReqHttpAuth,
            verify=args.opensearchSslVerify,
        )
        getResponse.raise_for_status()
        try:
            fieldFormatMap = json.loads(
                mmguero.DeepGet(getResponse.json(), ['attributes', 'fieldFormatMap'], default="{}")
            )
        except Exception as e:
            fieldFormatMap = {}

        # define field formatting map for Dashboards -> Arkime drilldown and other URL drilldowns
        #
        # fieldFormatMap is
        #    {
        #        "destination.port": {
        #           "id": "url",
        #           "params": {
        #             "urlTemplate": "https://www.iana.org/assignments/service-names-port-numbers/service-names-port-numbers.xhtml?search={{value}}",
        #             "labelTemplate": "{{value}}",
        #             "openLinkInCurrentTab": false
        #           }
        #        },
        #        ...
        #    }
        pivotIgnoreTypes = ['date']

        for field in [
            x
            for x in getFieldsList
            if x['name'][:1].isalpha() and (x['name'] not in fieldFormatMap) and (x['type'] not in pivotIgnoreTypes)
        ]:
            fieldFormatInfo = {}
            fieldFormatInfo['id'] = 'url'
            fieldFormatInfo['params'] = {}

            if field['name'].endswith('.segment.id'):
                fieldFormatInfo['params']['urlTemplate'] = '/netbox/ipam/prefixes/{{value}}'

            elif field['name'].endswith('.segment.name') or (field['name'] == 'network.name'):
                fieldFormatInfo['params'][
                    'urlTemplate'
                ] = '/netbox/search/?q={{value}}&obj_types=ipam.prefix&lookup=iexact'

            elif field['name'].endswith('.segment.tenant'):
                fieldFormatInfo['params'][
                    'urlTemplate'
                ] = '/netbox/search/?q={{value}}&obj_types=tenancy.tenant&lookup=iexact'

            elif field['name'].endswith('.device.id') or (field['name'] == 'related.device_id'):
                fieldFormatInfo['params']['urlTemplate'] = '/netbox/dcim/devices/{{value}}'

            elif field['name'].endswith('.device.name') or (field['name'] == 'related.device_name'):
                fieldFormatInfo['params'][
                    'urlTemplate'
                ] = '/netbox/search/?q={{value}}&obj_types=dcim.device&obj_types=virtualization.virtualmachine&lookup=iexact'

            elif field['name'].endswith('.device.cluster'):
                fieldFormatInfo['params'][
                    'urlTemplate'
                ] = '/netbox/search/?q={{value}}&obj_types=virtualization.cluster&lookup=iexact'

            elif field['name'].endswith('.device.device_type') or (field['name'] == 'related.device_type'):
                fieldFormatInfo['params']['urlTemplate'] = '/netbox/search/?q={{value}}&obj_types=dcim.devicetype'

            elif field['name'].endswith('.device.manufacturer') or (field['name'] == 'related.manufacturer'):
                fieldFormatInfo['params']['urlTemplate'] = '/netbox/search/?q={{value}}&obj_types=dcim.manufacturer'

            elif field['name'].endswith('.device.role') or (field['name'] == 'related.role'):
                fieldFormatInfo['params']['urlTemplate'] = '/netbox/search/?q={{value}}&obj_types=dcim.devicerole'

            elif field['name'].endswith('.device.service') or (field['name'] == 'related.service'):
                fieldFormatInfo['params']['urlTemplate'] = '/netbox/search/?q={{value}}&obj_types=ipam.service'

            elif field['name'].endswith('.device.url') or field['name'].endswith('.segment.url'):
                fieldFormatInfo['params']['urlTemplate'] = '{{value}}'

            elif (
                field['name'].endswith('.device.site')
                or field['name'].endswith('.segment.site')
                or (field['name'] == 'related.site')
            ):
                fieldFormatInfo['params'][
                    'urlTemplate'
                ] = '/netbox/search/?q={{value}}&obj_types=dcim.site&lookup=iexact'

            elif field['name'] == 'zeek.files.extracted_uri':
                fieldFormatInfo['params']['urlTemplate'] = '/{{value}}'

            else:
                # for Arkime to query by database field name, see arkime issue/PR 1461/1463
                valQuote = '"' if field['type'] == 'string' else ''
                valDbPrefix = (
                    '' if (field['name'].startswith('zeek') or field['name'].startswith('suricata')) else 'db:'
                )
                fieldFormatInfo['params']['urlTemplate'] = '/iddash2ark/{}{} == {}{{{{value}}}}{}'.format(
                    valDbPrefix, field['name'], valQuote, valQuote
                )

            fieldFormatInfo['params']['labelTemplate'] = '{{value}}'
            fieldFormatInfo['params']['openLinkInCurrentTab'] = False

            fieldFormatMap[field['name']] = fieldFormatInfo

        # set the index pattern with our complete list of fields
        putIndexInfo = {}
        putIndexInfo['attributes'] = {}
        putIndexInfo['attributes']['title'] = args.index
        putIndexInfo['attributes']['fields'] = json.dumps(getFieldsList)
        putIndexInfo['attributes']['fieldFormatMap'] = json.dumps(fieldFormatMap)

        if not args.dryrun:
            putResponse = requests.put(
                '{}/{}/{}'.format(args.dashboardsUrl, GET_PUT_INDEX_PATTERN_URI, indexId),
                headers={
                    'Content-Type': 'application/json',
                    xsrfHeader: 'true',
                },
                data=json.dumps(putIndexInfo),
                auth=opensearchReqHttpAuth,
                verify=args.opensearchSslVerify,
            )
            putResponse.raise_for_status()

        # if we got this far, it probably worked!
        if args.dryrun:
            print("success (dry run only, no write performed)")
        else:
            print("success")

    else:
        print("failure (could not find Index ID for {})".format(args.index))

    if args.fixUnassigned and not args.dryrun:
        # set some configuration-related indexes (opensearch/opendistro) replica count to 0
        # so we don't have yellow index state on those
        shardsResponse = requests.get(
            '{}/{}'.format(args.opensearchUrl, GET_SHARDS_URL),
            auth=opensearchReqHttpAuth,
            verify=args.opensearchSslVerify,
        )
        for shardLine in shardsResponse.iter_lines():
            shardInfo = shardLine.decode('utf-8').split()
            if (shardInfo is not None) and (len(shardInfo) == 2) and (shardInfo[1] == SHARD_UNASSIGNED_STATUS):
                putResponse = requests.put(
                    '{}/{}/{}'.format(args.opensearchUrl, shardInfo[0], '_settings'),
                    headers={
                        'Content-Type': 'application/json',
                        xsrfHeader: 'true',
                    },
                    data=json.dumps({'index': {'number_of_replicas': 0}}),
                    auth=opensearchReqHttpAuth,
                    verify=args.opensearchSslVerify,
                )
                putResponse.raise_for_status()


if __name__ == '__main__':
    main()
