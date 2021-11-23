import gzip
import logging
import tempfile
import boto3
import socket
import ssl
import re
import urllib
import csv
import json
import certifi
import os
from uuid import UUID


logger = logging.getLogger()
logger.setLevel(logging.INFO)

logger.info('Loading function...')

s3 = boto3.client('s3')

REGION = os.environ.get('region')
ENDPOINT = f'{REGION}.data.logs.insight.rapid7.com'
PORT = 20000
TOKEN = os.environ.get('token')


def lambda_handler(event, context):
    sock = create_socket()

    if not validate_uuid(TOKEN):
        logger.critical(f'{TOKEN} is not a valid token. Exiting.')
        raise SystemExit
    else:
        # Get the object from the event and show its content type
        bucket = event['Records'][0]['s3']['bucket']['name']
        key = urllib.parse.unquote_plus(event['Records'][0]['s3']['object']['key'])
        try:
            response = s3.get_object(Bucket=bucket, Key=key)
            logger.info(f'Fetched file {key} from S3 bucket {bucket}')
            body = response['Body']

            # If the name has a .gz extension, then decompress the data
            if key[-3:] == '.gz':
                with tempfile.TemporaryFile() as temporary_file:
                    temporary_file.write(body.read())
                    temporary_file.seek(0)

                    with gzip.GzipFile(fileobj=temporary_file, mode="r") as gz:
                        data = gz.read().decode('utf-8')
            else:
                data = body.read().decode('utf8')

            lines = data.split("\n")
            logger.info(f'Total number of lines: {len(list(lines))}')

            if validate_elb_log(str(key)) is True:
                # timestamp elb client:port backend:port request_processing_time backend_processing_time
                # response_processing_time elb_status_code backend_status_code received_bytes sent_bytes
                # "request" "user_agent" ssl_cipher ssl_protocol
                logger.info(f'File={key} is AWS ELB log format. Parsing and sending to R7')
                rows = csv.reader(data.splitlines(), delimiter=' ', quotechar='"')
                for line in rows:
                    request = line[11].split(' ')
                    idx = request[1].find('/', 9)
                    url = request[1][idx:]
                    parsed = {
                        'timestamp': line[0],
                        'elb_name': line[1],
                        'client_ip': line[2].split(':')[0],
                        'backend_ip': line[3].split(':')[0],
                        'request_processing_time': line[4],
                        'backend_processing_time': line[5],
                        'response_processing_time': line[6],
                        'elb_status_code': line[7],
                        'backend_status_code': line[8],
                        'received_bytes': line[9],
                        'sent_bytes': line[10],
                        'method': request[0],
                        'url': url,
                        'user_agent': line[12],
                        'ssl_cipher': line[13],
                        'ssl_protocol': line[14]
                    }
                    msg = json.dumps(parsed)
                    message = f'{TOKEN} {msg}\n'
                    sock.sendall(message.encode('utf-8'))
                logger.info(f'Finished sending file={key} to R7')
            elif validate_alb_log(str(key)) is True:
                logger.info(f'File={key} is AWS ALB log format. Parsing and sending to R7')
                rows = csv.reader(data.splitlines(), delimiter=' ', quotechar='"')
                total_run_count = 0
                good_run_count = 0
                bad_run_count = 0
                for line in rows:
                    total_run_count += 1
                    try:
                        request = line[12].split(' ')
                        url = request[1]
                        parsed = {
                            'type': line[0],
                            'timestamp': line[1],
                            'elb_id': line[2],
                            'client_ip': line[3].split(':')[0],
                            'client_port': line[3].split(':')[1],
                            'target_ip': line[4].split(':')[0],
                            'target_port': line[4].split(':')[1],
                            'request_processing_time': line[5],
                            'target_processing_time': line[6],
                            'response_processing_time': line[7],
                            'elb_status_code': line[8],
                            'target_status_code': line[9],
                            'received_bytes': line[10],
                            'sent_bytes': line[11],
                            'method': request[0],
                            'url': url,
                            'http_version' :request[2],
                            'user_agent': line[13],
                            'ssl_cipher': line[14],
                            'ssl_protocol': line[15],
                            'target_group_arn': line[16],
                            'trace_id': line[17]
                        }
                        msg = json.dumps(parsed)
                        message = f'{TOKEN} {msg}\n'
                        sock.sendall(message.encode('utf-8'))
                        good_run_count += 1
                    except IndexError:
                        bad_run_count += 1
                        logger.info(f'[ALB logs] bad log line: {line}')
                        pass
                logger.info(f'[ALB logs] total run count: {total_run_count}')
                logger.info(f'[ALB logs] processed-and-sent run count: {good_run_count}')
                logger.info(f'[ALB logs] bad run count: {bad_run_count}')
                logger.info(f'Finished sending file={key} to R7')
            elif validate_cf_log(str(key)) is True:
                # date time x-edge-location sc-bytes c-ip cs-method cs(Host)
                # cs-uri-stem sc-status cs(Referer) cs(User-Agent) cs-uri-query
                # cs(Cookie) x-edge-result-type x-edge-request-id x-host-header
                # cs-protocol cs-bytes time-taken x-forwarded-for ssl-protocol
                # ssl-cipher x-edge-response-result-type
                logger.info(f'File={key} is AWS CloudFront log format. Parsing and sending to R7')
                rows = csv.reader(data.splitlines(), delimiter='\t', quotechar='"')
                for line in rows:
                    # Skip headers and lines with insufficient values
                    if len(line) < 23:
                        continue
                    msg = "\"{0}T{1}Z\" x_edge_location=\"{2}\"" \
                          " sc_bytes=\"{3}\" c_ip=\"{4}\" cs_method=\"{5}\"" \
                          " cs_host=\"{6}\" cs_uri_stem=\"{7}\" sc_status=\"{8}\"" \
                          " cs_referer=\"{9}\" cs_user_agent=\"{10}\" cs_uri_query=\"{11}\"" \
                          " cs_cookie=\"{12}\" x_edge_result_type=\"{13}\"" \
                          " x_edge_request_id=\"{14}\" x_host_header=\"{15}\"" \
                          " cs_protocol=\"{16}\" cs_bytes=\"{17}\" time_taken=\"{18}\"" \
                          " x_forwarded_for=\"{19}\" ssl_protocol=\"{20}\"" \
                          " ssl_cipher=\"{21}\" x_edge_response_result_type=\"{22}\"\n" \
                        .format(*line)
                    message = f'{TOKEN} {msg}\n'
                    sock.sendall(message.encode('utf-8'))
                logger.info(f'Finished sending file={key} to R7')
            elif validate_ct_log(str(key)) is True:
                logger.info(f'File={key} is AWS CloudTrail log format. Parsing and sending to R7')
                cloud_trail = json.loads(data)
                for event in cloud_trail['Records']:
                    message = f'{TOKEN} {json.dumps(event)}\n'
                    sock.sendall(message.encode('utf-8'))
                logger.info(f'Finished sending file={key} to R7')
            else:
                logger.info(f'File={key} is unrecognized log format. Sending raw lines to R7')
                for line in lines:
                    message = f'{TOKEN} {line}\n'
                    sock.sendall(message.encode('utf-8'))
                logger.info(f'Finished sending file={key} to R7')
        except Exception as e:
            logger.error(f'Exception: {e}')
            raise e
        finally:
            sock.close()
            logger.info('Function execution finished.')


def create_socket():
    logger.info('Creating SSL socket')
    s_ = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s = ssl.wrap_socket(
        sock=s_,
        keyfile=None,
        certfile=None,
        server_side=False,
        cert_reqs=ssl.CERT_REQUIRED,
        ssl_version=getattr(
            ssl,
            'PROTOCOL_TLSv1_2',
            ssl.PROTOCOL_TLSv1
        ),
        ca_certs=certifi.where(),
        do_handshake_on_connect=True,
        suppress_ragged_eofs=True,
    )
    try:
        logger.info(f'Connecting to {ENDPOINT}:{PORT}')
        s.connect((ENDPOINT, PORT))
        return s
    except socket.error as exc:
        logger.error(f'Exception socket.error : {exc}')
        raise SystemExit


def validate_uuid(uuid_string):
    try:
        UUID(uuid_string)
    except Exception as uuid_exc:
        logger.error(f'Can not validate token: {uuid_exc}')
        return False
    return True


def validate_elb_log(key):
    regex = re.compile('\d+_\w+_\w{2}-\w{4,9}-[12]_.*._\d{8}T\d{4}Z_\d{1,3}.\d{1,3}.\d{1,3}.\d{1,3}_.*.log$', re.I)
    match = regex.search(key)
    return bool(match)


def validate_alb_log(key):
    regex = re.compile('\d+_\w+_\w{2}-\w{4,9}-[12]_.*._\d{8}T\d{4}Z_\d{1,3}.\d{1,3}.\d{1,3}.\d{1,3}_.*.log.gz$', re.I)
    match = regex.search(key)
    return bool(match)


def validate_cf_log(key):
    regex = re.compile('\w+\.\d{4}-\d{2}-\d{2}-\d{2}\.\w+\.gz$', re.I)
    match = regex.search(key)
    return bool(match)


def validate_ct_log(key):
    regex = re.compile('\d+_CloudTrail_\w{2}-\w{4,9}-[12]_\d{8}T\d{4}Z.+.json.gz$', re.I)
    match = regex.search(key)
    return bool(match)
