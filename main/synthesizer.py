import json
import os
from typing import List

import pulumi
import pulumi_aws
import pulumi_aws.iam as iam
import pulumi_mysql as mysql
import pulumi_okta
import pulumi_okta.app as okta_app
from loguru import logger
from prodict import Prodict

from config.settings import grant_types, bastion_host
from main.bastion_host import update_authorized_keys
from main.constants import RDS_ROLE_PREFIX
from main.dbstatus import DbStatus

TYPE_AWS_IAM_ROLE = "aws:iam:Role"
TYPE_AWS_IAM_ROLE_POLICY = "aws:iam:RolePolicy"
TYPE_MYSQL_USER = "mysql:User"
TYPE_MYSQL_GRANT = "mysql:Grant"
TYPE_OKTA_APP_USER = "okta:app:User"


class Synthesizer:

    def __init__(self, model: Prodict):
        self.model = model
        self._resources = {}

    def synthesize_all(self):
        self.synthesize_iam()
        self.synthesize_mysql()
        # TODO: maybe it's possible to detect the out-of-sync know-issue with Okta-AWS here and halt, asking for
        #   manual intervention from the Okta Administrator
        self.synthesize_okta()
        self.synthesize_bastion_host()

    def synthesize_iam(self):
        aws = self.model.aws
        provider = pulumi_aws.Provider("default", region=aws.region)
        okta = self.model.okta
        assume_role_policy = _aws_make_policy([{
            "Sid": "1",
            "Effect": "Allow",
            "Principal": {
                "Federated": f"arn:aws:iam::{aws.account}:saml-provider/{okta.aws_app.iam_user}"
            },
            "Action": "sts:AssumeRoleWithSAML",
            "Condition": {
                "StringEquals": {
                    "SAML:aud": "https://signin.aws.amazon.com/saml"
                }
            }
        }])
        for db_id, db in self.model.aws.databases.items():
            if DbStatus[db.status] < DbStatus.ENABLED:
                continue
            role_name = f"{RDS_ROLE_PREFIX}{db_id}"
            role = iam.Role(db_id,
                            name=role_name,
                            description=f"Allow access to '{db_id}' using db-auth-token",
                            # TODO: tags=
                            assume_role_policy=assume_role_policy,
                            opts=pulumi.ResourceOptions(provider=provider))
            self._add_resource(TYPE_AWS_IAM_ROLE, db_id, role)
            if db.permissions:
                db_policy = _aws_make_policy([{
                    "Sid": "DescribeDBInstances",
                    "Effect": "Allow",
                    "Action": "rds:DescribeDBInstances",
                    "Resource": "*"
                }] + [{
                    "Sid": str(i),
                    "Effect": "Allow",
                    "Action": "rds-db:connect",
                    "Resource": f"arn:aws:rds-db:{aws.region}:{aws.account}:dbuser:{db.dbi_resource_id}/{login}"
                } for i, login in enumerate(db.permissions)]
                                             )
            else:
                db_policy = _aws_make_policy([{
                    "Sid": "1",
                    "Effect": "Deny",
                    "Action": "rds:DescribeDBInstances",
                    "Resource": f"arn:aws:rds-db:{aws.region}:{aws.account}:dbuser:{db.dbi_resource_id}/*"
                }])
            role_policy = iam.RolePolicy(db_id,
                                         name=role_name,
                                         role=role.id,
                                         policy=db_policy,
                                         opts=pulumi.ResourceOptions(provider=provider))
            self._add_resource(TYPE_AWS_IAM_ROLE_POLICY, db_id, role_policy)

    def synthesize_mysql(self):
        providers = {}
        for login, user in self.model.okta.users.items():
            for db_id, grant_type in user.permissions.items():
                db = self.model.aws.databases[db_id]
                if DbStatus[db.status] < DbStatus.ACCESSIBLE:
                    continue
                provider = providers.get(db_id, None)
                if not provider:
                    provider = mysql.Provider(db_id,
                                              endpoint=f"{db.endpoint.address}:{db.endpoint.port}",
                                              proxy=(self.model.config.socks_proxy_url or None),
                                              username=db.master_username,
                                              # TODO: use password retrieved from SSM at execution time
                                              password=db.plain_master_password)
                    providers[db_id] = provider
                resource_name = f"{db_id}/{login}"
                mysql_user = mysql.User(resource_name,
                                        user=login,
                                        host="%",
                                        auth_plugin="AWSAuthenticationPlugin",
                                        tls_option="SSL",
                                        opts=pulumi.ResourceOptions(provider=provider))
                self._add_resource(TYPE_MYSQL_USER, login, mysql_user)
                mysql_grant = mysql.Grant(resource_name,
                                          user=login,
                                          database=db_id,
                                          host=user.host,
                                          privileges=grant_types[grant_type],
                                          opts=pulumi.ResourceOptions(
                                              provider=provider,
                                              depends_on=[mysql_user]
                                          ))
                self._add_resource(TYPE_MYSQL_GRANT, login, mysql_grant)

    def synthesize_okta(self):
        okta = self.model.okta
        provider = pulumi_okta.Provider("default", org_name=okta.organization)
        app_id = okta.aws_app.app_id
        for login, user in okta.users.items():
            okta_user = okta_app.User(login,
                                      app_id=app_id,
                                      user_id=user.user_id,
                                      username=login,
                                      profile=json.dumps({
                                          "email": login,
                                          "samlRoles": [f"{RDS_ROLE_PREFIX}{db_id}" for db_id in user.permissions]
                                      }),
                                      opts=pulumi.ResourceOptions(
                                          provider=provider,
                                          depends_on=[self._get_resource(TYPE_AWS_IAM_ROLE, db_id)
                                                      for db_id in user.permissions]
                                      ))
            self._add_resource(TYPE_OKTA_APP_USER, login, okta_user)

    def synthesize_bastion_host(self):
        ssh_pub_keys = [user.ssh_pubkey for user in self.model.okta.users.values()
                        if user.status == "ACTIVE"]
        config = Prodict.from_dict(bastion_host)
        errors = update_authorized_keys(config.hostname, config.admin_username,
                                        os.environ["BH_ADMIN_KEY_FILENAME"],
                                        os.environ["BH_ADMIN_KEY_PASSPHRASE"],
                                        config.proxy_username, ssh_pub_keys,
                                        bastion_host.get("port"))
        if errors:
            logger.error("Errors while updating Bastion Host")
            for err in errors:
                logger.error(err.strip())

    def _add_resource(self, type_: str, name: str, res):
        self._resources[f"{type_}::{name}"] = res

    def _get_resource(self, type_: str, name):
        return self._resources[f"{type_}::{name}"]


def _aws_make_policy(statements: List[dict]) -> str:
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": statements
    })
