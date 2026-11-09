import os

def resource_file_path(f):
    python_path = os.environ.get("PYTHONPATH")
    if python_path is None:
        directories = ['.']
    else:
        directories = python_path.split(os.pathsep) + ['.']

    for d in directories:
        filepath = os.path.join(d, f)
        if os.path.exists(filepath):
            return filepath

    print(f"File not found: {f}")
    print("Tried the following directories:")
    print(directories)

    raise ValueError(f"File not found: {f}")