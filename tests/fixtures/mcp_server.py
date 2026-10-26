"""Dependency-free JSON-RPC stdio fixture. Inputs are data, never executable code."""
import json
import sys
import time

version = 1
initialized = False
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "notifications/initialized":
        initialized = True
        continue
    request_id = request.get("id")
    if request_id is None:
        continue
    error = None
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": True}}, "serverInfo": {"name": "weyra-fixture", "version": "1"}}
    elif not initialized:
        error = {"code": -32000, "message": "Client initialization notification required"}
    elif method == "tools/list":
        key = "text" if version == 1 else "value"
        result = {"tools": [{"name": "echo", "description": "Local fixture echo", "inputSchema": {"type": "object", "properties": {key: {"type": "string"}}, "required": [key], "additionalProperties": False}}]}
    elif method == "tools/call":
        params = request.get("params", {})
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name == "change_schema":
            version = 2
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}), flush=True)
            result = {"content": [{"type": "text", "text": "schema version 2"}]}
        elif name == "slow":
            time.sleep(0.2)
            result = {"content": [{"type": "text", "text": "late response"}]}
        elif name == "echo":
            key = "text" if version == 1 else "value"
            if set(arguments) != {key} or not isinstance(arguments[key], str):
                error = {"code": -32602, "message": "Arguments do not match the current schema"}
            else:
                result = {"content": [{"type": "text", "text": arguments[key]}]}
        else:
            error = {"code": -32601, "message": "Unknown fixture tool"}
    else:
        error = {"code": -32601, "message": "Unknown method"}
    response = {"jsonrpc": "2.0", "id": request_id}
    response["error" if error else "result"] = error or result
    print(json.dumps(response), flush=True)
