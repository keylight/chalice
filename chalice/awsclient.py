"""Simplified AWS client.

This module abstracts the botocore session and clients
to provide a simpler interface.  This interface only
contains the API calls needed to work with AWS services
used by chalice.

The interface provided can range from a direct 1-1 mapping
of a method to a method on a botocore client all the way up
to combining API calls across multiple AWS services.

As a side benefit, I can also add type annotations to
this class to get improved type checking across chalice.

"""
import os
import time
import tempfile
import datetime
import zipfile
import shutil
import json

import botocore.session  # noqa
from typing import Any, Optional, Dict, Callable, List, Iterator  # noqa

from chalice.constants import DEFAULT_STAGE_NAME


_STR_MAP = Optional[Dict[str, str]]
_OPT_STR = Optional[str]
_OPT_INT = Optional[int]


class ResourceDoesNotExistError(Exception):
    pass


class TypedAWSClient(object):

    # 30 * 5 == 150 seconds or 2.5 minutes for the initial lambda
    # creation + role propagation.
    LAMBDA_CREATE_ATTEMPTS = 30
    DELAY_TIME = 5

    def __init__(self, session, sleep=time.sleep):
        # type: (botocore.session.Session, Callable[[int], None]) -> None
        self._session = session
        self._sleep = sleep
        self._client_cache = {}  # type: Dict[str, Any]

    def lambda_function_exists(self, name):
        # type: (str) -> bool
        client = self._client('lambda')
        try:
            client.get_function(FunctionName=name)
            return True
        except client.exceptions.ResourceNotFoundException:
            return False

    def get_function_configuration(self, name):
        # type: (str) -> Dict[str, Any]
        response = self._client('lambda').get_function_configuration(
            FunctionName=name)
        return response

    def create_function(self,
                        function_name,               # type: str
                        role_arn,                    # type: str
                        zip_contents,                # type: str
                        runtime,                     # type: str
                        environment_variables=None,  # type: _STR_MAP
                        tags=None,                   # type: _STR_MAP
                        timeout=None,                # type: _OPT_INT
                        memory_size=None             # type: _OPT_INT
                        ):
        # type: (...) -> str
        kwargs = {
            'FunctionName': function_name,
            'Runtime': runtime,
            'Code': {'ZipFile': zip_contents},
            'Handler': 'app.app',
            'Role': role_arn,
        }  # type: Dict[str, Any]
        if environment_variables is not None:
            kwargs['Environment'] = {"Variables": environment_variables}
        if tags is not None:
            kwargs['Tags'] = tags
        if timeout is not None:
            kwargs['Timeout'] = timeout
        if memory_size is not None:
            kwargs['MemorySize'] = memory_size
        client = self._client('lambda')
        attempts = 0
        while True:
            try:
                response = client.create_function(**kwargs)
            except client.exceptions.InvalidParameterValueException:
                # We're assuming that if we receive an
                # InvalidParameterValueException, it's because
                # the role we just created can't be used by
                # Lambda.
                self._sleep(self.DELAY_TIME)
                attempts += 1
                if attempts >= self.LAMBDA_CREATE_ATTEMPTS:
                    raise
                continue
            return response['FunctionArn']

    def delete_function(self, function_name):
        # type: (str) -> None
        lambda_client = self._client('lambda')
        try:
            lambda_client.delete_function(FunctionName=function_name)
        except lambda_client.exceptions.ResourceNotFoundException:
            raise ResourceDoesNotExistError(function_name)

    def update_function(self,
                        function_name,               # type: str
                        zip_contents,                # type: str
                        environment_variables=None,  # type: _STR_MAP
                        runtime=None,                # type: _OPT_STR
                        tags=None,                   # type: _STR_MAP
                        timeout=None,                # type: _OPT_INT
                        memory_size=None             # type: _OPT_INT
                        ):
        # type: (...) -> Dict[str, Any]
        """Update a Lambda function's code and configuration.

        This method only updates the values provided to it. If a parameter
        is not provided, no changes will be made for that that parameter on
        the targeted lambda function.
        """
        lambda_client = self._client('lambda')
        return_value = lambda_client.update_function_code(
            FunctionName=function_name, ZipFile=zip_contents)

        kwargs = {}  # type: Dict[str, Any]
        if environment_variables is not None:
            kwargs['Environment'] = {'Variables': environment_variables}
        if runtime is not None:
            kwargs['Runtime'] = runtime
        if timeout is not None:
            kwargs['Timeout'] = timeout
        if memory_size is not None:
            kwargs['MemorySize'] = memory_size
        if kwargs:
            kwargs['FunctionName'] = function_name
            lambda_client.update_function_configuration(**kwargs)
        if tags is not None:
            self._update_function_tags(return_value['FunctionArn'], tags)
        return return_value

    def _update_function_tags(self, function_arn, requested_tags):
        # type: (str, Dict[str, str]) -> None
        remote_tags = self._client('lambda').list_tags(
            Resource=function_arn)['Tags']
        self._remove_unrequested_remote_tags(
            function_arn, requested_tags, remote_tags)
        self._add_missing_or_differing_value_requested_tags(
            function_arn, requested_tags, remote_tags)

    def _remove_unrequested_remote_tags(
            self, function_arn, requested_tags, remote_tags):
        # type: (str, Dict[Any, Any], Dict[Any, Any]) -> None
        tag_keys_to_remove = list(set(remote_tags) - set(requested_tags))
        if tag_keys_to_remove:
            self._client('lambda').untag_resource(
                Resource=function_arn, TagKeys=tag_keys_to_remove)

    def _add_missing_or_differing_value_requested_tags(
            self, function_arn, requested_tags, remote_tags):
        # type: (str, Dict[Any, Any], Dict[Any, Any]) -> None
        tags_to_add = {k: v for k, v in requested_tags.items()
                       if k not in remote_tags or v != remote_tags[k]}
        if tags_to_add:
            self._client('lambda').tag_resource(
                Resource=function_arn, Tags=tags_to_add)

    def get_role_arn_for_name(self, name):
        # type: (str) -> str
        client = self._client('iam')
        try:
            role = client.get_role(RoleName=name)
        except client.exceptions.NoSuchEntityException:
            raise ValueError("No role ARN found for: %s" % name)
        return role['Role']['Arn']

    def delete_role_policy(self, role_name, policy_name):
        # type: (str, str) -> None
        self._client('iam').delete_role_policy(RoleName=role_name,
                                               PolicyName=policy_name)

    def put_role_policy(self, role_name, policy_name, policy_document):
        # type: (str, str, Dict[str, Any]) -> None
        # Note: policy_document is not JSON encoded.
        self._client('iam').put_role_policy(
            RoleName=role_name,
            PolicyName=policy_name,
            PolicyDocument=json.dumps(policy_document, indent=2))

    def create_role(self, name, trust_policy, policy):
        # type: (str, Dict[str, Any], Dict[str, Any]) -> str
        client = self._client('iam')
        response = client.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust_policy)
        )
        role_arn = response['Role']['Arn']
        self.put_role_policy(role_name=name, policy_name=name,
                             policy_document=policy)
        return role_arn

    def delete_role(self, name):
        # type: (str) -> None
        """Delete a role by first deleting all inline policies."""
        client = self._client('iam')
        inline_policies = client.list_role_policies(
            RoleName=name
        )['PolicyNames']
        for policy_name in inline_policies:
            self.delete_role_policy(name, policy_name)
        client.delete_role(RoleName=name)

    def get_rest_api_id(self, name):
        # type: (str) -> Optional[str]
        """Get rest api id associated with an API name.

        :type name: str
        :param name: The name of the rest api.

        :rtype: str
        :return: If the rest api exists, then the restApiId
            is returned, otherwise None.

        """
        rest_apis = self._client('apigateway').get_rest_apis()['items']
        for api in rest_apis:
            if api['name'] == name:
                return api['id']
        return None

    def rest_api_exists(self, rest_api_id):
        # type: (str) -> bool
        """Check if an an API Gateway REST API exists."""
        client = self._client('apigateway')
        try:
            client.get_rest_api(restApiId=rest_api_id)
            return True
        except client.exceptions.NotFoundException:
            return False

    def import_rest_api(self, swagger_document):
        # type: (Dict[str, Any]) -> str
        client = self._client('apigateway')
        response = client.import_rest_api(
            body=json.dumps(swagger_document, indent=2)
        )
        rest_api_id = response['id']
        return rest_api_id

    def update_api_from_swagger(self, rest_api_id, swagger_document):
        # type: (str, Dict[str, Any]) -> None
        client = self._client('apigateway')
        client.put_rest_api(
            restApiId=rest_api_id,
            mode='overwrite',
            body=json.dumps(swagger_document, indent=2))

    def delete_rest_api(self, rest_api_id):
        # type: (str) -> None
        client = self._client('apigateway')
        try:
            client.delete_rest_api(restApiId=rest_api_id)
        except client.exceptions.NotFoundException:
            raise ResourceDoesNotExistError(rest_api_id)

    def deploy_rest_api(self, rest_api_id, api_gateway_stage):
        # type: (str, str) -> None
        client = self._client('apigateway')
        client.create_deployment(
            restApiId=rest_api_id,
            stageName=api_gateway_stage,
        )

    def add_permission_for_apigateway_if_needed(self, function_name,
                                                region_name, account_id,
                                                rest_api_id, random_id):
        # type: (str, str, str, str, str) -> None
        """Authorize API gateway to invoke a lambda function is needed.

        This method will first check if API gateway has permission to call
        the lambda function, and only if necessary will it invoke
        ``self.add_permission_for_apigateway(...).

        """
        has_necessary_permissions = False
        client = self._client('lambda')
        try:
            policy = self.get_function_policy(function_name)
        except client.exceptions.ResourceNotFoundException:
            pass
        else:
            source_arn = self._build_source_arn_str(region_name, account_id,
                                                    rest_api_id)
            # Here's what a sample policy looks like after add_permission()
            # has been previously called:
            # {
            #  "Id": "default",
            #  "Statement": [
            #   {
            #    "Action": "lambda:InvokeFunction",
            #    "Condition": {
            #     "ArnLike": {
            #       "AWS:SourceArn": <source_arn>
            #     }
            #    },
            #    "Effect": "Allow",
            #    "Principal": {
            #     "Service": "apigateway.amazonaws.com"
            #    },
            #    "Resource": "arn:aws:lambda:us-west-2:aid:function:name",
            #    "Sid": "e4755709-067e-4254-b6ec-e7f9639e6f7b"
            #   }
            #  ],
            #  "Version": "2012-10-17"
            # }
            # So we need to check if there's a policy that looks like this.
            for statement in policy.get('Statement', []):
                if self._gives_apigateway_access(statement, function_name,
                                                 source_arn):
                    has_necessary_permissions = True
                    break
        if not has_necessary_permissions:
            self.add_permission_for_apigateway(
                function_name, region_name, account_id, rest_api_id, random_id)

    def _gives_apigateway_access(self, statement, function_name, source_arn):
        # type: (Dict[str, Any], str, str) -> bool
        if not statement['Action'] == 'lambda:InvokeFunction':
            return False
        if statement.get('Condition', {}).get('ArnLike',
                                              {}).get('AWS:SourceArn',
                                                      '') != source_arn:
            return False
        if statement.get('Principal', {}).get('Service', '') != \
                'apigateway.amazonaws.com':
            return False
        # We're not checking the "Resource" key because we're assuming
        # that lambda.get_policy() is returning the policy for the particular
        # resource in question.
        return True

    def get_function_policy(self, function_name):
        # type: (str) -> Dict[str, Any]
        """Return the function policy for a lambda function.

        This function will extract the policy string as a json document
        and return the json.loads(...) version of the policy.

        """
        client = self._client('lambda')
        policy = client.get_policy(FunctionName=function_name)
        return json.loads(policy['Policy'])

    def download_sdk(self, rest_api_id, output_dir,
                     api_gateway_stage=DEFAULT_STAGE_NAME,
                     sdk_type='javascript'):
        # type: (str, str, str, str) -> None
        """Download an SDK to a directory.

        This will generate an SDK and download it to the provided
        ``output_dir``.  If you're using ``get_sdk_download_stream()``,
        you have to handle downloading the stream and unzipping the
        contents yourself.  This method handles that for you.

        """
        zip_stream = self.get_sdk_download_stream(
            rest_api_id, api_gateway_stage=api_gateway_stage,
            sdk_type=sdk_type)
        tmpdir = tempfile.mkdtemp()
        with open(os.path.join(tmpdir, 'sdk.zip'), 'wb') as f:
            f.write(zip_stream.read())
        tmp_extract = os.path.join(tmpdir, 'extracted')
        with zipfile.ZipFile(os.path.join(tmpdir, 'sdk.zip')) as z:
            z.extractall(tmp_extract)
        # The extract zip dir will have a single directory:
        #  ['apiGateway-js-sdk']
        dirnames = os.listdir(tmp_extract)
        if len(dirnames) == 1:
            full_dirname = os.path.join(tmp_extract, dirnames[0])
            if os.path.isdir(full_dirname):
                final_dirname = 'chalice-%s-sdk' % sdk_type
                full_renamed_name = os.path.join(tmp_extract, final_dirname)
                os.rename(full_dirname, full_renamed_name)
                shutil.move(full_renamed_name, output_dir)
                return
        raise RuntimeError(
            "The downloaded SDK had an unexpected directory structure: %s" %
            (', '.join(dirnames)))

    def get_sdk_download_stream(self, rest_api_id,
                                api_gateway_stage=DEFAULT_STAGE_NAME,
                                sdk_type='javascript'):
        # type: (str, str, str) -> file
        """Generate an SDK for a given SDK.

        Returns a file like object that streams a zip contents for the
        generated SDK.

        """
        response = self._client('apigateway').get_sdk(
            restApiId=rest_api_id, stageName=api_gateway_stage,
            sdkType=sdk_type)
        return response['body']

    def add_permission_for_apigateway(self, function_name, region_name,
                                      account_id, rest_api_id, random_id):
        # type: (str, str, str, str, str) -> None
        """Authorize API gateway to invoke a lambda function."""
        client = self._client('lambda')
        source_arn = self._build_source_arn_str(region_name, account_id,
                                                rest_api_id)
        client.add_permission(
            Action='lambda:InvokeFunction',
            FunctionName=function_name,
            StatementId=random_id,
            Principal='apigateway.amazonaws.com',
            SourceArn=source_arn,
        )

    def _build_source_arn_str(self, region_name, account_id, rest_api_id):
        # type: (str, str, str) -> str
        source_arn = (
            'arn:aws:execute-api:'
            '{region_name}:{account_id}:{rest_api_id}/*').format(
                region_name=region_name,
                # Assuming same account id for lambda function and API gateway.
                account_id=account_id,
                rest_api_id=rest_api_id)
        return source_arn

    @property
    def region_name(self):
        # type: () -> str
        return self._client('apigateway').meta.region_name

    def iter_log_events(self, log_group_name, interleaved=True):
        # type: (str, bool) -> Iterator[Dict[str, Any]]
        logs = self._client('logs')
        paginator = logs.get_paginator('filter_log_events')
        for page in paginator.paginate(logGroupName=log_group_name,
                                       interleaved=True):
            events = page['events']
            for event in events:
                # timestamp is modeled as a 'long', so we'll
                # convert to a datetime to make it easier to use
                # in python.
                event['ingestionTime'] = self._convert_to_datetime(
                    event['ingestionTime'])
                event['timestamp'] = self._convert_to_datetime(
                    event['timestamp'])
                yield event

    def _convert_to_datetime(self, integer_timestamp):
        # type: (int) -> datetime.datetime
        return datetime.datetime.fromtimestamp(integer_timestamp / 1000.0)

    def _client(self, service_name):
        # type: (str) -> Any
        if service_name not in self._client_cache:
            self._client_cache[service_name] = self._session.create_client(
                service_name)
        return self._client_cache[service_name]
