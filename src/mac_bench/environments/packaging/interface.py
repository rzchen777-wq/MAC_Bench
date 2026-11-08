"""Business task instructions assembled from common and task-specific materials."""
from pathlib import Path


def business_guide(directory):
    """Render the current guide from its common text and the task's public files."""
    root = Path(directory)
    guide = Path(__file__).with_name('guide.txt').read_text().rstrip()
    operations = root / 'public/tool_operations.md'
    if operations.is_file():
        guide += '\n\n' + operations.read_text().rstrip()
    if (root / 'public/record_forms.json').is_file():
        guide += '\n\n# Administrative Records\nFor public record forms, discover records operations with list_tools(app="twenty", query="records"). Read describe_tool and submit typed fields through call_tool. Each form documents the record it creates, its business effect, and related actions to perform separately. Inspect partial or unknown receipts before further writes.'
    if (root / 'public/data_guide.md').is_file():
        guide += '\n\n# Data Processing\nUse compute for SQL over public JSON and saved observations. Read public/data_guide.md for source tables, output schemas and result references. Derived data does not change application state.'
    if (root / 'public/tool_tutorial.md').is_file():
        guide += '\n\n# Tool Tutorial\nRead public/tool_tutorial.md for an independent worked tool-use example.'
    return guide + '\n'
