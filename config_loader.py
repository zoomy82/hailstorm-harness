import json


def load_config(file_path):
    """Load configurations from a JSON file."""
    try:
        with open(file_path, "r") as file:
            return json.load(file)

    except FileNotFoundError:
        raise FileNotFoundError(f"Configuration file '{file_path}' not found.")
    except json.JSONDecodeError:
        raise ValueError(f"Configuration file '{file_path}' is not valid JSON.")