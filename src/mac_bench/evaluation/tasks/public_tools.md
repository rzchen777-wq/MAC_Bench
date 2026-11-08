# Business tools from scripts

The shared workspace contains the public task files. Scripts can also call the
same public tools used in conversation, then compute with their results and
save files for later work. Calls are recorded against the agent that launched
the script and use the team's existing application access.

## Discover and call tools

`mac-tools --list` lists public tool names, descriptions and parameter schemas.
Use the existing discovery tools to find and describe application operations.
The command and Python interfaces use the same parameters and return the same
JSON as direct tool calls.

```bash
mac-tools list_resources
```

```python
import json
from mac_tools import call, definitions

resources = call('list_resources')
with open('resources.json', 'w') as output:
    json.dump(resources, output, ensure_ascii=False, indent=2)
print(resources)
```

For any public tool, use `call(tool_name, **arguments)`, or
`mac-tools TOOL_NAME '{"parameter": "value"}'`. Check its description before
calling. Each call counts toward the episode tool budget, including calls made
inside loops and result-navigation calls.

## Operate on applications

Use `call('call_tool', tool_name=operation_name, arguments=parameters,
request_id=request_id)` for a described application operation. Choose a request
ID and retain it with the returned receipt. Request IDs are shared across the
team; use a distinct ID for every independent call, including calls from other
workers. `uuid.uuid4().hex` is one way to generate them. The operation's parameters and
business effects are unchanged by using a script.

Inspect the returned status. A business rejection remains a normal receipt;
Python returning successfully does not mean the business action succeeded.
`ToolClientError` indicates a client or execution failure. If the connection
breaks, the action may already have run. Check the existing receipt and current
state before deciding whether to retry. The client never retries automatically.

Save the `request_id` **before** issuing a write. If its reply is lost, look up
the original request without sending the operation again:

```python
saved = call('read_result', request_id=original_request_id, path='')
print(saved)
```

This lookup returns the saved receipt's status and navigation paths. It never
executes the application action. `request_not_recorded` means no receipt is
currently recorded; an in-flight request may still arrive. `result_unfinished`
means the request was recorded but has no final receipt. Inspect live state
before deciding what to do next. A later request can reconnect after a broken
transport; the failed operation itself is not resent automatically.

## Keep complete results and hand work over

Financial document reads show a compact overview in `data`. When
`data_complete` is false, use `load_result` for the complete record or a specific
path such as `/data/entries` for line items. The overview preserves native field
names and values; it does not replace the stored result.

```python
from mac_tools import load_result

# receipt is the response from a previous application call in this agent.
record = {
    'receipt': receipt,
    'observation': load_result(receipt['result_ref']),
}
with open('observation.json', 'w') as output:
    json.dump(record, output, ensure_ascii=False, indent=2)
```

`load_result` returns `result_ref`, `observed_at`, `source`, `path` and complete
`data`. It follows all navigation pages and nested paths of **one saved
receipt**. It does not issue a fresh application query or fetch further pages
from the application. If an application query returns a page of records, use
that operation's documented pagination to obtain additional records.
`observed_at` records when the receipt completed, not when it was retrieved.

Workers share workspace files. Save the receipt alongside its data so another
worker can identify the source and observation time. A saved file describes
what was observed then; it does not track later application changes. Query the
application again when a decision depends on current state. Writing a file
does not itself modify an application.
