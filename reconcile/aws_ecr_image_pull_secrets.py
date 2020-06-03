import base64
import json
import logging

import reconcile.queries as queries
import utils.vault_client as vault_client

from utils.aws_api import AWSApi

QONTRACT_INTEGRATION = 'aws-ecr-image-pull-secrets'


def enc_dec(data):
    return base64.b64encode(data.encode('utf-8')).decode('utf-8')


def get_password(token):
    return base64.b64decode(token).decode('utf-8').split(':')[1]


def construct_dockercfg_secret_data(data):
    auth_data = data['authorizationData'][0]
    server = auth_data['proxyEndpoint']
    token = auth_data['authorizationToken']
    password = get_password(token)
    data = {
        'auths': {
            server: {
                'username': 'AWS',
                'password': password,
                'email': '',
                'auth': token
            }
        }
    }

    return {'.dockercfg': enc_dec(json.dumps(data))}


def construct_basic_auth_secret_data(data):
    auth_data = data['authorizationData'][0]
    token = auth_data['authorizationToken']
    password = get_password(token)
    return {'username': enc_dec('AWS'), 'token': enc_dec(password)}


def write_output_to_vault(dry_run, vault_path, account, secret_data, name):
    integration_name = QONTRACT_INTEGRATION
    secret_path = f"{vault_path}/{integration_name}/{account}/{name}"
    secret = {'path': secret_path, 'data': secret_data}
    logging.info(['write_secret', secret_path])
    if not dry_run:
        vault_client.write(secret)


def run(dry_run=False, vault_output_path=''):
    accounts = [a for a in queries.get_aws_accounts() if a.get('ecrs')]
    settings = queries.get_app_interface_settings()
    aws = AWSApi(1, accounts, settings=settings)
    tokens = aws.get_ecr_auth_tokens()
    for account, data in tokens.items():
        dockercfg_secret_data = construct_dockercfg_secret_data(data)
        basic_auth_secret_data = construct_basic_auth_secret_data(data)
        write_output_to_vault(dry_run, vault_output_path,
                              account, dockercfg_secret_data, 'dockercfg')
        write_output_to_vault(dry_run, vault_output_path,
                              account, basic_auth_secret_data, 'basic-auth')
