# ms-graph-batched
The [Microsoft graph API](https://docs.microsoft.com/en-us/graph/overview) supports 
[JSON batching](https://docs.microsoft.com/en-us/graph/json-batching) 
to combine multiple API requests into one. While this can lead to drastically reduced network latency, 
it also leads to very cumbersome function signatures and / or bloated function definitions.

This project aims to solve this problem. It provides an async Python wrapper to automatically apply 
JSON batching to all requests. Code can be written _as if_ no JSON batching was used.

## Why should I care ?

For the most basic operations, MS graph often provides ways to natively combine multiple requests.
For example, you can already [add multiple members to a group](https://docs.microsoft.com/en-us/graph/api/resources/group?view=graph-rest-1.0) 
in one PATCH request. However, firstly, this is only provided for the most basic requests. Secondly,
it provides very little flexibility if for some reason some part of the combined request fails.
In the above example, if one request fails, so do all the others, and the entire PATCH request returns 
with an error.

For the most part, no native support for complex combined requests exists, 
and you would have to create the JSON batch yourself.


As a simple, basic example, say you want to get the members and owners of 3 groups:

## How definitely not to do it:

The naive approach is to just use the requests library and be done. While this leads to nice, short code, 
it's not production-viable since every request is handled synchronously.

```Python
import requests
from collections import defaultdict

def get_auth_header_from_somewhere():
    return {}

group_ids = ['11111111-2222-3333-4444-555555555555', 
             '11111111-2222-3333-4444-666666666666',
             '11111111-2222-3333-4444-777777777777']

groups = defaultdict(dict)
ms_graph_header = get_auth_header_from_somewhere()
for g_id in group_ids:
    groups[g_id]['members'] = requests.get(f'https://graph.microsoft.com/v1.0/groups/{g_id}/members', headers=ms_graph_header)
    groups[g_id]['owners'] = requests.get(f'https://graph.microsoft.com/v1.0/groups/{g_id}/owners',  headers=ms_graph_header)
    
```


## The 'not-so-bad-but-still-terrible' way:

The much better way to do things would be to use asyncio and aiohttp to make the requests asynchronously,
and use JSON batching to even further lower network latency.
This way, many thousand requests to ms graph can be sent before even noticing network latency.
(You will need to follow Microsoft's guidelines 
[concerning throttling](https://docs.microsoft.com/en-us/graph/throttling), though.)

```Python
import aiohttp
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Generator, Optional

__session: Optional[aiohttp.ClientSession] = None
groups = defaultdict(dict)

@dataclass
class Request:
    method: str  # 'GET', 'POST', 'PATCH', 'DELETE', 'PUT'
    endpoint: str  # the url to the ms_graph endpoint
    params: dict  #json of parameter that will be URL-encoded
    header: dict  # header for the http call
    body: dict  # body for the http call, a regular python dict. Do NOT json.dumps(...) it

async def _get_session() -> aiohttp.ClientSession:
    """
    Session wants to be created asynchronous.
    Reason: aiohttp.ClientSession - objects are bound to the asyncio - loop. If the current loop changes,
    the application hangs.
    https://github.com/aio-libs/aiohttp/blob/master/docs/faq.rst#why-is-creating-a-clientsession-outside-of-an-event-loop-dangerous
    """
    global __session
    if __session is None:
        __session = aiohttp.ClientSession()
    return __session


def _create_batches(requests: list[Request], ids=None) -> Generator[list[dict[str, str]], None, None]:
    """
    Generator, combines up to 20 requests in one batch-request. 
    """
    # URIs in batch requests always need to be relative.
    absolute_url_starts = ['https://graph.microsoft.com/v1.0', 'https://graph.microsoft.com/beta']
    for r in requests:
        for url_s in absolute_url_starts:
            if r.endpoint.startswith(url_s):
                r.endpoint = r.endpoint.replace(url_s, '', 1)  # just replace the first occurrence

    if ids is None:
        ids = [x for x in range(len(requests))]

    batch = []
    for r_id, r in zip(ids, requests):
        batch.append({'id': r_id, 'method': r.method.upper(), 'url': r.endpoint,
                      'headers': r.header, 'body': r.body})
        if len(batch) % 20 == 0 and len(batch) != 0:
            yield batch
            batch = []
    if len(batch) != 0:
        yield batch
        
def get_auth_header_from_somewhere():
    return {}

async def main():
    group_ids = ['11111111-2222-3333-4444-555555555555', 
                 '11111111-2222-3333-4444-666666666666',
                 '11111111-2222-3333-4444-777777777777']
    
    ms_graph_header = get_auth_header_from_somewhere()
    requests, ids = [], []
    for g_id in group_ids:
        endp_mem = f'https://graph.microsoft.com/v1.0/groups/{g_id}/members'
        endp_own = f'https://graph.microsoft.com/v1.0/groups/{g_id}/owners'
        requests.append(Request('GET', endp_mem, {}, ms_graph_header, {}))
        requests.append(Request('GET', endp_own, {}, ms_graph_header, {}))
        ids.append(f'{g_id}_members')
        ids.append(f'{g_id}_owners')

    
    loop = asyncio.get_event_loop()
    session = await _get_session()

    batch_url = 'https://graph.microsoft.com/v1.0/$batch'
    tasks = [loop.create_task(session.post(url=batch_url, headers=ms_graph_header, json={'requests': batch})) 
             for batch in _create_batches(requests, ids)]
    successful_batches = []
    for batch_resp in await asyncio.gather(*tasks):
        if batch_resp.status != 200:
            # handle failure of entire batch
            continue
        successful_batches.append(batch_resp)
    
    tasks = [loop.create_task(resp.text) for resp in successful_batches]    
    for batch_result in await asyncio.gather(*tasks):
        for response in batch_result['responses']:
            gid_kind = response['id']
            status = response['status']
            if status == 200:
                g_id, kind = gid_kind.split('_')
                groups[g_id][kind] = response['value']
            else:
                # handle failure of single request
                continue


if __name__ == '__main__':
    asyncio.run(main())
```

Oooph ...
Building the JSON batches, sending the requets and then taking them apart again bloated our code massively.
And the worst of it: You would have to do this for every complex operation you would like to perform.


# Introducing: ms-graph-batched

```Python
import asyncio
from batch_wrappers import batched_request
from collections import defaultdict

def get_auth_header_from_somewhere():
    return {}

group_ids = ['11111111-2222-3333-4444-555555555555', 
             '11111111-2222-3333-4444-666666666666',
             '11111111-2222-3333-4444-777777777777']
groups = defaultdict(dict)

endp_mems = [f'https://graph.microsoft.com/v1.0/groups/{g_id}/members' for g_id in group_ids]
endp_owns = [f'https://graph.microsoft.com/v1.0/groups/{g_id}/owners' for g_id in group_ids]
tasks = [batched_request(method='GET', endpoint=endp, header=get_auth_header_from_somewhere()) 
         for endp in [*endp_mems, *endp_owns]]

results = await asyncio.gather(*tasks)  # results in a SINGLE request to Microsoft graph, using JSON batching
mems, owns = results[0:2], results[3:]
for i, g_id in enumerate(group_ids):
    groups[g_id]['members'] = mems[i]
    groups[g_id]['owners'] = owns[i]
```

Isn't this nice ?

_ms-graph-batched_ handles the creation of batches, the request itself, and the deconstruction.
You don't have to fumble with any of it. To the caller, it looks like regular (async) requests.
Everything batch - related is done in the background.

The async wrapper redirects all function calls to batched_request and creates awaitable futures for each call.
It then constructs the JSON batch, calls ms graph, and de-constructs the returned response to 
feed to the individual futures.
