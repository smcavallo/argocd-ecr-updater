import os
import base64
import boto3
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from botocore.exceptions import ClientError, NoCredentialsError, NoRegionError
from bottle import route, run, template, response
from kubernetes import client, config
from prometheus_client import generate_latest, REGISTRY, Gauge, Counter


# CONFIGURATION
ARGOCD_ECR_UPDATER_SYNC_CRON = os.getenv('ARGOCD_ECR_UPDATER_SYNC_CRON', '0 */12 * * *')
ARGOCD_REPO_SECRET_NAME = os.getenv('ARGOCD_REPO_SECRET_NAME', None)
ARGOCD_ECR_REGISTRY = os.getenv('ARGOCD_ECR_REGISTRY', None)

# PROMETHEUS METRICS
CREDENTIAL_FAILURE = Counter('argocd_ecr_updater_aws_cred_failure_total', 'Failed Aws Credentials')
ARGO_ECR_UPDATER_SUCCESS = Counter('argocd_ecr_updater_success_total', 'Successful Updates')
ARGO_ECR_UPDATER_FAILURE = Counter('argocd_ecr_updater_failure_total', 'Failed Updates')
IN_PROGRESS = Gauge("argocd_ecr_updater_inprogress_requests", "Currently executing requests")
REQUESTS = Counter('argocd_ecr_updater_http_requests_total', 'Description of counter', ['method', 'endpoint'])


@IN_PROGRESS.track_inprogress()
@route('/')
def home():
    REQUESTS.labels(method='GET', endpoint="/").inc()
    body = '''<html>
            <head><title>argocd-ecr-updater</title></head>
            <body>
            <h1>argocd-ecr-updater</h1>
            <p><a href="/metrics">Metrics</a></p>
            <p><a href="/update">Manually Refresh Token</a></p>
            </body>
            </html>
    '''
    return template(body)


@IN_PROGRESS.track_inprogress()
@route('/metrics')
def metrics():
    REQUESTS.labels(method='GET', endpoint="/metrics").inc()
    data = generate_latest(REGISTRY)
    response.content_type = 'text/plain; version=0.0.4; charset=utf-8'
    return data


@route('/update', method='POST')
def sync():
    REQUESTS.labels(method='POST', endpoint="/update").inc()
    run_update_job()


if ARGOCD_REPO_SECRET_NAME is None:
    raise ValueError('Specify name of secret in env variable ARGOCD_REPO_SECRET_NAME')


def get_ecr_client():
    try:
        session = boto3.session.Session()
        aws_client = session.client(
            service_name='ecr'
        )

    except (ClientError, NoCredentialsError, NoRegionError) as e:
        CREDENTIAL_FAILURE.inc()
        print(e)
        return None
    else:
        return aws_client


def get_ecr_login():
    client = get_ecr_client()
    # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ecr.html#ECR.Client.get_authorization_token
    # Optionally provide registryIds, else the default registry will be used
    if ARGOCD_ECR_REGISTRY is not None:
        response = client.get_authorization_token(registryIds=[ARGOCD_ECR_REGISTRY])
    else:
        response = client.get_authorization_token()
    token = response['authorizationData'][0]['authorizationToken']
    print("New ecr authorizationToken expiresAt " + response['authorizationData'][0]['expiresAt'].strftime("%m/%d/%Y, %H:%M:%S"))
    decoded_token = base64.b64decode(token).decode('utf-8')
    registry_username = decoded_token.split(':')[0]
    registry_password = decoded_token.split(':')[1]
    return registry_username, registry_password


def run_update_job():
    """
    This is the main part of this entire application
    * It retrieves an updated ECR token
    * It base64 encrypts the token and creates a json patch for the password
    * It applies the json patch to the kubernetes secret
    """

    # Get updated credentials
    ecr_username, ecr_password = get_ecr_login()

    # Get a kubernetes config which points to the cluster this application is running inside
    config.load_incluster_config()
    v1 = client.CoreV1Api()

    # base64 encode the password
    ecr_password_base64 = base64.b64encode(ecr_password.encode('utf-8')).decode('utf-8')

    # Create a json patch
    body = {
                'data': {
                    'password': ecr_password_base64
            }
    }
    print("Updating Secret " + ARGOCD_REPO_SECRET_NAME)
    try:
        # Update the secret in kubernetes with the json patched password
        v1.patch_namespaced_secret(ARGOCD_REPO_SECRET_NAME, "argocd", body)
        ARGO_ECR_UPDATER_SUCCESS.inc()
    except Exception as e:
        print(str(e))
        ARGO_ECR_UPDATER_FAILURE.inc()


# Do not start the webserver for unit tests
if __name__ == "__main__":
    sched = BackgroundScheduler(daemon=True)
    sched.add_job(run_update_job, CronTrigger.from_crontab(ARGOCD_ECR_UPDATER_SYNC_CRON))
    sched.start()
    # Run an initial token refresh
    run_update_job()
    run(port=8080, host='0.0.0.0')
