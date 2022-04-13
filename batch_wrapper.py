# MIT License
# Copyright (c) 2019-2022, Fabian Weiersmüller, Switzerland

# Standard library imports
import asyncio
import functools
import logging
import secrets
import string
import time
from collections import defaultdict
from typing import Optional

# third party imports
import aiohttp

__pending_requests = {}
__pending_time_watch = set()  # see: https://bugs.python.org/issue44665, we need to keep refs.
__pending_batches = set()  # same as above
__session: Optional[aiohttp.ClientSession] = None

__expected_params = ['method', 'endpoint', 'parameter', 'header', 'body']
__timeout_watchdog = asyncio.Event()

MAX_WAIT = 0.6  # max wait time for a single request before we create a batch
module_logger = logging.getLogger(__name__)


def batched(http_request_func):
    """
    :param http_request_func: async function that calls the ms graph endpoint
    :return: wrapped version of the function, using JSON batching to speed it up.
    """

    @functools.wraps(http_request_func)
    async def wrapper(*args, **kwargs):
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        try:
            request_args = create_request_dict(*args, **kwargs)
        except Exception as ex:
            fut.set_exception(ex)
            return fut

        try:
            check_request_args(request_args)
        except Exception as ex:
            fut.set_exception(ex)
            return fut

        add_pending_request(fut, request_args)  # function takes care of future management
        return await fut

    return wrapper


def create_request_dict(*args, **kwargs):
    """Takes the positional arguments and converts them into keyword arguments. Then returns the dict"""

    request_args = dict(**kwargs)
    for i in range(len(args)):
        pos_arg = __expected_params[i]
        if pos_arg in request_args:
            raise TypeError(f'Got multiple values for argument {pos_arg}')
        else:
            request_args.update({pos_arg: args[i]})
    for arg in __expected_params:
        if arg not in request_args:
            request_args.update({arg: None})

    return request_args


def check_request_args(request_args: dict):
    """Checks if the parsed argument dict is valid."""

    res = set(request_args.keys()).difference(set(__expected_params))
    if len(res) > 0:
        raise ValueError(f'Unknown argument {res.pop()}')
    for key, value in request_args.items():
        if key == 'method':
            if not isinstance(value, str):
                raise TypeError(f'Wrong type for parameter \'method\'. str expected, found {value!r}')

            if value.upper() not in ['GET', 'POST', 'PATCH', 'DELETE', 'PUT']:
                raise ValueError(f'Unsupported HTTP verb {value}')
        elif key == 'endpoint':
            if not isinstance(value, str):
                raise TypeError(f'Wrong type for parameter \'endpoint\'. str expected, found {value!r}')

            if not (value.startswith('https://graph.microsoft.com/v1.0') or
                    value.startswith('https://graph.microsoft.com/beta')):
                raise ValueError('\'endpoint\' argument must specify an absolute URL, starting with either '
                                 '\'https://graph.microsoft.com/v1.0\' or \'https://graph.microsoft.com/beta\'. '
                                 f'Got \'{value}\'')
        elif key == 'parameter':
            if not (isinstance(value, dict) or value is None):
                raise TypeError(f'Wrong type for parameter \'parameter\'. dict or None expected, found {value!r}')
        elif key == 'header':
            if not isinstance(value, dict):
                raise TypeError(f'Wrong type for parameter \'header\'. dict expected, found {value!r}')

            # When a body is included with the request, the headers object must contain a value for Content-Type
            if 'Content-Type' not in value:
                raise ValueError(f'Header must contain \'Content-Type\' - entry.')
        elif key == 'body':
            if not (isinstance(value, dict) or value is None):
                raise TypeError(f'Wrong type for parameter \'body\'. dict or None expected, found {value!r}')
        else:
            raise ValueError(f'Unknown argument {key}')
    return


def add_pending_request(future: asyncio.Future, request_args: dict):
    """Add job to pending_job dictionary. If 20 entries are present, schedules the creation of a batch request.
    Uses time.monotonic() to schedule batch if there are not 20 requests present after a certain amount of time,
    usage of  time.monotonic() is in accordance with https://www.python.org/dev/peps/pep-0418/"""

    while True:
        new_id = create_random_string(10)
        if new_id not in __pending_requests:
            break
    __pending_requests[new_id] = {'future': future, 'request_args': request_args, 'age': time.monotonic()}
    module_logger.debug(f'Add http_request job to pending dict. Current length: {len(__pending_requests)}.')

    if not __timeout_watchdog.is_set():
        __timeout_watchdog.set()
        tsk = asyncio.create_task(check_age())
        __pending_time_watch.add(tsk)
        tsk.add_done_callback(lambda finished_tsk: __pending_time_watch.remove(finished_tsk))

    if len(__pending_requests) % 20 - 1 == 0:  # schedule at 21, 41, etc.
        tsk = asyncio.create_task(make_batch_call())
        __pending_batches.add(tsk)
        tsk.add_done_callback(lambda finished_tsk: __pending_batches.remove(finished_tsk))


async def make_batch_call():
    """Make a batch call to ms_graph, bundling up to 20 requests. If batch call fails, retries the calls individually"""

    # python 3.7+: Dictionary iteration order is guaranteed to be in order of insertion.
    # So no job will wait indefinitely in here
    batch_list = []
    sent_jobs = {}

    auth_bearer = get_auth_from_pending()

    for i, (u_id, job_dict) in enumerate(__pending_requests.items()):
        if not job_dict['request_args']['header'].get('Authorization', '') == auth_bearer:
            continue

        sent_jobs[u_id] = job_dict
        batch_list.append(
            {'id': u_id, 'method': job_dict['request_args']['method'].upper(),
             'url': make_url(job_dict['request_args']), 'headers': job_dict['request_args']['header'],
             'body': job_dict['request_args']['body']})
        if len(batch_list) == 20:
            break

    for single_batch_dict in batch_list:
        del __pending_requests[single_batch_dict['id']]

    module_logger.debug(f'Sending batch request with {len(batch_list)} entries to $batch - endpoint.')

    try:
        batch_status, batch_response_body = await aiohttp_request(
            method='POST', endpoint='https://graph.microsoft.com/v1.0/$batch', parameter={},
            header={'Content-Type': 'application/json', 'Authorization': auth_bearer}, body={'requests': batch_list})
    except Exception as ex:
        module_logger.warning(f'Batch call failed with exception {ex}. Retry individual calls.')
        tasks = [aiohttp_request(**job_dict['request_args']) for job_dict in sent_jobs.values()]
        res = await asyncio.gather(*tasks, return_exceptions=True)
        for r, job_dict in zip(res, sent_jobs.values()):
            if isinstance(r, Exception):
                job_dict['future'].set_exception(r)
            else:
                job_dict['future'].set_result(r)
        return

    if _is_valid_batch_response(batch_response_body):
        for res in batch_response_body['responses']:
            u_id = res['id']
            status = int(res['status'])
            body = res['body']
            job_dict = sent_jobs[u_id]
            job_dict['future'].set_result((status, body))
    else:
        module_logger.warning(
            'Response from ms graph did not comply with our expectations about responses from the $batch endpoint.')
        ex = ValueError(
            f'Response from MS graph could not be processed ({_is_valid_batch_response(batch_response_body)=}).',
            f'status code: {batch_status}. response body: {batch_response_body!r}')
        for job_dict in sent_jobs.values():
            job_dict['future'].set_exception(ex)


def get_auth_from_pending() -> str:
    """
    The JSON batch call needs to have the the same or more privileges than the requests it contains,
    so we use the Auth.-Token in the individual requests as Token for the batch
    :return:
    """
    counting_dict = defaultdict(int)
    for u_id, job_dict in __pending_requests.items():
        auth_bearer = job_dict['request_args']['header'].get('Authorization', '')
        counting_dict[auth_bearer] += 1
    most_common_auth, count = sorted(list(counting_dict.items()), key=lambda tup: tup[1], reverse=True)[0]
    return most_common_auth


async def check_age():
    """
    Checks that no individual request stays in the pending - dictionary for too long
    :return: void
    """
    while len(__pending_requests) > 0:
        oldest = min([job['age'] for job in __pending_requests.values()])
        if time.monotonic() - oldest > MAX_WAIT:
            fut = asyncio.create_task(make_batch_call())

            # if you're not waiting, there may be cases where
            # we call make_batch_call() multiple times for the same request
            await asyncio.wait_for(fut, timeout=None)
            # we could just call fut.result(), since cf.Future.result() is blocking,
            # but I want to release the event loop
        else:  # wait till the oldest would need to be processed, and some more
            await asyncio.sleep(MAX_WAIT - (time.monotonic() - oldest) + 0.1)
    __timeout_watchdog.clear()


def make_url(request_args: dict) -> str:
    """URL-encodes the query parameters for a single http_request into to url of the provided endpoint
     and makes it relative, as required for JSON-batching for ms graph"""
    query_params = request_args['parameter']
    endpoint = request_args['endpoint']
    absolute_url_starts = ['https://graph.microsoft.com/v1.0', 'https://graph.microsoft.com/beta']
    for url_s in absolute_url_starts:
        if endpoint.startswith(url_s):
            endpoint = endpoint.replace(url_s, '', 1)  # just replace the first occurrence

    if query_params is None or len(query_params) == 0:
        module_logger.debug(f'Transformed endpoint for inside batch call. Old endpoint: {request_args["endpoint"]} '
                            f'with no query parameters given --> new endpoint: {endpoint}')
        return endpoint

    concatenated_params = '&'.join([f'{param_name}={param_val}' for param_name, param_val in query_params.items()])
    endpoint = f'{endpoint}?{concatenated_params}'
    module_logger.debug(f'Transformed endpoint for inside batch call. Old endpoint: {request_args["endpoint"]} '
                        f'with with query parameter {query_params} --> new endpoint: {endpoint}')
    return endpoint


def _is_valid_batch_response(response_body: dict) -> bool:
    """
    Helper function to bundle the assumptions on how the response from the ms graph /$batch endpoint looks like
    The body should contain a 'responses' entry, containing a List of dicts.

    Each individual response dict should contain 'id', 'status', 'headers' and 'body' entries.

    If the request succeeded, we expect the 'body' entry in the response to contain whatever it is ms graph returns
    for this particular call. So we just check that a 'body' entry is present.

    If an error occurred, the 'body' entry should be a dict with a single key-value pair,
    'error': {}. Within there, we find 'code', 'message', 'details' and an 'innerError'

    :param response_body: a response body from the ms graph /$batch endpoint
    :return: bool, indicating if the response_body is structured in the expected way
    """

    if not isinstance(response_body, dict):
        return False

    try:
        responses = response_body['responses']
    except KeyError:
        return False

    expected_response_entries = ['id', 'status', 'body']
    for single_response in responses:
        if not isinstance(single_response, dict):
            return False

        for key in expected_response_entries:
            if key not in single_response:
                return False

        if not 199 < int(single_response['status']) < 300:  # Error
            if 'error' not in single_response['body']:
                return False
            if 'message' not in single_response['body']['error']:
                return False

    return True


def create_random_string(n=8) -> str:
    """
    This function creates a cryptographically secure string, following the best practice advise from here:
    https://docs.python.org/3/library/secrets.html
    Additionally, for the case in which the string is used as password, no vowels are used
    (to not accidentally create offensive words) nor hard to distinguish characters.
    :param n: int, length of the password created. Standard is 8 chars
    :return: random string of length 'n'
    """
    alphabet = list(set(string.ascii_letters).union(set(string.digits)).difference(
        {'I', 'l', 'O', 'o', '0', 'a', 'e', 'i', 'u', 'A', 'E', 'U'}))
    while True:
        random_str = ''.join(secrets.choice(alphabet) for _ in range(n))
        if any(c.islower() for c in random_str) \
                and any(c.isupper() for c in random_str) \
                and any(c.isdigit() for c in random_str):
            break

    return random_str


async def _get_session() -> aiohttp.ClientSession:
    """
    Session wants to be created asynchronous.
    Reason: aiohttp.ClientSession - objects are bound to the asyncio - loop. If the current loop changes,
    the application hangs.
    https://github.com/aio-libs/aiohttp/blob/master/docs/faq.rst#why-is-creating-a-clientsession-outside-of-an-event-loop-dangerous
    :return: returns and, if its called the first time, also creates the aiohttp.ClientSession session.
    """
    global __session
    if __session is None:
        __session = aiohttp.ClientSession()
    return __session


async def aiohttp_request(method: str, endpoint: str, parameter: dict, header: dict, body: dict) -> tuple[int, dict]:
    """
    :param method: 'GET', 'POST', 'PATCH', 'DELETE', 'PUT'
    :param endpoint: the url to the ms_graph endpoint
    :param parameter: json of parameter that will be URL-encoded, if one wishes to specify them
    :param header: header of the http call
    :param body: body of the http call, a regular python dict. Do NOT json.dumps(...) it,
                 since it will be converted into a JSON-conforming form by the requests module
    :return: Tuple of (status_code, response_body)
    """
    # Quote from requests-quickstart:
    # "Nearly all production code should use the timeout parameter in nearly all requests"
    # Even though we are using asyncIO (aka, another library), I still use this advice,
    timeout = aiohttp.ClientTimeout(total=20)
    session = await _get_session()
    try:
        # Difference between data and json parameter: json makes a conversion for you!
        # data=json.dumps(body) and json=body are equal. But if you choose data, you have to
        # set the content-type in the header yourself, while if you choose json, it is set for you to
        # application/json , while the former, usually, uses content-type: application/x-www-form-urlencoded
        # by default
        response = await session.request(
            method=method, url=endpoint, headers=header, params=parameter, json=body, timeout=timeout)
    except asyncio.TimeoutError as ex:
        # do whatever you want
        raise ex
    except aiohttp.ClientConnectionError as ex:
        # do whatever you want
        raise ex
    except Exception as ex:
        # do whatever you want
        raise ex

    async with response:  # after exiting the with, the response is released back to the connection pool
        try:
            body = await response.json(content_type=response.content_type)
        except aiohttp.ContentTypeError as ex:
            # do whatever you want
            raise ex

        return response.status, body


# The batched version of the aiohttp request function.
batched_request = batched(aiohttp_request)
