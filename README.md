# ms-graph-batched
Small async Python wrapper to make batched requests to the Microsoft graph API.

# Usage:

from batch_wrappers import batched_request

upns = ["robert.smith@contoso.com",
        "william.doe@@contoso.com",
        "mary.jones@contoso.com"]

endpoints = [f'https://graph.microsoft.com/v1.0/users/{upn}' for upn in upns]
header = get_auth_header_from_somewhere()
tasks = [batched_call(method='GET', endpoint=endp, parameter={}, header=header, body={}) for endp in endpoints]
results = await asyncio.gather(*tasks)
