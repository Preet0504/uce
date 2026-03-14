from uce.core.graph_db import GraphDB as _GraphDB
from config import NEO4J_URI, NEO4J_USER, NEO4J_PASS


class GraphDB(_GraphDB):
    def __init__(self):
        super().__init__(NEO4J_URI, NEO4J_USER, NEO4J_PASS)


def create_function(graph: GraphDB, name: str, file_path: str):
    if not name or not file_path:
        raise ValueError("create_function requires name and file_path")

    graph.run(
        "MERGE (f:File {path: $path})",
        path=file_path,
    )
    graph.run(
        "MERGE (fn:Function {name: $name, file_path: $file_path})",
        name=name,
        file_path=file_path,
    )
    graph.run(
        """
        MATCH (f:File {path: $path})
        MATCH (fn:Function {name: $name, file_path: $file_path})
        MERGE (f)-[:DECLARES_FUNCTION]->(fn)
        """,
        path=file_path,
        name=name,
        file_path=file_path,
    )


def link_function_call(
    graph: GraphDB,
    caller_name: str,
    caller_file: str,
    callee_name: str,
    callee_file: str,
):
    if not caller_name or not caller_file or not callee_name or not callee_file:
        raise ValueError("link_function_call requires caller and callee name/file")

    graph.run(
        "MERGE (c:Function {name: $name, file_path: $file_path})",
        name=caller_name,
        file_path=caller_file,
    )
    graph.run(
        "MERGE (c:Function {name: $name, file_path: $file_path})",
        name=callee_name,
        file_path=callee_file,
    )
    graph.run(
        """
        MATCH (caller:Function {name: $caller_name, file_path: $caller_file})
        MATCH (callee:Function {name: $callee_name, file_path: $callee_file})
        MERGE (caller)-[:CALLS]->(callee)
        """,
        caller_name=caller_name,
        caller_file=caller_file,
        callee_name=callee_name,
        callee_file=callee_file,
    )


def link_function_to_api(
    graph: GraphDB,
    function_name: str,
    function_file: str,
    route: str,
    method: str,
    service_name: str | None = None,
):
    if not function_name or not function_file or not route or not method:
        raise ValueError("link_function_to_api requires function, route, and method")

    graph.run(
        "MERGE (fn:Function {name: $name, file_path: $file_path})",
        name=function_name,
        file_path=function_file,
    )
    graph.run(
        "MERGE (a:API {route: $route, method: $method})",
        route=route,
        method=method,
    )
    graph.run(
        """
        MATCH (fn:Function {name: $name, file_path: $file_path})
        MATCH (a:API {route: $route, method: $method})
        MERGE (fn)-[:EXPOSED_AS]->(a)
        """,
        name=function_name,
        file_path=function_file,
        route=route,
        method=method,
    )

    if service_name:
        graph.run(
            "MERGE (s:Service {name: $name})",
            name=service_name,
        )
        graph.run(
            """
            MATCH (a:API {route: $route, method: $method})
            MATCH (s:Service {name: $service})
            MERGE (a)-[:BELONGS_TO]->(s)
            """,
            route=route,
            method=method,
            service=service_name,
        )


def create_service(graph: GraphDB, name: str):
    if not name:
        raise ValueError("create_service requires name")
    graph.run(
        "MERGE (s:Service {name: $name})",
        name=name,
    )


def create_policy(graph: GraphDB, policy_id: str, description: str, requirement_id: str | None = None):
    if not policy_id:
        raise ValueError("create_policy requires policy_id")
    graph.run(
        "MERGE (p:Policy {id: $id}) SET p.description = $description",
        id=policy_id,
        description=description or "",
    )

    if requirement_id:
        graph.run(
            "MERGE (r:Requirement {id: $id})",
            id=requirement_id,
        )
        graph.run(
            """
            MATCH (p:Policy {id: $policy_id})
            MATCH (r:Requirement {id: $req_id})
            MERGE (p)-[:ENFORCES]->(r)
            """,
            policy_id=policy_id,
            req_id=requirement_id,
        )


def create_migration(
    graph: GraphDB,
    name: str,
    table_name: str | None = None,
    column_name: str | None = None,
):
    if not name:
        raise ValueError("create_migration requires name")

    graph.run(
        "MERGE (m:Migration {name: $name})",
        name=name,
    )

    if column_name and not table_name:
        raise ValueError("create_migration requires table_name when column_name is provided")

    if table_name and column_name:
        graph.run(
            """
            MATCH (m:Migration {name: $name})
            MATCH (c:Column {name: $column, table: $table})
            MERGE (m)-[:MODIFIES]->(c)
            """,
            name=name,
            column=column_name,
            table=table_name,
        )
    elif table_name:
        graph.run(
            """
            MATCH (m:Migration {name: $name})
            MATCH (t:Table {name: $table})
            MERGE (m)-[:MODIFIES]->(t)
            """,
            name=name,
            table=table_name,
        )
