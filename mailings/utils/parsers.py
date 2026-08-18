import json

def parse_client_ids(value):
    """
    Parse comma-separated string or JSON array string into a list of integers.
    
    EXAMPLES:
        "1,2,3"         -> [1, 2, 3]
        "[1, 2, 3]"     -> [1, 2, 3]
        " 1 , 2 , 3 "   -> [1, 2, 3]
    """
    if not value:
        return []
    
    value = value.strip()
    
    # Handle JSON array string
    if value.startswith('['):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [int(item) for item in parsed if str(item).isdigit()]
        except json.JSONDecodeError:
            pass
            
    # Handle comma-separated string
    return [int(item.strip()) for item in value.split(',') if item.strip().isdigit()]


def safe_dict_merge(*dicts) -> dict:
    """Merge multiple dicts, skipping None values."""
    result = {}
    for d in dicts:
        if d and isinstance(d, dict):
            result.update(d)
    return result