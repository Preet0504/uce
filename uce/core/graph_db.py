from __future__ import annotations

from neo4j import GraphDatabase


class GraphDB:
    def __init__(self, uri: str, user: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self.driver.close()

    def run(self, query: str, **params):
        with self.driver.session() as session:
            return list(session.run(query, **params))

    def ensure_file(self, path: str) -> None:
        self.run(
            "MERGE (f:File {path: $path}) SET f.last_seen = timestamp()",
            path=path,
        )

    def clear_file_relationships(self, path: str) -> None:
        self.run(
            "MATCH (f:File {path: $path})-[r]->() DELETE r",
            path=path,
        )
        self.run(
            "MATCH (fn:Function {file_path: $path})-[r]->() DELETE r",
            path=path,
        )
        self.run(
            "MATCH (c:Class {file_path: $path})-[r]->() DELETE r",
            path=path,
        )
        self.run(
            "MATCH (m:Method {file_path: $path})-[r]->() DELETE r",
            path=path,
        )

    def delete_file(self, path: str) -> None:
        self.run("MATCH (f:File {path: $path}) DETACH DELETE f", path=path)
        self.run("MATCH (fn:Function {file_path: $path}) DETACH DELETE fn", path=path)
        self.run("MATCH (c:Class {file_path: $path}) DETACH DELETE c", path=path)
        self.run("MATCH (m:Method {file_path: $path}) DETACH DELETE m", path=path)

    def cleanup_orphan_apis(self) -> None:
        self.run(
            "MATCH (a:API) WHERE NOT ( ()-[:EXPOSED_AS]->(a) ) DETACH DELETE a"
        )
        self.run(
            "MATCH (s:Service) WHERE NOT ( ()-[:BELONGS_TO]->(s) ) DETACH DELETE s"
        )

    def cleanup_missing_files(self, known_paths: list[str]) -> None:
        if not known_paths:
            return
        self.run(
            "MATCH (f:File) WHERE NOT f.path IN $paths DETACH DELETE f",
            paths=known_paths,
        )
