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

```Python
import requests
from collections import defaultdict

group_ids = ['11111111-2222-3333-4444-555555555555', 
             '11111111-2222-3333-4444-666666666666',
             '11111111-2222-3333-4444-777777777777']

groups = defaultdict(dict)
for g_id in group_ids:
    groups[g_id]['members'] = requests.get(f'https://graph.microsoft.com/v1.0/groups/{g_id}/members')
    groups[g_id]['owners'] = requests.get(f'https://graph.microsoft.com/v1.0/groups/{g_id}/owners')
    
```
This is the worst possible way, since the requests are handled synchronous and thus take a very long time.

## The 'not-so-bad-but-still-terrible' way:
```Python
# show how to create batch manually
```


# Introducing: ms-graph-batched

```Python
import asyncio
from batch_wrappers import batched_request
from collections import defaultdict


group_ids = ['11111111-2222-3333-4444-555555555555', 
             '11111111-2222-3333-4444-666666666666',
             '11111111-2222-3333-4444-777777777777']
        
urls_mems = [f'https://graph.microsoft.com/v1.0/groups/{g_id}/members' for g_id in group_ids]
urls_owns = [f'https://graph.microsoft.com/v1.0/groups/{g_id}/owners' for g_id in group_ids]
tasks = [batched_request(method='GET', endpoint=url, header={}) for url in [*urls_mems, *urls_owns]]

results = await asyncio.gather(*tasks)  # results in a SINGLE request to Microsoft graph, using JSON batching
mems, owns = results[0:2], results[3:]
groups = defaultdict(dict)
for i, g_id in enumerate(group_ids):
    groups[g_id]['members'] = mems[i]
    groups[g_id]['owners'] = owns[i]
```

The async wrapper redirects all function calls to batched_request and creates awaitable futures for each call.
It then constructs the JSON batch, calls ms graph, and de-constructs the returned response to 
feed to the individual futures.
