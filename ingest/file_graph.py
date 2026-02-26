import os
import re
from graph import GraphDB
from config import PROJECT_ROOT

# -------- CONFIG -------- #

IGNORED_FOLDERS = [
    "components",
    "styles",
    "public",
    ".next",
    "node_modules"
]

IGNORED_FILES = [
    "layout.tsx",
    "page.tsx"
]

IMPORT_REGEX = re.compile(r'import\s+.*?\s+from\s+[\'"](.+?)[\'"]')

# -------- HELPERS -------- #

def is_ignored(path: str) -> bool:
    parts = path.split("/")

    if any(folder in parts for folder in IGNORED_FOLDERS):
        return True

    if any(path.endswith(file) for file in IGNORED_FILES):
        return True

    return False


def normalize(path: str) -> str:
    return path.replace("\\", "/")


def resolve_import(source_relative_path: str, import_path: str):
    """
    Resolves:
    - ./relative imports
    - ../relative imports
    - @/ alias imports (assumed to map to src/)
    """

    # Handle alias @/
    if import_path.startswith("@/"):
        resolved = import_path.replace("@/", "")
        resolved = normalize(resolved)
        return ensure_extension(resolved)

    # Handle relative imports
    if import_path.startswith("."):
        source_dir = os.path.dirname(source_relative_path)
        combined = os.path.normpath(os.path.join(source_dir, import_path))
        combined = normalize(combined)
        return ensure_extension(combined)

    return None


def ensure_extension(path: str):
    """
    Ensures .ts or .tsx exists.
    """
    ts_path = os.path.join(PROJECT_ROOT, path + ".ts")
    tsx_path = os.path.join(PROJECT_ROOT, path + ".tsx")
    direct_path = os.path.join(PROJECT_ROOT, path)

    if os.path.exists(ts_path):
        return normalize(path + ".ts")

    if os.path.exists(tsx_path):
        return normalize(path + ".tsx")

    if os.path.exists(direct_path):
        return normalize(path)

    return None


# -------- INGESTION -------- #

def ingest_files():
    graph = GraphDB()

    print("Starting ingestion...")

    for root, _, files in os.walk(PROJECT_ROOT):
        for file in files:
            if not (file.endswith(".ts") or file.endswith(".tsx")):
                continue

            full_path = os.path.join(root, file)
            relative_path = os.path.relpath(full_path, PROJECT_ROOT)
            relative_path = normalize(relative_path)

            if is_ignored(relative_path):
                continue

            # Create File node
            graph.run(
                "MERGE (f:File {path: $path})",
                path=relative_path
            )

            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()

            imports = IMPORT_REGEX.findall(content)

            for imp in imports:
                resolved = resolve_import(relative_path, imp)
                if resolved and not is_ignored(resolved):

                    # Ensure target node exists
                    graph.run(
                        "MERGE (f:File {path: $path})",
                        path=resolved
                    )

                    # Create IMPORT edge
                    graph.run("""
                        MATCH (a:File {path: $source})
                        MATCH (b:File {path: $target})
                        MERGE (a)-[:IMPORTS]->(b)
                    """,
                        source=relative_path,
                        target=resolved
                    )

    graph.close()
    print("Ingestion complete.")


if __name__ == "__main__":
    ingest_files()