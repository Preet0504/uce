from neo4j import GraphDatabase
from config import NEO4J_URI, NEO4J_USER, NEO4J_PASS

class GraphDB:
    def __init__(self):
        self.driver = GraphDatabase.driver(
            NEO4J_URI,
            auth=(NEO4J_USER, NEO4J_PASS)
        )

    def close(self):
        self.driver.close()

    def run(self, query, **params):
        with self.driver.session() as session:
            return list(session.run(query, **params))