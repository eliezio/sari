import random
import re
from concurrent.futures.thread import ThreadPoolExecutor
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import List, Tuple
from urllib.parse import unquote

import boto3
import pytest
import pytz
from httmock import HTTMock, response, urlmatch
from moto import mock_rds2, mock_ssm, mock_sts, mock_ec2
from moto.ec2.utils import random_security_group_id
from moto.iam.models import ACCOUNT_ID
from prodict import Prodict

from main.aws_client import AwsClient
from main.domain import IssueLevel
from main.gatherer.aws import AwsGatherer
from main.gatherer.config import DatabaseConfigGatherer, UserConfigGatherer, ServiceConfigGatherer, \
    ApplicationConfigGatherer
from main.gatherer.dbinfo import DatabaseInfoGatherer
from main.gatherer.okta import OktaGatherer
from main.gatherer.pwd_resolver import MasterPasswordResolver
from main.util import dict_deep_merge, assert_dict_equals

AWS_REGION_US = "us-east-1"
AWS_REGION_UK = "eu-west-2"

AWS_REGIONS = [AWS_REGION_UK, AWS_REGION_US]

# The constants below are valid for moto v1.3.14
MOTO_RDS_FIXED_SUBDOMAIN = "aaaaaaaaaa"
MOTO_RDS_FIXED_RESOURCE_ID = "db-M5ENSHXFPU6XHZ4G4ZEI5QIO2U"

RDS_CONFIG_DATABASES = {
    f"{AWS_REGION_US}/borders": {
        "status": "ENABLED",
        "master_password": "vigilant_swirles",
        "password_age": False,
        "permissions": {},
    },
    f"{AWS_REGION_UK}/blackwells": {
        "status": "ENABLED",
        "master_password": "focused_mendel",
        "password_age": False,
        "permissions": {},
    },
    f"{AWS_REGION_UK}/foyles": {
        "status": "DISABLED",
    },
    f"{AWS_REGION_UK}/whsmith": {
        "status": "ENABLED",
        "master_password": "quirky_ganguly",
        "password_age": False,
        "permissions": {},
    },
}

RDS_INFO_DATABASES = {
    f"{AWS_REGION_US}/borders": {
        "db_name": "db_borders",
        "master_username": "acme",
        "dbi_resource_id": MOTO_RDS_FIXED_RESOURCE_ID,
        "endpoint": {
            "address": f"borders.{MOTO_RDS_FIXED_SUBDOMAIN}.{AWS_REGION_US}.rds.amazonaws.com",
            "port": 3306,
        },
        "availability_zone": f"{AWS_REGION_US}a",
        "vpc_security_group_ids": ["sg-593ad699"],
        "primary_subnet": "subnet-4283fefc",
    },
    f"{AWS_REGION_UK}/blackwells": {
        "db_name": "db_blackwells",
        "master_username": "acme",
        "dbi_resource_id": MOTO_RDS_FIXED_RESOURCE_ID,
        "endpoint": {
            "address": f"blackwells.{MOTO_RDS_FIXED_SUBDOMAIN}.{AWS_REGION_UK}.rds.amazonaws.com",
            "port": 3306,
        },
        "availability_zone": f"{AWS_REGION_UK}a",
        "vpc_security_group_ids": ["sg-593ad699"],
        "primary_subnet": "subnet-4283fefc",
    },
    f"{AWS_REGION_UK}/whsmith": {
        "status": "ABSENT",
    },
}

USERS_CONFIG = {
    "leroy.trent@acme.com": {
        "db_username": "leroy.trent@acme.com",
        "permissions": {
            f"{AWS_REGION_US}/borders": {"db_names": ["db_borders", "ebooks"], "grant_type": "query"},
            f"{AWS_REGION_UK}/blackwells": {"db_names": ["db_blackwells"], "grant_type": "query"},
            f"{AWS_REGION_UK}/whsmith": {"db_names": ["db_whsmith"], "grant_type": "crud"},
        },
    },
    "bridget.huntington-whiteley@acme.com": {
        "db_username": "bridget.huntington-whiteley@acme.com"[:32],
        "permissions": {},
    },
    "valerie.tennant@acme.com": {
        "db_username": "valerie.tennant@acme.com",
        "permissions": {
            f"{AWS_REGION_UK}/blackwells": {"db_names": ["db_blackwells"], "grant_type": "crud"}
        },
    },
}

SERVICES_CONFIG = {
    "glue_connections": {
        f"{AWS_REGION_UK}/blackwells": {
            "db_names": ["db_blackwells"],
            "grant_type": "crud",
            "physical_connection_requirements": {
                "availability_zone": f"{AWS_REGION_UK}a",
                "security_group_id_list": ["sg-593ad699"],
                "subnet_id": "subnet-4283fefc",
            },
        },
    }
}

MASTER_PASSWORD_DEFAULTS = {
    r"([a-z][a-z0-9-]+)": r"ssm:\1.master_password"
}

OKTA_API_TOKEN = "000AmAPPcvEZ8qvjY3vwh7CS6__JrRNatR3XuvaCZx"


def initial_model() -> Prodict:
    return Prodict.from_dict({
        "aws": {
            "regions": [AWS_REGION_US, AWS_REGION_UK],
            "single_region": False,
        },
        "okta": {
            "organization": "acme",
        },
        "applications": {},
        "job": {
            "next_transition": None,
        },
    })


class TestGatherers:

    @mock_sts
    def test_aws_gather_account_info(self):
        # Given:
        aws_gatherer = AwsGatherer(AwsClient(AWS_REGIONS[0]))

        # When:
        resp, issues = aws_gatherer.gather(initial_model())

        # Then:
        assert_dict_equals(resp, {"aws": {"account": str(ACCOUNT_ID)}})

    @mock_ssm
    @pytest.mark.parametrize("region, okay_instances, cfg_error_instances", [
        (AWS_REGION_US, ["borders"], []),
        (AWS_REGION_UK, ["blackwells", "whsmith"], ["daunt-books"])
    ])
    def test_cfg_gather_rds_config(self, region: str,
                                   okay_instances: List[str],
                                   cfg_error_instances: List[str]):
        # Given:
        ssm = boto3.client("ssm", region_name=region)
        for db_id in okay_instances:
            ssm.put_parameter(
                Name=f"{db_id}.master_password",
                Value=RDS_CONFIG_DATABASES[f"{region}/{db_id}"]["master_password"],
                Type="SecureString"
            )
        aws_client = AwsClient(region)
        local_databases = {k: v for k, v in RDS_CONFIG_DATABASES.items() if k.startswith(f"{region}/")}
        pwd_resolver = MasterPasswordResolver(aws_client, MASTER_PASSWORD_DEFAULTS)

        # When:
        resp, issues = DatabaseConfigGatherer(region,
                                              f"tests/data/{region}/databases.yaml",
                                              pwd_resolver).gather(initial_model())

        # Then:
        assert len(issues) == len(cfg_error_instances)
        for index, id_ in enumerate(cfg_error_instances):
            assert issues[index].level == IssueLevel.ERROR
            assert issues[index].type == "DB"
            assert issues[index].id == f"{region}/{id_}"
        assert_dict_equals(resp, {"aws": {"databases": local_databases}})

    @mock_ec2
    @mock_rds2
    @pytest.mark.parametrize("region, present_instances, absent_instances", [
        (AWS_REGION_US, ["borders"], []),
        (AWS_REGION_UK, ["blackwells", "foyles"], ["whsmith"]),
    ])
    def test_aws_gather_rds_info(self, region: str,
                                 present_instances: List[str],
                                 absent_instances: List[str]):
        # Given:
        _create_subnets("db_subnet", region, "10.0.0.0/16", [("a", "10.0.1.0/24"), ("b", "10.0.2.0/24")])
        rds = boto3.client("rds", region_name=region)
        instances = present_instances
        instances.insert(0, "acme-test")
        for index, db_id in enumerate(instances):
            rds.create_db_instance(
                DBInstanceIdentifier=db_id,
                Engine="mysql",
                EngineVersion="5.7.28",
                DBName=f"db_{db_id}",
                MasterUsername="acme",
                DBInstanceClass="db.m1.small",
                MultiAZ=True,
                AvailabilityZone=f"{AWS_REGION_UK}a",
                VpcSecurityGroupIds=[random_security_group_id()],
                DBSubnetGroupName="db_subnet",
            )
        aws = AwsClient(region)
        gatherer = DatabaseInfoGatherer(aws, MasterPasswordResolver(aws, MASTER_PASSWORD_DEFAULTS))
        model = initial_model()
        model.aws["databases"] = Prodict.from_dict(RDS_CONFIG_DATABASES)
        local_databases = {k: v for k, v in RDS_INFO_DATABASES.items() if k.startswith(f"{region}/")}

        # When:
        resp, issues = gatherer.gather(model)

        # Then:
        assert len(issues) == 1 + len(absent_instances)
        assert issues[0].level == IssueLevel.WARNING
        assert issues[0].type == "DB"
        assert issues[0].id == f"{region}/acme-test"
        for index, db_id in enumerate(absent_instances, 1):
            assert issues[index].level == IssueLevel.ERROR
            assert issues[index].type == "DB"
            assert issues[index].id == f"{region}/{db_id}"
        assert_dict_equals(resp, {"aws": {"databases": local_databases}})

    def test_cfg_gather_empty_user_config(self):
        # Given:
        model = initial_model()
        model.aws["databases"] = {}
        user_config = UserConfigGatherer("tests/data/users-empty.yaml")

        # When:
        resp, issues = user_config.gather(model)

        # Then:
        assert_dict_equals(resp, {
            "okta": {
                "users": {},
            },
            "aws": {
                "databases": {},
            },
        })

    def test_cfg_gather_user_config(self):
        # Given:
        model = initial_model()
        model.aws["databases"] = Prodict.from_dict({
            f"{AWS_REGION_US}/borders": {
                "status": "ACCESSIBLE",
                "db_name": "db_borders",
                "master_password": "ssm:borders.master_password",
            },
            f"{AWS_REGION_UK}/blackwells": {
                "status": "ACCESSIBLE",
                "db_name": "db_blackwells",
                "master_password": "ssm:blackwells.master_password",
            },
            f"{AWS_REGION_UK}/foyles": {
                "status": "DISABLED",
            },
            f"{AWS_REGION_UK}/blackwells-recover": {
                "status": "ABSENT",
            },
            f"{AWS_REGION_UK}/whsmith": {
                "status": "ENABLED",
                "db_name": "db_whsmith",
                "master_password": "ssm:whsmith.master_password",
            },
        })
        tz = pytz.timezone("Europe/Dublin")
        time_ref = datetime(2020, 5, 15, 22, 24, 51, tzinfo=tz)
        user_config = UserConfigGatherer("tests/data/users.yaml", time_ref)

        # When:
        resp, issues = user_config.gather(model)

        # Then:
        assert_dict_equals(resp, {
            "job": {
                # 2020-05-26 10:22:00 +01
                "next_transition": datetime(2020, 5, 26, 9, 22, 0, tzinfo=timezone.utc)
            },
            "okta": {
                "users": USERS_CONFIG,
            },
            "aws": {
                "databases": {
                    f"{AWS_REGION_US}/borders": {
                        "permissions": {
                            "leroy.trent@acme.com": {"db_names": ["db_borders", "ebooks"], "grant_type": "query"},
                        },
                    },
                    f"{AWS_REGION_UK}/blackwells": {
                        "permissions": {
                            "leroy.trent@acme.com": {"db_names": ["db_blackwells"], "grant_type": "query"},
                            "valerie.tennant@acme.com": {"db_names": ["db_blackwells"], "grant_type": "crud"},
                        },
                    },
                    f"{AWS_REGION_UK}/whsmith": {
                        "permissions": {
                            "leroy.trent@acme.com": {"db_names": ["db_whsmith"], "grant_type": "crud"},
                        },
                    },
                },
            },
        })

    def test_cfg_gather_user_config_single_region(self):
        # Given:
        model = initial_model()
        model.aws["single_region"] = AWS_REGION_UK
        model.aws["databases"] = Prodict.from_dict({
            f"{AWS_REGION_UK}/blackwells": {
                "status": "ACCESSIBLE",
                "db_name": "db_blackwells",
                "master_password": "ssm:blackwells.master_password",
            },
            f"{AWS_REGION_UK}/whsmith": {
                "status": "ENABLED",
                "db_name": "db_whsmith",
                "master_password": "ssm:whsmith.master_password",
            },
        })
        user_config = UserConfigGatherer(StringIO(f"""
- login: leroy.trent@acme.com
  default_grant_type: query
  permissions:
    - db: "*"
    - db: "whsmith"
      grant_type: crud
        """))

        # When:
        resp, issues = user_config.gather(model)

        # Then:
        assert not issues
        assert_dict_equals(resp, {
            "okta": {
                "users": {
                    "leroy.trent@acme.com": {
                        "db_username": "leroy.trent@acme.com",
                        "permissions": {
                            f"{AWS_REGION_UK}/blackwells": {"db_names": ["db_blackwells"], "grant_type": "query"},
                            f"{AWS_REGION_UK}/whsmith": {"db_names": ["db_whsmith"], "grant_type": "crud"},
                        },
                    },
                },
            },
            "aws": {
                "databases": {
                    f"{AWS_REGION_UK}/blackwells": {
                        "permissions": {
                            "leroy.trent@acme.com": {"db_names": ["db_blackwells"], "grant_type": "query"},
                        },
                    },
                    f"{AWS_REGION_UK}/whsmith": {
                        "permissions": {
                            "leroy.trent@acme.com": {"db_names": ["db_whsmith"], "grant_type": "crud"},
                        },
                    },
                },
            },
        })

    def test_cfg_gather_service_config(self):
        # Given:
        model = initial_model()
        model.aws["databases"] = dict_deep_merge(Prodict.from_dict(RDS_CONFIG_DATABASES), RDS_INFO_DATABASES)
        svc_config = ServiceConfigGatherer("tests/data/services.yaml")

        # When:
        resp, issues = svc_config.gather(model)

        # Then:
        assert len(issues) == 2
        assert issues[0].level == IssueLevel.ERROR
        assert issues[0].type == "GLUE"
        assert issues[0].id == f"{AWS_REGION_UK}/whsmith"
        assert issues[1].level == IssueLevel.ERROR
        assert issues[1].type == "GLUE"
        assert issues[1].id == f"{AWS_REGION_UK}/foyles"

        assert_dict_equals(resp, {"aws": SERVICES_CONFIG})

    def test_cfg_gather_applications_config(self):
        # Given:
        model = initial_model()
        model.aws["databases"] = dict_deep_merge(Prodict.from_dict(RDS_CONFIG_DATABASES), RDS_INFO_DATABASES)
        app_config = ApplicationConfigGatherer("tests/data/applications.yaml")

        # When:
        resp, issues = app_config.gather(model)

        # Then:
        assert len(issues) == 0
        assert_dict_equals(resp, {"applications": {
            "monitoring": ["us-east-1/borders", "eu-west-2/blackwells"]
        }})

    def test_okta_gather_no_users_info(self):
        # Given:
        model = initial_model()
        model.okta["users"] = {}

        # When:
        with ThreadPoolExecutor(max_workers=1) as executor:
            okta_gatherer = OktaGatherer(OKTA_API_TOKEN, executor)
            resp, issues = okta_gatherer.gather(model)

        assert len(issues) == 0
        assert_dict_equals(resp, {
            "okta": {
                "users": {},
            }
        })

    def test_okta_gather_user_info(self):
        # Given:
        model = initial_model()
        model.okta.update(Prodict(users=USERS_CONFIG))
        model.okta.users["tracy.mickelsen@acme.com"] = {
            "db_username": "tracy.mickelsen@acme.com",
            "permissions": {},
        }
        model.okta.users["miguel.heidler@acme.com"] = {
            "db_username": "miguel.heidler@acme.com",
            "permissions": {},
        }

        query_prefix = r"^limit=1&search=profile\.login\+eq\+"

        @urlmatch(scheme="https", netloc="acme.okta.com",
                  path=r"^/api/v1/users",
                  query=query_prefix)
        def okta_user_info(url, request):
            assert request.headers["Authorization"] == f"SSWS {OKTA_API_TOKEN}"
            m = re.match(query_prefix + r'"(.*)@acme\.com"$', unquote(url.query))
            assert m
            username = m.group(1)
            user_file = Path(f"tests/data/users/{username}.json")
            content = user_file.read_text() if user_file.exists() else "[]"
            return response(status_code=200,
                            content=content,
                            headers={
                                "Content-Type": "application/json"
                            })

        # When:
        with ThreadPoolExecutor(max_workers=1) as executor:
            okta_gatherer = OktaGatherer(OKTA_API_TOKEN, executor)
            with HTTMock(okta_user_info):
                resp, issues = okta_gatherer.gather(model)

        # Then:
        assert len(issues) == 3
        assert issues[0].level == IssueLevel.ERROR
        assert issues[0].type == "USER"
        assert issues[0].id == "valerie.tennant@acme.com"
        assert issues[1].level == IssueLevel.ERROR
        assert issues[1].type == "USER"
        assert issues[1].id == "tracy.mickelsen@acme.com"
        assert issues[2].level == IssueLevel.ERROR
        assert issues[2].type == "USER"
        assert issues[2].id == "miguel.heidler@acme.com"
        assert_dict_equals(resp, {"okta": {"users": {
            "valerie.tennant@acme.com": {
                "status": "MISSING_SSH_PUBKEY",
            },
            "miguel.heidler@acme.com": {
                "status": "DEPROVISIONED",
            },
            "leroy.trent@acme.com": {
                "status": "ACTIVE",
                "user_id": "00m6q2lgisjgmFq64772",
                "ssh_pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEfzjdkO1LKnS/it62jmw9tH4BznlnDCBrzaKguujJ15 "
                              "leroy.trent@acme.com",
            },
            "bridget.huntington-whiteley@acme.com": {
                "status": "ACTIVE",
                "user_id": "00u4subrvCRYYe2dx765",
                "ssh_pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGD13Dbe1QoYrFZqCue1TzGkzDSra9ZHzv8gZy9+vb0Y "
                              "bridget.huntington-whiteley@acme.com",
            },
            "tracy.mickelsen@acme.com": {
                "status": "ABSENT",
            }
        }}})


def _create_subnets(name: str,
                    region: str,
                    vpc_cidr: str,
                    subnet_defs: List[Tuple[str, str]]):
    ec2 = boto3.client("ec2", region_name=region)
    vpc = ec2.create_vpc(CidrBlock=vpc_cidr)["Vpc"]
    # to force a deterministic sequence of pseudo random numbers
    random.seed(1)
    subnet_ids = [ec2.create_subnet(VpcId=vpc["VpcId"],
                                    CidrBlock=cidr,
                                    AvailabilityZone=f"{region}{az_id}")["Subnet"]["SubnetId"]
                  for az_id, cidr in subnet_defs]

    rds = boto3.client("rds", region_name=region)
    rds.create_db_subnet_group(
        DBSubnetGroupName=name,
        DBSubnetGroupDescription="my db subnet",
        SubnetIds=subnet_ids,
    )
