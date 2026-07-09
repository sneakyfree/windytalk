"""
Tool definitions (the schema the voice model sees) + dispatch to hands.py.

Keep TOOLS and DISPATCH in sync: every tool's `name` must have an entry in
DISPATCH mapping the JSON args dict to a hands function call.
"""
import hands

TOOLS = [
    {"type": "function", "name": "open_app",
     "description": "Launch a desktop application by name (e.g. 'Firefox', 'terminal', 'files', 'settings').",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string", "description": "App name, .desktop id, or binary."}},
         "required": ["name"]}},

    {"type": "function", "name": "web_search",
     "description": "Open the web browser and search the web for a query.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"}}, "required": ["query"]}},

    {"type": "function", "name": "open_url",
     "description": "Open a specific URL in the default browser.",
     "parameters": {"type": "object", "properties": {
         "url": {"type": "string"}}, "required": ["url"]}},

    {"type": "function", "name": "type_text",
     "description": "Type text into whatever field is currently focused.",
     "parameters": {"type": "object", "properties": {
         "text": {"type": "string"}}, "required": ["text"]}},

    {"type": "function", "name": "press_keys",
     "description": "Press a key or keyboard shortcut, e.g. 'ctrl+c', 'alt+Tab', 'super', 'Return', 'ctrl+shift+t'.",
     "parameters": {"type": "object", "properties": {
         "combo": {"type": "string"}}, "required": ["combo"]}},

    {"type": "function", "name": "click_element",
     "description": "Click an on-screen button, link, or menu item by its visible label (uses the accessibility tree).",
     "parameters": {"type": "object", "properties": {
         "label": {"type": "string"}}, "required": ["label"]}},

    {"type": "function", "name": "mouse_click",
     "description": "Click at absolute screen coordinates. Use only when you know exact pixel coordinates.",
     "parameters": {"type": "object", "properties": {
         "x": {"type": "integer"}, "y": {"type": "integer"},
         "button": {"type": "string", "enum": ["left", "right", "middle"]}},
         "required": ["x", "y"]}},

    {"type": "function", "name": "scroll",
     "description": "Scroll the active window. Negative = down, positive = up.",
     "parameters": {"type": "object", "properties": {
         "amount": {"type": "integer"}}, "required": ["amount"]}},

    {"type": "function", "name": "read_screen",
     "description": "Read the visible text and labels of the active window (no screenshot needed).",
     "parameters": {"type": "object", "properties": {}}},

    {"type": "function", "name": "list_apps",
     "description": "List the currently open, accessible applications.",
     "parameters": {"type": "object", "properties": {}}},

    {"type": "function", "name": "screenshot",
     "description": "Capture a screenshot of the whole screen to a PNG file.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}}}},

    {"type": "function", "name": "run_shell",
     "description": "Run a shell command and return its output. Destructive commands are blocked. Prefer dedicated tools when one fits.",
     "parameters": {"type": "object", "properties": {
         "command": {"type": "string"}}, "required": ["command"]}},
]

DISPATCH = {
    "open_app":      lambda a: hands.open_app(a["name"]),
    "web_search":    lambda a: hands.web_search(a["query"]),
    "open_url":      lambda a: hands.open_url(a["url"]),
    "type_text":     lambda a: hands.type_text(a["text"]),
    "press_keys":    lambda a: hands.press_keys(a["combo"]),
    "click_element": lambda a: hands.click_element(a["label"]),
    "mouse_click":   lambda a: hands.mouse_click(a.get("x"), a.get("y"), a.get("button", "left")),
    "scroll":        lambda a: hands.scroll(a.get("amount", -3)),
    "read_screen":   lambda a: hands.read_screen(),
    "list_apps":     lambda a: hands.list_apps(),
    "screenshot":    lambda a: hands.screenshot(a.get("path", "/tmp/jarvis_shot.png")),
    "run_shell":     lambda a: hands.run_shell(a["command"]),
}


def call_tool(name: str, args: dict) -> str:
    fn = DISPATCH.get(name)
    if fn is None:
        return f"Unknown tool: {name}"
    try:
        return fn(args or {})
    except Exception as e:
        return f"Tool {name} error: {e}"
