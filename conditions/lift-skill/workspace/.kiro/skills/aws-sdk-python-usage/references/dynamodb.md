# DynamoDB Reference

Use the resource interface to work with native Python types instead of AttributeValue dicts:

```python
import boto3
from boto3.dynamodb.conditions import Key, Attr

table = boto3.resource("dynamodb").Table("my-table")
table.put_item(Item={"pk": "user#1", "name": "Alice", "age": 30})
item = table.get_item(Key={"pk": "user#1"}).get("Item")
```

## Common Pitfall: AttributeValue Dicts

If you see `{"id": {"S": "1"}, "count": {"N": "42"}}` instead of `{"id": "1", "count": 42}`, you're using `boto3.client("dynamodb")` which does not auto-marshal types. You have two options:

1. Use the **resource interface** (recommended) -- `Table` methods auto-marshal types.
2. Use the **resource's underlying client** -- a low-level client that still
   auto-marshals types is available through the `.meta.client` attribute of a
   resource type:

```python

# Instead of: boto3.client('dynamodb')
# you can use `boto3.resource('dynamodb').meta.client`.
# This is still a boto3 DynamoDB client with custom handlers
# to automatically marshal to the AttributeValue dict types.
dynamodb = boto3.resource("dynamodb").meta.client
# This client auto-converts Python types to/from DynamoDB AttributeValue format
response = dynamodb.get_item(TableName="my-table", Key={"pk": "user#1"})
item = response.get("Item")  # {"pk": "user#1", "name": "Alice"} -- plain Python types
```

ALWAYS prefer using native python types instead of low level AttributeValue
dicts.  These are more idiomatic for Python developers to work with and handle the
conversion and various edge cases automatically for you.

## Resource Interface (Recommended)

```python
import boto3
from boto3.dynamodb.conditions import Key, Attr
from decimal import Decimal

table = boto3.resource("dynamodb").Table("my-table")
```

### Scan

```python
# Full table scan (expensive -- prefer query when possible)
response = table.scan()
items = response["Items"]

# Scan with filter
response = table.scan(
    FilterExpression=Attr("age").gte(18) & Attr("status").eq("active"),
)
```

## Pagination (Query / Scan)

DynamoDB returns up to 1MB per call. Use the resource's underlying client to get paginators with auto-marshalled types:

```python
dynamodb = boto3.resource("dynamodb").meta.client
paginator = dynamodb.get_paginator("scan")
for page in paginator.paginate(TableName="my-table"):
    for item in page["Items"]:
        print(item)
```

If you must use the low-level client directly, loop on `LastEvaluatedKey` until it is absent so no items are dropped beyond the first 1MB page:

```python
import boto3

def scan_all_items(table_name: str) -> list[dict]:
    client = boto3.client("dynamodb")
    items = []
    kwargs = {"TableName": table_name}
    while True:
        response = client.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items
```
