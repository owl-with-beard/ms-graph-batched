# ms-graph-batched
Small async Python wrapper to make batched requests to the Microsoft graph API.

# Usage:

from batch_wrappers import batched_request

header = get_auth_header_from_somewhere()

upns = ["robert.smith@contoso.com",
        "william.doe@contoso.com",
        "mary.jones@contoso.com"]
        
endpoints = [f'https://graph.microsoft.com/v1.0/users/{upn}' for upn in upns]

tasks = [batched_request(method='GET', endpoint=endp, parameter={}, header=header, body={}) for endp in endpoints]

results = await asyncio.gather(*tasks)

--> This results in a single HTTP call to Microsoft graph, using JSON batching.
