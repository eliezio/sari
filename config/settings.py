settings = {
    "aws": {
        "region": "us-west-2",
    },
    "okta": {
        "organization": "acme",
        "aws_app": {
            "iam_user": "OktaSSOUser",
            "label": "Amazon Web Services",
        },
    },
}

grant_types = {
    "query": ["SELECT"],
    "crud": ["SELECT", "UPDATE", "INSERT", "DELETE"],
}

bastion_host = {
    "hostname": "127.0.0.1",
    "admin_username": "admin",
    "proxy_username": "acme",
}

master_password_patterns = {
    r"([a-z][a-z0-9-]+)": r"ssm:\1.master_password",
}
